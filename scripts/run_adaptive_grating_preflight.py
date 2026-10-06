"""Resumable real-mask preflight for an existing adaptive grating study.

All changes go through researcher commands. SQLite is opened read-only for
precise status and asset reads, avoiding repeated complete campaign snapshots.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from copy import deepcopy
import fcntl
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
from typing import Any
from urllib.parse import quote

import httpx

from dqn_meent.problem_2d import ORDER_CONVERGENCE_RECIPE
from optimization_framework.storage.artifacts import atomic_json


ALGORITHMS = {"flrl_ppo", "flrl_ppo_residual", "flrl_autograd_adam", "flrl_lsf_es",
              "flrl_lsf_random", "phenotype_de", "motif_surgery", "nested_fourier"}
LIVE_STATUSES = {"queued", "running", "pausing", "stopping"}
DEFAULT_FIDELITIES = [{"rcwa_order_x": x, "rcwa_order_y": y}
                      for x, y in ((10, 5), (12, 6), (14, 7))]
HIGHER_FIDELITIES = [{"rcwa_order_x": x, "rcwa_order_y": y}
                     for x, y in ((18, 9), (22, 11), (26, 13), (30, 15), (34, 17))]
MEMORY_HEADROOM_BYTES = 4 * 1024**3


def available_memory_bytes():
    try:
        return int(next(line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines()
                        if line.startswith("MemAvailable:"))) * 1024
    except (OSError, ValueError, StopIteration):
        return None


def trusted_worker_memory(trial, proc_directory=Path("/proc")):
    """Read only the owned worker PID; never attribute a reused PID's memory."""
    pid, expected = trial.get("pid"), trial.get("process_identity")
    if not pid or not expected:
        return None
    try:
        directory = proc_directory / str(int(pid))
        stat = (directory / "stat").read_text()
        tail = stat[stat.rfind(")") + 2:].split()
        if tail[0] == "Z" or tail[19] != str(expected):
            return None
        fields = {line.split(":", 1)[0]: line.split(":", 1)[1].strip()
                  for line in (directory / "status").read_text().splitlines() if ":" in line}
        measured = {name: int(fields[key].split()[0]) * 1024 for name, key in
                    (("peak_rss_bytes", "VmHWM"), ("rss_bytes", "VmRSS"), ("swap_bytes", "VmSwap"))}
        return {"pid": int(pid), "process_identity": str(expected), **measured}
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return None


def harmonic_count(fidelity):
    return (2 * fidelity["rcwa_order_x"] + 1) * (2 * fidelity["rcwa_order_y"] + 1)


def predict_validation_memory(fidelity, samples, floor_bytes=5 * 1024**3):
    measured = [sample for sample in samples if sample.get("completed_evaluations", 0) > 0
                and sample.get("phase") == "validation" and sample.get("peak_rss_bytes", 0) > 0]
    if not measured:
        measured = [{"peak_rss_bytes": 5 * 1024**3, "fidelity": DEFAULT_FIDELITIES[-1]}]
        basis = "conservative unmeasured 5 GiB planning allowance at (14,7), already including headroom margin"
        factor = 1
    else:
        highest_measured = max(harmonic_count(sample["fidelity"]) for sample in measured)
        measured = [sample for sample in measured if harmonic_count(sample["fidelity"]) == highest_measured]
        basis = "identity-checked worker VmHWM after completed physical evaluations"
        factor = 2
    predictions = [factor * sample["peak_rss_bytes"] *
                   (harmonic_count(fidelity) / harmonic_count(sample["fidelity"])) ** 2 for sample in measured]
    return {"fidelity": deepcopy(fidelity), "predicted_peak_bytes": max(int(floor_bytes), math.ceil(max(predictions))),
            "memory_headroom_bytes": MEMORY_HEADROOM_BYTES, "available_memory_bytes": available_memory_bytes(),
            "growth_model": ("2 * observed peak RSS * (target harmonics / measured harmonics)^2" if factor == 2 else
                             "existing conservative planning allowance * (target harmonics / reference harmonics)^2"),
            "basis": basis, "measured_samples": len(measured) if "identity-checked" in basis else 0}


def truncation_failure(trial):
    """Order escalation addresses truncation disagreement, not invalid solves."""
    result = trial.get("result") or trial.get("progress") or {}
    summary = result.get("recipe_result", {})
    if not result.get("scientific_complete") or not summary.get("complete"):
        return False
    findings = summary.get("subjects", [])
    if not findings:
        return False
    for finding in findings:
        parameters = finding.get("parameters", {})
        energy = finding.get("max_energy_error")
        repeats = finding.get("repeat_max_absolute_differences")
        differences = finding.get("last_two_absolute_differences")
        if (finding.get("invalid_evidence") or finding.get("missing_evidence") or energy is None
                or energy > parameters.get("energy_tolerance", .001) or not repeats or not differences
                or max(repeats.values()) > parameters.get("repeat_tolerance", 1e-6)):
            return False
    return any(max(finding["last_two_absolute_differences"].values()) >
               finding.get("parameters", {}).get("absolute_tolerance", .005) for finding in findings)


class WaitingForEvidence(RuntimeError):
    """A prerequisite is not yet present; retain state and try again later."""


class CommandRejected(RuntimeError):
    pass


def read_json(path: Path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def content_digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


def select_fixtures(results: list[dict]) -> list[dict]:
    """Freeze median endpoint masks and the distinct highest Adam endpoint."""
    grouped = defaultdict(list)
    for row in results:
        if row.get("algorithm") in ALGORITHMS:
            if row.get("best_mean") is None or not isinstance(row.get("best_observation_id"), str):
                raise ValueError("Every fixture source requires a measured endpoint and observation ID")
            grouped[row["algorithm"]].append(row)
    if set(grouped) != ALGORITHMS:
        raise ValueError("Preflight requires the eight declared pilot algorithm configurations")
    fixtures = []
    for algorithm in sorted(grouped):
        rows = grouped[algorithm]
        if len(rows) < 3 or len({row["seed"] for row in rows}) != len(rows):
            raise ValueError(f"{algorithm} requires at least three distinct pilot seeds")
        if len({row["config_label"] for row in rows}) != 1:
            raise ValueError("Different configurations cannot be combined to choose a median fixture")
        row = sorted(rows, key=lambda item: (item["best_mean"], item["seed"]))[len(rows) // 2]
        fixtures.append(_fixture(algorithm + "_median", row, "median endpoint"))
    outlier = max(grouped["flrl_autograd_adam"], key=lambda row: (row["best_mean"], row["seed"]))
    if any(item["source_trial_id"] == outlier["trial_id"] for item in fixtures):
        raise ValueError("Adam maximum and median are identical; supply a distinct asymmetry fixture explicitly")
    fixtures.append(_fixture("flrl_autograd_adam_outlier", outlier, "highest Adam endpoint"))
    return fixtures


def _fixture(identity, row, role):
    return {"fixture_id": identity, "role": role, "algorithm": row["algorithm"],
            "config_label": row["config_label"], "seed": row["seed"],
            "source_trial_id": row["trial_id"], "source_observation_id": row["best_observation_id"],
            "screening_mean": row["best_mean"], "screening_te": row.get("best_te"),
            "screening_tm": row.get("best_tm"), "phase": "asset", "validation_attempts": []}


class ReadOnlyRecords:
    def __init__(self, database: Path):
        self.database = database.resolve()

    def _query(self, sql, parameters=()):
        with sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True, timeout=10) as connection:
            return connection.execute(sql, parameters).fetchall()

    def get(self, identity, kind=None):
        query = "SELECT data FROM records WHERE id=?" + (" AND kind=?" if kind else "")
        rows = self._query(query, (identity, kind) if kind else (identity,))
        if not rows:
            raise WaitingForEvidence(f"Missing {kind or 'record'}: {identity}")
        return json.loads(rows[0][0])

    def campaign_revision(self, campaign_id):
        return self.get(campaign_id, "campaign")["version"]

    def solution_for_fixture(self, fixture):
        rows = self._query("SELECT data FROM records WHERE kind='asset' AND "
                           "json_extract(data,'$.producer_id')=? AND json_extract(data,'$.kind')='solution'",
                           (fixture["source_trial_id"],))
        matches = []
        for (data,) in rows:
            asset = json.loads(data)
            payload = asset.get("payload", {})
            observation_id = payload.get("observation_id") or payload.get("observation", {}).get("id")
            if observation_id != fixture["source_observation_id"]:
                continue
            if not isinstance(payload.get("candidate"), list):
                continue
            matches.append(asset)
        matches.sort(key=lambda asset: (not asset.get("title", "").endswith("archived solution 1"), asset["id"]))
        if not matches:
            raise WaitingForEvidence(f"{fixture['fixture_id']}: source {fixture['source_trial_id']} lacks an immutable "
                f"solution for {fixture['source_observation_id']}. Publish a journal-backed snapshot; do not substitute another seed.")
        return matches[0]


class DurableCommands:
    """Save exact requests before dispatch and reconcile receipts after timeouts."""
    def __init__(self, url: str, campaign_id: str, directory: Path, records: ReadOnlyRecords,
                 http=None, namespace="grating_preflight"):
        self.campaign_id, self.records, self.namespace = campaign_id, records, namespace
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.http = http or httpx.Client(base_url=url.rstrip("/"), timeout=60)

    def close(self):
        self.http.close()

    def execute(self, key: str, operation: str, payload: dict):
        identity = "preflight_" + content_digest([self.namespace, key])[:48]
        path = self.directory / (identity + ".json")
        saved = read_json(path)
        if saved:
            request = saved["request"]
            if request["operation"] != operation or request["payload"] != payload:
                raise ValueError("A saved command cannot change its operation or scientific payload")
        else:
            request = {"id": identity, "campaign_id": self.campaign_id, "operation": operation,
                       "expected_revision": self.records.campaign_revision(self.campaign_id), "payload": deepcopy(payload)}
            saved = {"request": request}
            atomic_json(path, saved)
        if saved.get("receipt"):
            return saved["receipt"]["outcome"]
        response = self.http.get("/api/v1/commands/" + quote(request["id"], safe=""))
        if response.status_code == 404:
            revision = self.records.campaign_revision(self.campaign_id)
            if saved.get("rejection", {}).get("status_code") in {400, 409} and revision != request["expected_revision"]:
                saved.setdefault("rejected_requests", []).append({"request": deepcopy(request), "rejection": saved["rejection"]})
                request = {**request, "id": identity + "_r" + str(revision), "expected_revision": revision}
                saved["request"] = request
                saved.pop("rejection", None)
                atomic_json(path, saved)
                response = self.http.get("/api/v1/commands/" + quote(request["id"], safe=""))
            if response.status_code == 404:
                response = self.http.post("/api/v1/commands", json=request)
        if response.status_code >= 400:
            # An uncertain network result is handled by reconciling the same ID
            # on the next turn. A proven rejection is retained for inspection.
            saved["rejection"] = {"status_code": response.status_code, "detail": response.text}
            atomic_json(path, saved)
            raise CommandRejected(f"{operation}: HTTP {response.status_code}: {response.text}")
        receipt = response.json()
        if receipt.get("actor") != "researcher" or receipt.get("request") != {"schema_version": 1, **request}:
            # Older server serializers may omit the constant schema version.
            received = deepcopy(receipt.get("request", {}))
            received.pop("schema_version", None)
            received = {key: value for key, value in received.items() if value is not None or key in request}
            if receipt.get("actor") != "researcher" or received != request:
                raise ValueError("The accepted receipt differs from the exact saved researcher command")
        if receipt.get("status") != "completed" or not isinstance(receipt.get("outcome"), dict):
            raise WaitingForEvidence("Saved command receipt has not completed")
        saved["receipt"] = receipt
        atomic_json(path, saved)
        return receipt["outcome"]


def compact_result(trial):
    result = trial.get("result") or trial.get("progress") or {}
    summary = result.get("recipe_result", {})
    compact = {key: summary.get(key) for key in ("kind", "recipe_id", "complete", "verdict")}
    compact["subjects"] = [{key: subject.get(key) for key in
        ("design_index", "complete", "converged", "verdict", "numerical_status", "last_two_absolute_differences",
         "repeat_max_absolute_differences", "max_energy_error", "highest_fidelity", "missing_evidence", "invalid_evidence")}
        for subject in summary.get("subjects", [])]
    return {"trial_id": trial["id"], "status": trial["status"], "reason": trial.get("reason"),
            "scientific_complete": result.get("scientific_complete", False),
            "worker_seconds": result.get("elapsed_seconds"), "solver_executions": result.get("solver_calls"),
            "recipe_result": compact}


def recipe_passed(trial):
    result = compact_result(trial)
    summary = result["recipe_result"]
    return bool(result["status"] not in {"failed", "interrupted", "stopped"} and result["scientific_complete"]
                and summary.get("recipe_id") == ORDER_CONVERGENCE_RECIPE and summary.get("complete")
                and summary.get("verdict") == "passed" and len(summary.get("subjects", [])) == 1
                and all(subject.get("complete") and subject.get("verdict") == "passed"
                        for subject in summary["subjects"]))


class PreflightRunner:
    """One durable state transition at a time; never allocate a second live job."""
    def __init__(self, directory: Path, records: ReadOnlyRecords, commands: DurableCommands,
                 *, campaign_id: str, task_id: str, pilot_results: Path, race_id=None, resource_profile=None):
        self.directory, self.records, self.commands = directory, records, commands
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "preflight.json"
        source = read_json(pilot_results)
        if not isinstance(source, list):
            raise ValueError("Pilot results must be the saved list of measured endpoint records")
        source_digest = content_digest(source)
        saved = read_json(self.path)
        if saved:
            if (saved["campaign_id"], saved["task_id"], saved["pilot_results_digest"]) != (campaign_id, task_id, source_digest):
                raise ValueError("A resumed preflight must retain its campaign, task, and exact source evidence")
            self.manifest = saved
        else:
            self.manifest = {"schema_version": 1, "campaign_id": campaign_id, "task_id": task_id,
                "pilot_results": str(pilot_results.resolve()), "pilot_results_digest": source_digest,
                "created_at": time.time(), "status": "preparing", "fixtures": select_fixtures(source),
                "events": [], "resource_profile": str(resource_profile.resolve()) if resource_profile else None}
            self.save()
        if race_id:
            if self.manifest.get("race_id") not in {None, race_id}:
                raise ValueError("The preflight is already bound to another immutable race")
            race = self.records.get(race_id, "adaptive_race")
            if race["campaign_id"] != campaign_id or race["task_id"] != task_id:
                raise ValueError("The race must belong to the declared campaign and physical task")
            self.manifest["race_id"] = race_id
            self.manifest["study_id"] = race["study_id"]
        if resource_profile:
            self.manifest["resource_profile"] = str(resource_profile.resolve())
        self.save()

    def save(self):
        self.manifest["updated_at"] = time.time()
        atomic_json(self.path, self.manifest)
        self.write_report()

    def event(self, kind, detail):
        self.manifest["events"].append({"time": time.time(), "kind": kind, "detail": detail})
        self.save()

    def write_report(self):
        lines = ["# Actual 2D mask preflight", "", f"Status: **{self.manifest['status']}**.", "",
                 "Every measurement and convergence check uses an actual archived pilot mask. "
                 "Missing or incomplete evidence is never counted as a pass.", "",
                 "| Fixture | Seed | Original mean | Fresh mean | Phase | Numerical verdict |",
                 "|---|---:|---:|---:|---|---|"]
        for fixture in self.manifest["fixtures"]:
            verdict = (fixture["validation_attempts"][-1].get("result", {}).get("recipe_result", {}).get("verdict", "pending")
                       if fixture["validation_attempts"] else "pending")
            fresh = fixture.get("fresh_mean")
            lines.append(f"| {fixture['fixture_id']} | {fixture['seed']} | {fixture['screening_mean']:.6f} | "
                         f"{fresh:.6f}" + f" | {fixture['phase']} | {verdict} |" if fresh is not None else
                         f"| {fixture['fixture_id']} | {fixture['seed']} | {fixture['screening_mean']:.6f} | — | {fixture['phase']} | {verdict} |")
        lines += ["", "## Failed or incomplete checks", ""]
        failures = 0
        for fixture in self.manifest["fixtures"]:
            for attempt in fixture["validation_attempts"]:
                result = attempt.get("result")
                if not result or result["recipe_result"].get("verdict") == "passed":
                    continue
                failures += 1
                lines.append(f"- `{fixture['fixture_id']}` / `{attempt['trial_id']}`: "
                             f"{result['status']}; {result['recipe_result'].get('verdict')}; {result.get('reason') or 'see evidence'}. "
                             f"Measurements: `{json.dumps(result['recipe_result'].get('subjects', []), sort_keys=True)}`")
        if not failures:
            lines.append("No completed failed checks recorded.")
        if self.manifest.get("waiting_reason"):
            lines += ["", "## Pending prerequisite", "", self.manifest["waiting_reason"]]
        if self.manifest.get("memory_predictions"):
            lines += ["", "## Numerical memory admission", "", "Predictions use measured resident memory, "
                      "a factor of two for uncertainty, quadratic harmonic growth, and 4 GiB of headroom.", ""]
            for prediction in self.manifest["memory_predictions"]:
                lines.append(f"- Fidelity `{prediction['fidelity']}`: predicted "
                    f"{prediction['predicted_peak_bytes'] / 1024**3:.2f} GiB; available "
                    f"{(prediction.get('available_memory_bytes') or 0) / 1024**3:.2f} GiB; {prediction['basis']}.")
        (self.directory / "preflight-report.md").write_text("\n".join(lines) + "\n")

    def _command(self, fixture, operation, suffix, payload):
        return self.commands.execute(f"{fixture['fixture_id']}_{suffix}", operation, payload)

    def _register(self, trial_id):
        trial = self.records.get(trial_id, "trial")
        fidelities = trial.get("recipe", {}).get("parameters", {}).get("fidelities", [])
        profile = {}
        if fidelities:
            highest = fidelities[-1]
            if harmonic_count(highest) > harmonic_count(DEFAULT_FIDELITIES[-1]):
                race = self.records.get(self.manifest["race_id"], "adaptive_race")
                prediction = predict_validation_memory(highest, self.manifest.get("worker_memory_samples", []),
                    race.get("profile", {}).get("validation_peak_bytes", 5 * 1024**3))
                profile["validation_peak_bytes"] = prediction["predicted_peak_bytes"]
        return self.commands.execute("register_" + trial_id, "study.race.decide", {
            "race_id": self.manifest["race_id"], "action": "register_preflight", "evidence_trial_ids": [trial_id],
            "profile": profile,
            "rationale": "Register this actual frozen-mask preflight job before the race scheduler admits it."})

    def _prepare_asset(self, fixture):
        try:
            asset = self.records.solution_for_fixture(fixture)
        except WaitingForEvidence:
            outcome = self._command(fixture, "asset.snapshot", "source_snapshot", {
                "trial_id": fixture["source_trial_id"], "observation_id": fixture["source_observation_id"]})
            asset = self.records.get(outcome["asset_id"], "asset")
        payload = asset.get("payload", {})
        if asset.get("producer_id") != fixture["source_trial_id"] or not isinstance(payload.get("candidate"), list):
            raise ValueError("The returned fixture asset differs from its declared source trial")
        fixture.update(asset_id=asset["id"], candidate_digest=content_digest(payload["candidate"]), phase="reuse")
        self.save()

    def step(self, *, prepare_only=False):
        self.manifest.pop("waiting_reason", None)
        for fixture in self.manifest["fixtures"]:
            if fixture["phase"] == "asset":
                self._prepare_asset(fixture)
                return True
        if prepare_only:
            self.manifest["status"] = "prepared"
            self.save()
            return False
        if not self.manifest.get("race_id"):
            raise WaitingForEvidence("Prepared fixtures require an existing race with their nine source asset IDs frozen.")
        race = self.records.get(self.manifest["race_id"], "adaptive_race")
        if race["status"] in {"stopped", "completed", "budget_exhausted"} or time.time() >= race["deadline_at"]:
            self.manifest["status"] = "execution_closed"
            self.save()
            return False
        if race["status"] == "paused":
            if self.manifest["status"] == "numerically_unresolved":
                return False
            raise WaitingForEvidence("The race is paused; its original elapsed deadline remains fixed.")
        if race.get("preflight", {}).get("status") == "passed":
            self.manifest["status"] = "completed"
            self.save()
            return False
        self.manifest["status"] = "running"
        # Worker reports use the framework's objective scalar; recover this
        # projection when resuming an operator that predates that field.
        for fixture in self.manifest["fixtures"]:
            if fixture.get("measurement_trial_id") and fixture.get("fresh_mean") is None:
                source = self.records.get(fixture["measurement_trial_id"], "trial")
                result = source.get("result") or source.get("progress") or {}
                if source["status"] == "completed" and result.get("scientific_complete"):
                    score = result.get("best_objective", result.get("best_mean_plus1_transmission"))
                    if type(score) in (int, float):
                        fixture.update(fresh_mean=score, reproduction_delta=abs(score - fixture["screening_mean"]))
        for fixture in self.manifest["fixtures"]:
            if fixture["phase"] == "done":
                continue
            if fixture["phase"] in {"numerical_followup", "measurement_incomplete"}:
                continue
            self._advance_fixture(fixture)
            return True
        if any(fixture["phase"] == "measurement_incomplete" for fixture in self.manifest["fixtures"]):
            self._recover_measurements()
            return True
        if any(fixture["phase"] == "numerical_followup" for fixture in self.manifest["fixtures"]):
            return self._escalate_numerical()
        self._accept()
        return True

    def _advance_fixture(self, fixture):
        phase = fixture["phase"]
        if phase == "reuse":
            outcome = self._command(fixture, "asset.reuse", "reuse", {
                "asset_id": fixture["asset_id"], "study_id": self.manifest["study_id"], "decision": "reuse",
                "intended_use": "optimizer_input", "rationale": "Measure exactly the declared pilot endpoint mask; "
                    "freeze its provenance and perform 2D order/energy/repeat checks before new algorithm comparisons."})
            fixture.update(reuse_decision_id=outcome["reuse_decision_id"], phase="measurement_create")
        elif phase == "measurement_create":
            outcome = self._command(fixture, "trial.create", "measurement", {
                "task_id": self.manifest["task_id"], "algorithm": "evaluate_asset", "seed": fixture["seed"],
                "max_steps": 1, "wall_seconds": 180, "numerical_threads": 1,
                "initial_assets": [fixture["asset_id"]], "reuse_decision_ids": [fixture["reuse_decision_id"]],
                "race_id": self.manifest["race_id"], "race_phase": "preflight",
                "question": f"Fresh current-code measurement of frozen fixture {fixture['fixture_id']} before 2D convergence validation."})
            fixture.update(measurement_trial_id=outcome["trial_id"], phase="measurement_register")
        elif phase == "measurement_register":
            self._register(fixture["measurement_trial_id"])
            fixture["phase"] = "measurement_wait"
        elif phase == "measurement_wait":
            trial = self.records.get(fixture["measurement_trial_id"], "trial")
            if trial["status"] in LIVE_STATUSES:
                self._record_worker_memory(trial, fixture, "measurement")
                raise WaitingForEvidence(f"Measuring {fixture['fixture_id']} in {trial['id']} ({trial['status']}).")
            result = trial.get("result") or trial.get("progress") or {}
            if trial["status"] != "completed" or not result.get("scientific_complete"):
                fixture["phase"] = "measurement_incomplete"
                fixture["measurement_failure"] = {"status": trial["status"], "reason": trial.get("reason")}
                self.save()
                raise WaitingForEvidence(f"Fixture measurement {trial['id']} requires recovery; no duplicate trial has been dispatched.")
            best = result.get("best_objectives", {}).get("mean_plus1_transmission",
                result.get("best_objective", result.get("best_mean_plus1_transmission", result.get("best_efficiency"))))
            fixture.update(fresh_mean=best, reproduction_delta=abs(best - fixture["screening_mean"]) if best is not None else None,
                           phase="validation_create")
        elif phase == "validation_create":
            generation = len(fixture["validation_attempts"])
            parameters = {"fidelities": deepcopy(self.manifest.get("fidelities", DEFAULT_FIDELITIES))}
            outcome = self._command(fixture, "validation.run", f"validation_{generation}", {
                "trial_id": fixture["measurement_trial_id"], "recipe_id": ORDER_CONVERGENCE_RECIPE,
                "parameters": parameters, "subject_limit": 1, "wall_seconds": 600})
            fixture["validation_attempts"].append({"trial_id": outcome["trial_id"], "parameters": parameters})
            fixture["phase"] = "validation_register"
        elif phase == "validation_register":
            self._register(fixture["validation_attempts"][-1]["trial_id"])
            fixture["phase"] = "validation_wait"
        elif phase == "validation_wait":
            attempt = fixture["validation_attempts"][-1]
            trial = self.records.get(attempt["trial_id"], "trial")
            if trial["status"] in LIVE_STATUSES:
                self._record_worker_memory(trial, fixture, "validation")
                raise WaitingForEvidence(f"Checking {fixture['fixture_id']} in {trial['id']} ({trial['status']}).")
            attempt["result"] = compact_result(trial)
            fixture["phase"] = "done" if recipe_passed(trial) else "numerical_followup"
        elif phase in {"numerical_followup", "measurement_incomplete"}:
            raise WaitingForEvidence(f"{fixture['fixture_id']} has an incomplete or failed real check; "
                                     "its evidence is retained and needs a declared higher-fidelity or recovery follow-up.")
        else:
            raise ValueError(f"Unknown durable fixture phase: {phase}")
        self.save()

    def _record_worker_memory(self, trial, fixture, phase):
        memory = trusted_worker_memory(trial)
        if not memory:
            return
        progress = trial.get("progress") or {}
        completed = progress.get("step", 0)
        if type(completed) is not int or completed < 1:
            return
        cases = trial.get("recipe", {}).get("cases", [])
        fidelity = (cases[min(completed - 1, len(cases) - 1)]["problem"]["fidelity"] if cases else
                    self.records.get(self.manifest["task_id"], "task")["problem"]["fidelity"])
        if cases and fidelity != cases[-1]["problem"]["fidelity"]:
            # VmHWM may already include the next case's allocations. Associate
            # it only with a completed highest-order solve and its repeat.
            return
        sample = {**memory, "trial_id": trial["id"], "fixture_id": fixture["fixture_id"], "phase": phase,
                  "fidelity": fidelity, "completed_evaluations": completed, "sampled_at": time.time()}
        self.manifest.setdefault("worker_memory_samples", []).append(sample)
        fixture["observed_peak_rss_bytes"] = max(fixture.get("observed_peak_rss_bytes", 0), memory["peak_rss_bytes"])
        self.save()

    def _accept(self):
        changed = False
        original_fidelity = self.records.get(self.manifest["task_id"], "task")["problem"]["fidelity"]
        for fixture in self.manifest["fixtures"]:
            check = self.records.get(fixture["validation_attempts"][-1]["trial_id"], "trial")
            subject = (check.get("result") or check.get("progress"))["recipe_result"]["subjects"][0]
            source = next(row for row in subject["observations"] if row["fidelity"] == original_fidelity)
            final = next(row for row in subject["observations"] if row["fidelity"] == subject["highest_fidelity"])
            changed |= any(abs(source["objectives"][name] - final["objectives"][name]) > .005
                           for name in ("mean_plus1_transmission", "te_plus1_transmission", "tm_plus1_transmission"))
        selected_task_id = self._higher_fidelity_task() if changed else self.manifest["task_id"]
        selected_task = self.records.get(selected_task_id, "task")
        self.manifest.update(selected_task_id=selected_task_id, currenttask_fidelity=selected_task["problem"]["fidelity"],
                             numerical_status="passed")
        self.save()
        profile_path = self.manifest.get("resource_profile")
        if not profile_path or not Path(profile_path).exists():
            raise WaitingForEvidence("All mask checks passed; waiting for measured resource calibration on the selected search fidelity.")
        profile = read_json(Path(profile_path))
        if not profile.get("calibrated") or not profile.get("calibration_evidence_ids"):
            raise WaitingForEvidence("A claimed resource setting without measured immutable calibration evidence cannot close preflight.")
        if profile.get("task_id", selected_task_id) != selected_task_id:
            raise WaitingForEvidence("The measured resource profile belongs to another fidelity; calibrate the selected numerical condition.")
        profile["task_id"] = selected_task_id
        accepted_checks = {fixture["validation_attempts"][-1]["trial_id"] for fixture in self.manifest["fixtures"]}
        peaks = {}
        for sample in self.manifest.get("worker_memory_samples", []):
            identity = sample.get("trial_id")
            if identity in accepted_checks and sample.get("phase") == "validation" and sample.get("peak_rss_bytes", 0) > peaks.get(identity, {}).get("peak_rss_bytes", 0):
                peaks[identity] = sample
        profile["validation_memory_samples"] = list(peaks.values())
        outcome = self.commands.execute("accept_preflight", "study.race.decide", {
            "race_id": self.manifest["race_id"], "action": "accept_preflight",
            "evidence_trial_ids": [fixture["validation_attempts"][-1]["trial_id"] for fixture in self.manifest["fixtures"]],
            "profile": profile, "rationale": "All nine frozen actual 2D pilot masks passed complete mean/TE/TM order, energy "
                "and repeat checks; measured resource calibration supplies safe concurrency and thread settings."})
        self.manifest.update(status="completed", acceptance=outcome, finished_at=time.time())
        self.save()

    def _recover_measurements(self):
        for fixture in self.manifest["fixtures"]:
            if fixture["phase"] != "measurement_incomplete":
                continue
            trial = self.records.get(fixture["measurement_trial_id"], "trial")
            if trial["status"] not in {"interrupted", "paused", "budget_exhausted"}:
                raise WaitingForEvidence(f"The recorded source measurement {trial['id']} failed before usable evidence. "
                                         "Remaining fixture checks have run; this failure needs code or evaluator repair.")
            if fixture.get("measurement_recoveries", 0) >= 2:
                raise WaitingForEvidence(f"Source measurement {trial['id']} exhausted its two bounded recovery allocations.")
            action = "extend" if trial["status"] == "budget_exhausted" else "resume"
            payload = {"trial_id": trial["id"], "action": action,
                       "expected_control_revision": trial["control_revision"],
                       "rationale": "Recover the same actual-mask measurement from its saved state; retain cumulative cost."}
            if action == "extend":
                payload["wall_seconds"] = min(600, trial["wall_seconds"] + 180)
            self._command(fixture, "trial.control", "measurement_recovery_" + str(trial["control_revision"]), payload)
            fixture["measurement_recoveries"] = fixture.get("measurement_recoveries", 0) + 1
            fixture["phase"] = "measurement_wait"
            self.save()
            return

    def _escalate_numerical(self):
        # An interrupted or time-exhausted job should recover, not consume a new
        # independent higher-fidelity attempt merely because the service restarted.
        for fixture in self.manifest["fixtures"]:
            if fixture["phase"] != "numerical_followup":
                continue
            trial = self.records.get(fixture["validation_attempts"][-1]["trial_id"], "trial")
            if trial["status"] in {"interrupted", "paused", "budget_exhausted"} and not (trial.get("result") or {}).get("scientific_complete"):
                recoveries = fixture.get("validation_recoveries", 0)
                if recoveries < 2:
                    payload = {"trial_id": trial["id"], "action": "extend" if trial["status"] == "budget_exhausted" else "resume",
                        "expected_control_revision": trial["control_revision"],
                        "rationale": "Complete the same declared numerical recipe after interruption; preserve observations and cumulative costs."}
                    if payload["action"] == "extend":
                        if trial["wall_seconds"] >= 900:
                            continue
                        payload["wall_seconds"] = min(900, trial["wall_seconds"] + 300)
                    self._command(fixture, "trial.control", "validation_recovery_" + str(trial["control_revision"]), payload)
                    fixture["validation_recoveries"] = recoveries + 1
                    fixture["phase"] = "validation_wait"
                    self.save()
                    return True
        failures = [self.records.get(fixture["validation_attempts"][-1]["trial_id"], "trial")
                    for fixture in self.manifest["fixtures"] if fixture["phase"] == "numerical_followup"]
        if any(not truncation_failure(trial) for trial in failures):
            return self._numerical_hold("solver_consistency", "A completed check failed energy, repeat consistency, "
                "or physical validity, or remained incomplete after bounded recovery. Increasing Fourier order is "
                "not an established remedy; failed evidence is preserved for diagnosis.")
        current = self.manifest.get("fidelities", DEFAULT_FIDELITIES)[-1]
        higher = next((fidelity for fidelity in HIGHER_FIDELITIES
                       if harmonic_count(fidelity) > harmonic_count(current)), None)
        if higher is None:
            return self._numerical_hold("ladder_exhausted", "All declared higher 2D truncations were tested; "
                "the actual masks still do not establish convergence. Algorithm ranking remains gated.")
        race = self.records.get(self.manifest["race_id"], "adaptive_race")
        prediction = predict_validation_memory(higher, self.manifest.get("worker_memory_samples", []),
            race.get("profile", {}).get("validation_peak_bytes", 5 * 1024**3))
        self.manifest.setdefault("memory_predictions", []).append(prediction)
        available = prediction["available_memory_bytes"]
        if available is None or available - prediction["predicted_peak_bytes"] < MEMORY_HEADROOM_BYTES:
            return self._numerical_hold("memory_headroom", f"The next common fidelity {higher} needs a predicted "
                f"{prediction['predicted_peak_bytes'] / 1024**3:.2f} GiB plus 4 GiB of headroom, while "
                f"{(available or 0) / 1024**3:.2f} GiB is available. No higher-order job was allocated.")
        durations = [attempt.get("result", {}).get("worker_seconds") for fixture in self.manifest["fixtures"]
                     for attempt in fixture["validation_attempts"][-1:]]
        duration = max((value for value in durations if type(value) in (int, float) and value > 0), default=600)
        predicted_seconds = len(self.manifest["fixtures"]) * duration * (harmonic_count(higher) / harmonic_count(current)) ** 3 + 60
        prediction["predicted_round_seconds"] = predicted_seconds
        prediction["remaining_elapsed_seconds"] = max(0, race["deadline_at"] - time.time())
        if prediction["remaining_elapsed_seconds"] < predicted_seconds:
            return self._numerical_hold("elapsed_envelope", "The next numerical round does not fit the remaining "
                "original elapsed-time envelope. Saved evidence remains available and the deadline is unchanged.")
        # Preserve every preceding immutable check. The new round measures the
        # original search fidelity and only the final pair that needs assessment.
        original = self.records.get(self.manifest["task_id"], "task")["problem"]["fidelity"]
        self.manifest["fidelities"] = [deepcopy(original), deepcopy(current), deepcopy(higher)]
        self.manifest.setdefault("fidelity_ladder_history", deepcopy(DEFAULT_FIDELITIES)).append(deepcopy(higher))
        for fixture in self.manifest["fixtures"]:
            fixture["phase"] = "validation_create"
        self.event("fidelity_escalation", f"Actual truncation-only disagreement requires a common {higher} "
                   "check for all nine masks; memory and elapsed admission predictions passed.")
        return True

    def _numerical_hold(self, category, reason):
        self.manifest.update(status="numerically_unresolved", numerical_status="unresolved", hold_category=category,
                             waiting_reason=reason)
        self.save()
        outcome = self.commands.execute("numerical_hold_" + category, "study.race.control", {
            "race_id": self.manifest["race_id"], "action": "pause"})
        self.manifest["pause_outcome"] = outcome
        self.save()
        return False

    def _higher_fidelity_task(self):
        if self.manifest.get("higher_fidelity_task_id"):
            return self.manifest["higher_fidelity_task_id"]
        original = self.records.get(self.manifest["task_id"], "task")
        final = self.manifest.get("fidelities", DEFAULT_FIDELITIES)[-1]
        name = original["name"] + f" — common RCWA ({final['rcwa_order_x']},{final['rcwa_order_y']})"
        task = {"name": name, "problem_id": original["problem"]["definition_id"],
                "configuration": original["problem"]["configuration"], "fidelity": final,
                "split": original.get("split", "development")}
        preserved = {"name": original["name"], "problem_id": original["problem"]["definition_id"],
                     "configuration": original["problem"]["configuration"], "fidelity": original["problem"]["fidelity"],
                     "split": original.get("split", "development")}
        self.commands.execute("common_fidelity_task", "campaign.update", {
            "tasks": [preserved, task], "rationale": "Measured actual-mask differences exceed 0.5 percentage points; "
                "create a common converged higher-fidelity condition and start new algorithm trials without pooling old scores."})
        tasks = self.records._query("SELECT data FROM records WHERE kind='task' AND campaign_id=? "
                                    "AND json_extract(data,'$.name')=? AND json_extract(data,'$.archived')=0",
                                    (self.manifest["campaign_id"], name))
        if len(tasks) != 1:
            raise WaitingForEvidence("The audited common-fidelity task update has not produced exactly one matching task.")
        self.manifest["higher_fidelity_task_id"] = json.loads(tasks[0][0])["id"]
        self.save()
        return self.manifest["higher_fidelity_task_id"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--workspace-url", default="http://127.0.0.1:8765")
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--race-id")
    parser.add_argument("--pilot-results", type=Path, required=True)
    parser.add_argument("--resource-profile", type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=1)
    arguments = parser.parse_args()
    arguments.directory.mkdir(parents=True, exist_ok=True)
    with (arguments.directory / ".operator.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        records = ReadOnlyRecords(arguments.database)
        commands = DurableCommands(arguments.workspace_url, arguments.campaign_id,
            arguments.directory / "command-receipts", records, namespace=str(arguments.directory.resolve()))
        runner = PreflightRunner(arguments.directory, records, commands, campaign_id=arguments.campaign_id,
            task_id=arguments.task_id, pilot_results=arguments.pilot_results, race_id=arguments.race_id,
            resource_profile=arguments.resource_profile)
        try:
            while True:
                try:
                    if not runner.step(prepare_only=arguments.prepare_only):
                        break
                except (WaitingForEvidence, httpx.TransportError, CommandRejected) as exc:
                    runner.manifest["waiting_reason"] = str(exc)
                    runner.save()
                    time.sleep(max(.1, min(arguments.poll_seconds, 60)))
        finally:
            commands.close()
        print(json.dumps({"status": runner.manifest["status"], "manifest": str(runner.path),
                          "preflight_source_asset_ids": [fixture.get("asset_id") for fixture in runner.manifest["fixtures"]]}, indent=2))
        return 0 if runner.manifest["status"] in {"prepared", "completed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
