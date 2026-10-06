"""Durable adaptive allocation above the ordinary workspace scheduler.

The race envelope is not an ExecutionGrant: ordinary jobs already reserve their
allocation in the campaign ledger. Race spending is the *new* worker-time delta,
so continuing historical checkpoints never charges their old cost twice.
"""
from __future__ import annotations

from collections import defaultdict
import json
import math
from pathlib import Path
import time

import numpy as np

from optimization_framework.contracts.base import content_hash
from optimization_framework.contracts.racing import RaceCreateInput, RaceControlInput, RaceDecisionInput
from optimization_framework.contracts.requests import ControlInput, RecipeInput, StudyInput, TrialInput
from optimization_framework.execution.resources import ACTIVE
from optimization_framework.storage.sqlite import atomic_json, identifier, now


TERMINAL = {"completed", "budget_exhausted", "paused", "interrupted", "stopped", "failed"}
PREFERRED = ("motif_surgery", "flrl_ppo_residual", "flrl_autograd_adam", "flrl_lsf_random")
FINAL_VALIDATION_LIMIT = {"rcwa_order_x": 34, "rcwa_order_y": 17}


def memory_available_bytes():
    try:
        return int(next(line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines() if line.startswith("MemAvailable:"))) * 1024
    except (OSError, StopIteration, ValueError):
        return None


def harmonics(fidelity):
    return (2 * fidelity["rcwa_order_x"] + 1) * (2 * fidelity["rcwa_order_y"] + 1)


def activity_gate(algorithm, config, diagnostics):
    """Missing evidence remains unassessed; it cannot justify elimination."""
    d, reasons = diagnostics or {}, []
    evaluations = int(d.get("evaluations", 0))
    if "ppo" in algorithm:
        if d.get("training_updates", 0) < 10:
            reasons.append("Fewer than ten policy updates")
        if d.get("completed_episodes", d.get("episodes", 0)) < 1 and d.get("decisions", 0) < config.get("episode_length", 128):
            reasons.append("No completed episode established")
    elif algorithm == "flrl_autograd_adam":
        if d.get("gradient_evaluations", 0) < 50:
            reasons.append("Fewer than fifty gradient steps")
        if d.get("decisions", 0) < config.get("reference_epochs", 500):
            reasons.append("No complete continuation cycle established")
    elif algorithm in {"flrl_lsf_es", "phenotype_de"}:
        cycles = d.get("population_cycles", max(0, evaluations - config.get("population_size", 16)) // config.get("population_size", 16))
        if cycles < 20:
            reasons.append("Fewer than twenty population-sized update cycles")
    elif algorithm == "nested_fourier":
        if evaluations < 100:
            reasons.append("Fewer than one hundred proposals")
        if not (d.get("stage_transition_verified") or d.get("stage", 0) > 0):
            reasons.append("Stage-transition mechanism has not been observed")
    return {"eligible": not reasons, "reasons": reasons}


def adaptive_assessment(configurations, endpoints, *, rung, previous_rung, margin=.02, plateau=.01):
    """Choose follow-up from complete equal-horizon evidence, not peak seeds."""
    groups = defaultdict(dict)
    previous = defaultdict(dict)
    for row in endpoints:
        if row.get("eligible"):
            if row["rung_seconds"] == rung:
                groups[row["configuration_id"]][row["seed"]] = row
            elif row["rung_seconds"] == previous_rung:
                previous[row["configuration_id"]][row["seed"]] = row
    means = {key: float(np.mean([r["score"] for r in rows.values()])) for key, rows in groups.items() if len(rows) >= 3}
    leaders = sorted(means, key=lambda key: (-means[key], key))[:3]
    result = {}
    best = max(means.values(), default=0)
    # Leave-one-block-out ranking detects a shortlist supported by one seed.
    all_seeds = set.intersection(*(set(groups[key]) for key in means)) if means else set()
    unstable = set()
    for seed in all_seeds:
        trimmed = {key: np.mean([row["score"] for s, row in groups[key].items() if s != seed]) for key in means}
        changed = set(sorted(trimmed, key=lambda key: (-trimmed[key], key))[:2]) ^ set(sorted(means, key=lambda key: (-means[key], key))[:2])
        unstable.update(changed)
    for configuration in configurations:
        key, rows = configuration["id"], groups[configuration["id"]]
        maturity = [r.get("maturity", {"eligible": False}) for r in rows.values()]
        mature = len(rows) >= 3 and all(item["eligible"] for item in maturity)
        gains = [row["score"] - previous[key][seed]["score"] for seed, row in rows.items() if seed in previous[key]]
        if len(rows) < 3:
            action, reason = "unassessed", "Three eligible seed endpoints are not yet available"
        elif key in unstable:
            action, reason = "replicate", "Removing one seed changes the finalist shortlist"
        elif not mature:
            action, reason = "extend", "Protect undertrained or unassessed activity before eliminating"
        elif key in leaders or sum(gain >= margin for gain in gains) >= 2:
            action, reason = "extend", "Leading performance or improvement in at least two seeds"
        elif len(gains) >= 3 and all(row["score"] < best - margin for row in rows.values()) and max(gains) < plateau:
            action, reason = "deprioritize", "All seeds trail by the practical margin and have plateaued at this horizon"
        else:
            action, reason = "competitive", "Remaining evidence does not justify elimination"
        result[key] = {"action": action, "reason": reason, "mean_score": means.get(key), "mature": mature}
    return result


def confirmation_statistics(scores, baseline, *, margin=.02, bootstrap_resamples=20000):
    """Seed-block bootstrap, Bonferroni simultaneous 95% intervals for 3 pairs."""
    keys = sorted(scores)
    if len(keys) != 3 or baseline not in scores or any(len(scores[key]) != 10 for key in keys):
        return {"status": "inconclusive", "reason": "The frozen three-method, ten-seed roster is incomplete"}
    values = np.array([scores[key] for key in keys], dtype=float)
    if not np.isfinite(values).all():
        return {"status": "inconclusive", "reason": "A confirmation endpoint is non-finite"}
    blocks = np.random.default_rng(51973).integers(0, 10, (bootstrap_resamples, 10))
    comparisons = []
    for i, left in enumerate(keys):
        for j in range(i + 1, len(keys)):
            right = keys[j]
            differences = values[i] - values[j]
            sampled = differences[blocks].mean(axis=1)
            low, high = np.quantile(sampled, [.05 / 6, 1 - .05 / 6])
            comparisons.append({"left": left, "right": right, "mean_difference": float(differences.mean()),
                "lower": float(low), "upper": float(high), "left_wins": int((differences > 0).sum()),
                "right_wins": int((differences < 0).sum())})
    promising = []
    for key in keys:
        if key == baseline:
            continue
        row = next(row for row in comparisons if {row["left"], row["right"]} == {key, baseline})
        lower = row["lower"] if row["left"] == key else -row["upper"]
        wins = row["left_wins"] if row["left"] == key else row["right_wins"]
        if lower > margin and wins >= 8:
            promising.append(key)
    winners = []
    for key in promising:
        other = next(item for item in keys if item not in {key, baseline})
        row = next(row for row in comparisons if {row["left"], row["right"]} == {key, other})
        lower = row["lower"] if row["left"] == key else -row["upper"]
        wins = row["left_wins"] if row["left"] == key else row["right_wins"]
        if lower > margin and wins >= 8:
            winners.append(key)
    return {"status": "complete", "bootstrap_resamples": bootstrap_resamples, "intervals": "Bonferroni-adjusted 95% familywise, three comparisons",
        "practical_margin": margin, "comparisons": comparisons, "confirmed_promising": promising,
        "method_summaries": {key: {"mean": float(np.mean(scores[key])), "sample_standard_deviation": float(np.std(scores[key], ddof=1))} for key in keys},
        "confirmed_winner": winners[0] if len(winners) == 1 else None,
        "classification": "superiority_established" if len(winners) == 1 else "competitive" if len(promising) == 2 else "inconclusive",
        "scope": "Fresh-seed empirical replication on the known physical condition; no unseen-instance claim"}


class AdaptiveRacing:
    def __init__(self, workspace):
        self.workspace, self.store = workspace, workspace.store

    def _protocol(self, race):
        return self.store.get(race["protocol_id"], "race_protocol")["definition"]

    @staticmethod
    def _effective_configuration(configuration, task):
        parameters = dict(configuration["algorithm_config"])
        for key in ("rcwa_order_x", "rcwa_order_y"):
            if key in parameters:
                parameters[key] = task["problem"]["fidelity"][key]
        return parameters

    def _save(self, race, event="race.progress"):
        race["revision"] += 1
        race["updated_at"] = now()
        return self.store.put("adaptive_race", race, event)

    def _decision(self, race, action, rationale, configuration_ids=(), **details):
        return self.store.put_immutable("race_decision", {"id": identifier("race_decision"), "campaign_id": race["campaign_id"],
            "race_id": race["id"], "action": action, "rationale": rationale, "configuration_ids": list(configuration_ids),
            "created_at": now(), **details}, "race.decided")

    def create(self, campaign_id, request, *, authority="researcher"):
        request = request if isinstance(request, RaceCreateInput) else RaceCreateInput(**request)
        with self.workspace.lock, self.store.transaction():
            task = self.store.get(request.task_id, "task")
            if task["campaign_id"] != campaign_id or task.get("archived") or task.get("split") == "test":
                raise ValueError("Adaptive development requires a current known-condition development task")
            if any(row["status"] not in {"completed", "stopped", "budget_exhausted"} for row in self.store.list("adaptive_race", campaign_id)):
                raise ValueError("This campaign already has an active adaptive race")
            for configuration in request.configurations:
                seeds = set()
                for trial_id in configuration.pilot_trial_ids:
                    trial = self.store.get(trial_id, "trial")
                    if trial["campaign_id"] != campaign_id or trial["task_id"] != request.task_id or trial["algorithm"] != configuration.algorithm or trial.get("algorithm_config", {}) != configuration.algorithm_config:
                        raise ValueError("A checkpoint must match its configuration and physical task exactly")
                    if trial.get("execution_grant_id") or trial.get("absolute_deadline") or trial.get("confirmation_protocol_hash"):
                        raise ValueError("A previously frozen execution cannot become an extendable race checkpoint")
                    if trial["seed"] in seeds or trial["seed"] not in request.development_seeds:
                        raise ValueError("Each configuration has one checkpoint per declared development seed")
                    seeds.add(trial["seed"])
            for trial_id in request.preflight_subject_trial_ids:
                trial = self.store.get(trial_id, "trial")
                if trial["campaign_id"] != campaign_id or trial["task_id"] != request.task_id or trial.get("recipe"):
                    raise ValueError("Numerical checks must name observed optimization designs on this task")
            for asset_id in request.preflight_source_asset_ids:
                asset = self.store.get(asset_id, "asset")
                if asset["campaign_id"] != campaign_id or asset.get("kind") != "solution" or asset.get("producer_id") not in request.preflight_subject_trial_ids:
                    raise ValueError("Preflight fixtures must be observed solution assets from the declared source trials")
            start, race_id = time.time(), identifier("race")
            campaign = self.store.get(campaign_id, "campaign")
            definition = request.model_dump(mode="json")
            protocol = self.store.put_immutable("race_protocol", {"id": "race_protocol_" + content_hash([race_id, definition]),
                "campaign_id": campaign_id, "race_id": race_id, "definition": definition, "problem_digest": content_hash(task["problem"]),
                "authority": authority, "created_at": now()}, "race.protocol_frozen")
            race = {"id": race_id, "campaign_id": campaign_id, "study_id": campaign.get("active_study_id"), "task_id": request.task_id,
                "protocol_id": protocol["id"], "status": "preflight", "stage": "numerical_checks", "revision": 1,
                "created_at": now(), "updated_at": now(), "started_at": start, "deadline_at": start + request.total_seconds,
                "batch_deadline_at": min(start + request.batch_seconds, start + request.total_seconds), "batch_index": 0,
                "profile": {"max_workers": min(2, request.max_workers), "threads": 1, "memory_headroom_bytes": 4 * 1024**3},
                "preflight": {"status": "pending", "required_subject_trial_ids": request.preflight_subject_trial_ids},
                "configurations": {c.id: {"status": "active"} for c in request.configurations}, "cells": [], "confirmation": {},
                "worker_seconds_spent": 0., "report_revision": 0}
            for configuration in request.configurations:
                pilots = {self.store.get(key, "trial")["seed"]: key for key in configuration.pilot_trial_ids}
                for seed in request.development_seeds:
                    pilot = self._summary(pilots[seed]) if seed in pilots else {}
                    race["cells"].append({"id": identifier("race_cell"), "configuration_id": configuration.id, "seed": seed,
                        "phase": "development", "rung_seconds": request.rungs_seconds[0], "trial_id": pilots.get(seed),
                        "initial_execution_seconds": pilot.get("elapsed_seconds", 0.), "status": "pending"})
            self.store.put("adaptive_race", race, "race.created")
            self._decision(race, "created", request.rationale, authority=authority)
            self._lease(race)
            return self.view(race_id)

    def _lease(self, race):
        return self.store.put_immutable("race_execution_lease", {"id": f"race_lease_{race['id']}_{race['batch_index']}",
            "campaign_id": race["campaign_id"], "race_id": race["id"], "batch_index": race["batch_index"],
            "deadline_at": race["batch_deadline_at"], "global_deadline_at": race["deadline_at"], "created_at": now()}, "race.batch_authorized")

    def control(self, campaign_id, request, *, authority="researcher"):
        request = request if isinstance(request, RaceControlInput) else RaceControlInput(**request)
        with self.workspace.lock, self.store.transaction():
            race = self.store.get(request.race_id, "adaptive_race")
            self._belongs(race, campaign_id)
            if request.expected_revision is not None and request.expected_revision != race["revision"]:
                raise ValueError("The adaptive race revision changed; refresh before controlling it")
            if race["status"] in {"completed", "stopped", "budget_exhausted"}:
                raise ValueError("This adaptive race has ended")
            if request.action == "resume":
                if time.time() >= race["deadline_at"]:
                    raise ValueError("The original global elapsed deadline has passed")
                race["status"] = "running" if race["preflight"]["status"] == "passed" else "preflight"
            else:
                race["status"] = "paused" if request.action == "pause" else "stopped"
                self._halt(race, "pause" if request.action == "pause" else "stop", authority)
            controller = "Researcher" if authority == "researcher" else "The lead agent"
            self._decision(race, request.action, f"{controller} controlled the race; the original elapsed deadline remains fixed")
            self._save(race, "race.controlled")
            return self.view(race["id"])

    @staticmethod
    def _belongs(race, campaign_id):
        if race["campaign_id"] != campaign_id:
            raise ValueError("Adaptive race belongs to another campaign")

    def decide(self, campaign_id, request, *, authority="researcher"):
        request = request if isinstance(request, RaceDecisionInput) else RaceDecisionInput(**request)
        with self.workspace.lock, self.store.transaction():
            race = self.store.get(request.race_id, "adaptive_race")
            self._belongs(race, campaign_id)
            protocol = self._protocol(race)
            if time.time() >= race["deadline_at"] or race["status"] in {"completed", "stopped", "budget_exhausted"}:
                raise ValueError("The race execution envelope is closed")
            if set(request.configuration_ids) - set(race["configurations"]):
                raise ValueError("Choose configuration IDs from the frozen race roster")
            if request.action == "register_calibration":
                if race["preflight"]["status"] != "pending":
                    raise ValueError("Resource calibration must finish before optimizer development")
                workers = request.profile.get("max_workers", 1)
                threads = request.profile.get("numerical_threads", request.profile.get("threads", 1))
                if type(workers) is not int or not 1 <= workers <= protocol["max_workers"] or threads not in {1, 2, 4}:
                    raise ValueError("Calibration profiles are bounded by four workers and one, two, or four threads")
                race["profile"].update(request.profile)
                race["profile"].update(threads=threads, max_workers=workers)
                selected_task = self.store.get(request.profile.get("task_id", race["task_id"]), "task")
                original_task = self.store.get(protocol["task_id"], "task")
                if selected_task["campaign_id"] != campaign_id or selected_task["problem"]["configuration"] != original_task["problem"]["configuration"]:
                    raise ValueError("Resource calibration must preserve the race's physical problem")
                for trial_id in request.evidence_trial_ids:
                    trial = self.store.get(trial_id, "trial")
                    if trial["campaign_id"] != campaign_id or trial["task_id"] != selected_task["id"] or trial.get("race_phase") != "calibration":
                        raise ValueError("Register campaign-owned calibration trials on this physical task")
                    configuration = next((item for item in protocol["configurations"] if item["algorithm"] == trial["algorithm"] and self._effective_configuration(item, selected_task) == trial.get("algorithm_config", {})), None)
                    if not configuration or trial.get("numerical_threads", 1) != threads or trial["wall_seconds"] > 120:
                        raise ValueError("Resource probes require an approved configuration, matching threads, and at most 120 seconds")
                    if any(cell.get("trial_id") == trial_id for cell in race["cells"]):
                        continue
                    cell = {"id": identifier("race_cell"), "configuration_id": configuration["id"], "seed": trial["seed"],
                        "phase": "calibration", "trial_id": trial_id, "rung_seconds": trial["wall_seconds"],
                        "initial_execution_seconds": 0., "status": "allocated"}
                    race["cells"].append(cell)
                    self._tag(trial, race, cell)
            elif request.action == "record_calibration":
                samples = request.profile.get("samples", [])
                if len(samples) < 3 or any(type(sample.get("available_memory_bytes")) not in {int, float} or sample["available_memory_bytes"] < 4 * 1024**3 for sample in samples):
                    raise ValueError("Safe calibration requires at least three memory samples retaining four GiB of available RAM")
                summaries = []
                for trial_id in request.evidence_trial_ids:
                    if not any(cell.get("trial_id") == trial_id and cell["phase"] == "calibration" for cell in race["cells"]):
                        raise ValueError("Resource calibration must refer to registered measured probes")
                    summary = self._summary(trial_id)
                    if summary["status"] in ACTIVE or summary["status"] == "failed" or summary["diagnostics"].get("evaluations", 0) < 1:
                        raise ValueError("Every resource probe must finish with actual numerical work")
                    summaries.append({"trial_id": trial_id, "algorithm": summary["algorithm"], "elapsed_seconds": summary["elapsed_seconds"], "diagnostics": summary["diagnostics"]})
                if not summaries or not any(row["diagnostics"].get("gradient_evaluations", 0) > 0 for row in summaries) or not any(row["algorithm"] not in {"flrl_autograd_adam", "flrl_ppo_residual"} for row in summaries):
                    raise ValueError("Measure both gradient and forward-only workloads before selecting a safe profile")
                record = self.store.put_immutable("race_resource_calibration", {"id": "race_calibration_" + content_hash([race["id"], request.evidence_trial_ids, request.profile]),
                    "campaign_id": campaign_id, "race_id": race["id"], "complete": True, "safe": True,
                    "trial_ids": request.evidence_trial_ids, "measurements": request.profile, "executions": summaries, "created_at": now()}, "race.resources_measured")
                self._decision(race, "resource_evidence", request.rationale, calibration_evidence_id=record["id"])
            elif request.action == "register_preflight":
                if race["preflight"]["status"] != "pending":
                    raise ValueError("Numerical preflight fixtures are already closed")
                if "validation_peak_bytes" in request.profile:
                    peak = request.profile["validation_peak_bytes"]
                    corrected = False
                    references = request.profile.get("memory_reference_evidence_trial_ids", [])
                    if authority == "researcher" and len(set(references)) >= 2:
                        target = self.store.get(request.evidence_trial_ids[0], "trial")
                        fidelity = target.get("recipe", {}).get("parameters", {}).get("fidelities", [{}])[-1]
                        corrected = all(any(cell.get("trial_id") == key and cell["phase"] == "preflight" for cell in race["cells"])
                            and (self.store.get(key, "trial").get("result") or {}).get("scientific_complete")
                            and (self.store.get(key, "trial").get("result") or {}).get("recipe_result", {}).get("verdict") == "passed"
                            and self.store.get(key, "trial").get("recipe", {}).get("parameters", {}).get("fidelities", [{}])[-1] == fidelity
                            for key in references)
                    if type(peak) not in {int, float} or peak < 5 * 1024**3 or (peak < race["profile"].get("validation_peak_bytes", 5 * 1024**3) and not corrected):
                        raise ValueError("Higher-order checks may only increase their conservative memory prediction")
                    race["profile"]["validation_peak_bytes"] = peak
                for trial_id in request.evidence_trial_ids:
                    trial = self.store.get(trial_id, "trial")
                    if trial["campaign_id"] != campaign_id or trial["task_id"] != race["task_id"]:
                        raise ValueError("Preflight executions must belong to this physical task")
                    if any(cell.get("trial_id") == trial_id for cell in race["cells"]):
                        continue
                    parent = trial.get("parent_trial_id")
                    if parent:
                        if not any(cell.get("trial_id") == parent and cell["phase"] == "preflight" for cell in race["cells"]):
                            raise ValueError("Register the frozen-fixture source before its validation execution")
                    elif trial.get("algorithm") != "evaluate_asset" or not set(trial.get("initial_assets", [])) & set(protocol["preflight_source_asset_ids"]):
                        raise ValueError("Register an actual reevaluation of a frozen preflight fixture")
                    cell = {"id": identifier("race_cell"), "configuration_id": "preflight", "seed": trial["seed"], "phase": "preflight",
                        "trial_id": trial_id, "rung_seconds": trial["wall_seconds"], "initial_execution_seconds": 0., "status": "allocated"}
                    race["cells"].append(cell)
                    self._tag(trial, race, cell)
            elif request.action == "accept_preflight":
                required = set(protocol["preflight_source_asset_ids"] or protocol["preflight_subject_trial_ids"])
                subjects = set()
                findings = []
                for trial_id in request.evidence_trial_ids:
                    trial = self.store.get(trial_id, "trial")
                    result = trial.get("result") or trial.get("progress") or {}
                    summary = result.get("recipe_result", {})
                    if trial["campaign_id"] != campaign_id or trial["status"] in ACTIVE or trial["status"] == "failed" or not result.get("scientific_complete"):
                        raise ValueError("Numerical preflight requires completed actual validation executions")
                    if summary.get("recipe_id") != protocol["numerical_recipe_id"] or not summary.get("complete") or summary.get("verdict") != "passed":
                        raise ValueError("Numerical preflight did not establish the declared 2D convergence and energy checks")
                    if not summary.get("subjects") or any(not item.get("complete") or item.get("verdict") != "passed" for item in summary["subjects"]):
                        raise ValueError("Every declared actual-mask numerical subject must pass")
                    findings.extend(summary["subjects"])
                    parent_id = trial.get("parent_trial_id")
                    if protocol["preflight_source_asset_ids"]:
                        parent = self.store.get(parent_id, "trial")
                        if not any(cell.get("trial_id") == parent_id and cell["phase"] == "preflight" for cell in race["cells"]):
                            raise ValueError("The numerical evidence source is outside the registered fixed-fixture roster")
                        subjects.update(set(parent.get("initial_assets", [])) & required)
                    else:
                        subjects.add(parent_id)
                if subjects != required:
                    raise ValueError("Supply passing numerical evidence for every declared preflight mask")
                original_task = self.store.get(protocol["task_id"], "task")
                original_fidelity = original_task["problem"]["fidelity"]
                changed = []
                for finding in findings:
                    observations = finding.get("observations", [])
                    lowest = next((row for row in observations if row.get("fidelity") == original_fidelity), None)
                    highest = next((row for row in observations if row.get("fidelity") == finding.get("highest_fidelity")), None)
                    if lowest is None or highest is None:
                        raise ValueError("Preflight must measure the current search fidelity and converged highest fidelity")
                    changed.append(max(abs(highest["objectives"][key] - lowest["objectives"][key]) for key in
                        ("mean_plus1_transmission", "te_plus1_transmission", "tm_plus1_transmission")) > .005)
                if any(changed):
                    fidelities = [finding["highest_fidelity"] for finding in findings]
                    if any(fidelity != fidelities[0] for fidelity in fidelities):
                        raise ValueError("Reevaluate every fixture at one common converged highest fidelity")
                    task_id = request.profile.get("task_id")
                    if not task_id:
                        raise ValueError("Measured fidelity differences require a new common higher-fidelity task before search")
                    task = self.store.get(task_id, "task")
                    if task["campaign_id"] != campaign_id or task.get("archived") or task.get("split") == "test" or task["problem"]["configuration"] != original_task["problem"]["configuration"] or task["problem"]["fidelity"] != fidelities[0]:
                        raise ValueError("The replacement task must preserve physics and use the measured common converged fidelity")
                    amendment = {"id": "race_fidelity_" + race["id"], "campaign_id": campaign_id, "race_id": race["id"],
                        "original_task_id": race["task_id"], "task_id": task_id, "fidelity": fidelities[0],
                        "resolved_configurations": {item["id"]: self._effective_configuration(item, task) for item in protocol["configurations"]},
                        "evidence_trial_ids": request.evidence_trial_ids, "created_at": now()}
                    self.store.put_immutable("race_fidelity_amendment", amendment, "race.fidelity_amended")
                    race["task_id"] = task_id
                    race["study_id"] = self.store.get(campaign_id, "campaign").get("active_study_id")
                    for cell in race["cells"]:
                        if cell["phase"] == "development":
                            cell.update(trial_id=None, initial_execution_seconds=0.)
                workers = request.profile.get("max_workers", 2)
                threads = request.profile.get("numerical_threads", request.profile.get("threads", 1))
                if type(workers) is not int or not 1 <= workers <= protocol["max_workers"] or threads not in {1, 2, 4}:
                    raise ValueError("Resource calibration must choose one through four workers and one, two, or four numerical threads")
                if request.profile.get("memory_headroom_bytes", 4 * 1024**3) < 4 * 1024**3:
                    raise ValueError("Keep at least four GiB of available-memory headroom")
                evidence = request.profile.get("calibration_evidence_ids", [])
                if not request.profile.get("calibrated") or not evidence:
                    raise ValueError("Supply measured resource calibration evidence before choosing parallelism")
                matching_profile = False
                for evidence_id in evidence:
                    record = self.store.get_entry(evidence_id)
                    data = record["data"]
                    if record["kind"] != "race_resource_calibration" or data.get("campaign_id") != campaign_id or data.get("race_id") != race["id"] or not data.get("complete") or not data.get("safe"):
                        raise ValueError("Supply completed safe resource calibration evidence from this race")
                    measured = data["measurements"]
                    matching_profile |= measured.get("max_workers") == workers and measured.get("numerical_threads", measured.get("threads", 1)) == threads and measured.get("task_id", protocol["task_id"]) == race["task_id"]
                if not matching_profile:
                    raise ValueError("The selected resource profile must have measured matching worker and thread counts")
                race["profile"] = {**race["profile"], **request.profile, "threads": threads}
                if threads != 1:
                    for cell in race["cells"]:
                        if cell["phase"] == "development" and cell.get("trial_id"):
                            cell.update(trial_id=None, initial_execution_seconds=0.)
                race["preflight"] = {"status": "passed", "evidence_trial_ids": request.evidence_trial_ids,
                    "required_subject_trial_ids": sorted(required), "accepted_at": now(),
                    "highest_fidelities": [finding["highest_fidelity"] for finding in findings]}
                self._final_validation_plan(race)
                race.update(status="running", stage="development")
            elif request.action == "freeze_confirmation":
                self._freeze_confirmation(race, request.configuration_ids)
            elif race["preflight"]["status"] != "passed":
                raise ValueError("Numerical preflight must pass before allocating optimizer work")
            else:
                if race["confirmation"]:
                    raise ValueError("The confirmation roster and allocations are frozen")
                for key in request.configuration_ids:
                    if request.action == "hold":
                        race["configurations"][key]["status"] = "held"
                    elif request.action == "extend":
                        race["configurations"][key]["status"] = "active"
                        self._extend_configuration(race, key)
                    elif request.action == "replicate":
                        self._replicate(race, key)
            self._decision(race, request.action, request.rationale, request.configuration_ids, authority=authority,
                evidence_trial_ids=request.evidence_trial_ids, profile=request.profile)
            self._save(race, "race.decision_applied")
            return self.view(race["id"])

    def _summary(self, trial_id):
        # SQLite projects scalars/diagnostics before decoding; masks never enter
        # a polling response or every scheduling iteration.
        expressions = {"status": "$.status", "seed": "$.seed", "algorithm": "$.algorithm", "wall_seconds": "$.wall_seconds",
            "execution_seconds": "$.execution_seconds", "progress_elapsed": "$.progress.elapsed_seconds", "result_elapsed": "$.result.elapsed_seconds",
            "diagnostics": "$.progress.diagnostics", "result_diagnostics": "$.result.diagnostics", "best_objective": "$.progress.best_objective",
            "checkpoint_available": "$.progress.checkpoint_available", "resume_supported": "$.progress.resume_supported",
            "confirmation_method_id": "$.confirmation_method_id", "unknown_worker_cost": "$.progress.unknown_worker_cost",
            "unknown_solver_cost": "$.progress.unknown_solver_cost"}
        with self.store.connection() as db:
            row = db.execute("SELECT " + ",".join("json_extract(data,?) AS " + key for key in expressions) + " FROM records WHERE id=? AND kind='trial'",
                [*expressions.values(), trial_id]).fetchone()
        if row is None:
            raise KeyError(trial_id)
        result = dict(row)
        result["elapsed_seconds"] = max(result.get(key) or 0 for key in ("execution_seconds", "progress_elapsed", "result_elapsed"))
        diagnostics = result.pop("result_diagnostics") or result.get("diagnostics")
        result["diagnostics"] = json.loads(diagnostics) if isinstance(diagnostics, str) else diagnostics or {}
        return result

    def _endpoints(self, race):
        return [row for row in self.store.list("race_endpoint", race["campaign_id"]) if row["race_id"] == race["id"]]

    def _endpoint(self, race, cell, summary):
        identity = f"race_endpoint_{cell['id']}_{cell['rung_seconds']}"
        try:
            return self.store.get(identity, "race_endpoint")
        except KeyError:
            pass
        fields = ("elapsed_seconds", "best_objective", "confirmed_observations", "unknown_worker_cost", "unknown_solver_cost")
        rows = self.workspace._metric_projections.read(self.workspace.job_dir(cell["trial_id"]) / "metrics.jsonl", fields)
        observed = [row for row in rows if type(row.get("best_objective")) in (int, float) and math.isfinite(row["best_objective"])
            and row.get("confirmed_observations", 0) > 0 and row.get("elapsed_seconds", float("inf")) <= cell["rung_seconds"]]
        endpoint = observed[-1] if observed else {}
        eligible = bool(endpoint) and summary["elapsed_seconds"] >= cell["rung_seconds"] and summary["status"] not in {"failed", "interrupted", "stopped"}
        eligible &= not endpoint.get("unknown_worker_cost", False) and not endpoint.get("unknown_solver_cost", False)
        configuration = next(item for item in self._protocol(race)["configurations"] if item["id"] == cell["configuration_id"])
        observation = self._observation_for_score(cell["trial_id"], endpoint.get("best_objective")) if endpoint else None
        objectives = observation.get("objectives", {}) if observation else {}
        eligible &= observation is not None and all(name in objectives for name in
            ("mean_plus1_transmission", "te_plus1_transmission", "tm_plus1_transmission", "min_plus1_transmission"))
        return self.store.put_immutable("race_endpoint", {"id": identity, "campaign_id": race["campaign_id"], "race_id": race["id"],
            "cell_id": cell["id"], "trial_id": cell["trial_id"], "configuration_id": cell["configuration_id"], "phase": cell["phase"],
            "seed": cell["seed"], "rung_seconds": cell["rung_seconds"], "observed_seconds": endpoint.get("elapsed_seconds"),
            "actual_worker_seconds": summary["elapsed_seconds"], "score": endpoint.get("best_objective"), "eligible": eligible,
            "objectives": objectives, "observation_id": observation.get("id") if observation else None,
            "maturity": activity_gate(configuration["algorithm"], configuration["algorithm_config"], summary["diagnostics"]),
            "diagnostics": summary["diagnostics"], "censored": not eligible, "created_at": now()}, "race.endpoint_recorded")

    def _spent(self, race):
        return sum(max(0., self._summary(cell["trial_id"])["elapsed_seconds"] - cell.get("initial_execution_seconds", 0.))
            for cell in race["cells"] if cell.get("trial_id"))

    def _tag(self, trial, race, cell):
        trial.update(race_id=race["id"], race_cell_id=cell["id"], race_phase=cell["phase"],
            race_global_deadline=race["deadline_at"])
        return self.store.put("trial", trial, "race.trial_bound")

    def _launch_cell(self, race, cell):
        protocol = self._protocol(race)
        configuration = next(item for item in protocol["configurations"] if item["id"] == cell["configuration_id"])
        task = self.store.get(race["task_id"], "task")
        if cell.get("trial_id"):
            trial = self.store.get(cell["trial_id"], "trial")
            self._tag(trial, race, cell)
            self.workspace.control(trial["id"], ControlInput(action="extend", wall_seconds=cell["rung_seconds"],
                max_steps=max(trial["max_steps"], protocol["max_steps"]), rationale="Adaptive protocol continuation; retain schedule and checkpoint state"))
        else:
            trial = self.workspace.create_trial(TrialInput(campaign_id=race["campaign_id"], task_id=race["task_id"],
                algorithm=configuration["algorithm"], algorithm_config=self._effective_configuration(configuration, task), hypothesis_id=configuration.get("hypothesis_id"),
                seed=cell["seed"], wall_seconds=cell["rung_seconds"], max_steps=protocol["max_steps"], schedule_steps=128,
                numerical_threads=race["profile"].get("threads", 1),
                recovery={"every_observations": 25, "every_seconds": 30}, question="Adaptive equal-worker-time development screening"))
            self._tag(trial, race, cell)
            cell["trial_id"] = trial["id"]
        cell["status"] = "allocated"

    def _extend_configuration(self, race, key):
        rungs = self._protocol(race)["rungs_seconds"]
        for cell in race["cells"]:
            if cell["phase"] == "development" and cell["configuration_id"] == key and cell["status"] == "finished" and cell["rung_seconds"] < rungs[-1]:
                cell.update(rung_seconds=rungs[rungs.index(cell["rung_seconds"]) + 1], status="pending")

    def _replicate(self, race, key):
        protocol = self._protocol(race)
        used = {cell["seed"] for cell in race["cells"] if cell["configuration_id"] == key and cell["phase"] == "development"}
        seed = next((seed for seed in protocol["additional_development_seeds"] if seed not in used), None)
        if seed is None:
            return False
        horizon = max(cell["rung_seconds"] for cell in race["cells"] if cell["configuration_id"] == key and cell["phase"] == "development")
        race["cells"].append({"id": identifier("race_cell"), "configuration_id": key, "seed": seed, "phase": "development",
            "rung_seconds": horizon, "trial_id": None, "initial_execution_seconds": 0., "status": "pending", "exploration": True})
        return True

    def _freeze_confirmation(self, race, finalists):
        if race["preflight"]["status"] != "passed" or race["confirmation"]:
            raise ValueError("Confirmation requires successful preflight and an unfrozen roster")
        protocol = self._protocol(race)
        baseline = protocol["baseline_configuration_id"]
        if len(finalists) != 2 or len(set(finalists)) != 2 or baseline in finalists:
            raise ValueError("Freeze two distinct finalists plus the declared random-search baseline")
        endpoints = self._endpoints(race)
        selected = [*finalists, baseline]
        prototypes = {}
        for key in selected:
            candidates = [cell for cell in race["cells"] if cell["configuration_id"] == key and cell["phase"] == "development" and cell["status"] == "finished"]
            if not candidates:
                raise ValueError("Each frozen method needs a completed development prototype")
            if key != baseline and len({row["seed"] for row in endpoints if row["configuration_id"] == key and row["rung_seconds"] == protocol["rungs_seconds"][-1] and row["eligible"]}) < 3:
                raise ValueError("Finalists require three eligible full-horizon development endpoints")
            prototypes[key] = self.store.get(candidates[0]["trial_id"], "trial")
        allocations = {trial["id"]: {"expected_control_revision": trial["control_revision"], "wall_seconds": protocol["rungs_seconds"][-1]}
            for trial in prototypes.values()}
        study = self.workspace.create_study(race["campaign_id"], StudyInput(goal="Frozen fresh-seed confirmation of the adaptive race finalists",
            scope="confirmation", task_ids=[race["task_id"]], confirmation_kind="seed_replication",
            prototype_trial_ids=[trial["id"] for trial in prototypes.values()], prototype_allocations=allocations,
            seeds=protocol["confirmation_seeds"], validation_policy={}, comparison={"cost_axis": "full_worker_seconds", "cost_view": "actual_expenditure"},
            assumptions=["Known physical condition; fresh optimizer seeds", "Primary endpoint is confirmed incumbent at the frozen worker-time horizon"]), authority="researcher")
        confirmation_id = study["confirmation"]["id"]
        frozen = self.store.get(confirmation_id, "confirmation_protocol")
        mapping = {method_id: key for method_id, trial_id in frozen["prototypes"].items() for key, trial in prototypes.items() if trial["id"] == trial_id}
        race["confirmation"] = {"protocol_id": confirmation_id, "study_id": study["id"], "finalists": finalists,
            "configuration_by_method": mapping, "seed_roster": protocol["confirmation_seeds"], "status": "frozen", "frozen_at": now()}
        self.store.put_immutable("race_confirmation_roster", {"id": "race_confirmation_" + race["id"], "campaign_id": race["campaign_id"],
            "race_id": race["id"], "confirmation": race["confirmation"], "created_at": now()}, "race.confirmation_frozen")
        # Existing ConfirmationService freezes source/runtime/configuration and
        # creates each method/seed cell atomically before any worker launches.
        scheduled = self.workspace.confirmations.schedule(self.workspace, confirmation_id, authority="researcher")
        for trial_id in [*scheduled["created_trial_ids"], *scheduled["existing_trial_ids"]]:
            trial = self.store.get(trial_id, "trial")
            cell = {"id": identifier("race_cell"), "configuration_id": mapping[trial["confirmation_method_id"]], "seed": trial["seed"],
                "phase": "confirmation", "rung_seconds": protocol["rungs_seconds"][-1], "trial_id": trial_id,
                "initial_execution_seconds": 0., "status": "allocated"}
            race["cells"].append(cell)
            self._tag(trial, race, cell)
        race.update(stage="confirmation", status="running")

    def guard_control(self, trial, command):
        if trial.get("race_phase") == "confirmation" and any(getattr(command, key, None) is not None and getattr(command, key) != trial[key]
            for key in ("wall_seconds", "max_steps")):
            raise ValueError("Adaptive race confirmation allocation is immutable")

    def admission(self, trial, running):
        if not trial.get("race_id"):
            return True
        race = self.store.get(trial["race_id"], "adaptive_race")
        if race["status"] not in {"preflight", "running"} or time.time() >= race["deadline_at"]:
            return False
        if not any(cell.get("trial_id") == trial["id"] for cell in race["cells"]):
            return False
        if trial.get("race_phase") not in {"preflight", "calibration", "validation"} and race["preflight"]["status"] != "passed":
            return False
        workers = race["profile"]["max_workers"]
        if len(running) >= workers:
            return False
        if trial.get("race_phase") in {"preflight", "validation"} and any(row.get("race_phase") in {"preflight", "validation"} for row in running):
            return False
        profile = race["profile"]
        available = memory_available_bytes()
        if available is None:
            return False
        prediction = trial.get("race_validation_peak_bytes", profile.get("validation_peak_bytes", 5 * 1024**3)) if trial.get("race_phase") in {"preflight", "validation"} else profile.get("worker_peak_bytes", 5 * 1024**3)
        def resident(row):
            try:
                return int(Path(f"/proc/{row['pid']}/statm").read_text().split()[1]) * 4096
            except (OSError, ValueError, KeyError, IndexError):
                return 0
        outstanding = sum(max(0, profile.get("worker_peak_bytes", prediction) - resident(row)) for row in running)
        return available - prediction - outstanding >= profile.get("memory_headroom_bytes", 4 * 1024**3)

    def _halt(self, race, action, authority, reason=None):
        for cell in race["cells"]:
            if cell.get("trial_id"):
                summary = self._summary(cell["trial_id"])
                if summary["status"] in {"queued", "running"}:
                    self.workspace.control(cell["trial_id"], ControlInput(action=action), authority=authority, reason=reason)

    def _adaptive(self, race):
        protocol = self._protocol(race)
        rungs = protocol["rungs_seconds"]
        cells = [cell for cell in race["cells"] if cell["phase"] == "development" and race["configurations"][cell["configuration_id"]]["status"] != "held"]
        if not cells or any(cell["status"] != "finished" for cell in cells):
            return
        if all(cell["rung_seconds"] == rungs[0] for cell in cells):
            for key in race["configurations"]:
                self._extend_configuration(race, key)
            self._decision(race, "breadth_continuation", "Every method receives the thirty-minute three-seed development allowance")
            return
        endpoints = self._endpoints(race)
        if race["stage"] == "development":
            assessment = adaptive_assessment(protocol["configurations"], endpoints, rung=rungs[1], previous_rung=rungs[0],
                margin=protocol["practical_margin"], plateau=protocol["plateau_margin"])
            leaders = sorted(assessment, key=lambda key: -(assessment[key]["mean_score"] or 0))[:3]
            for key, decision in assessment.items():
                if decision["action"] == "replicate":
                    self._replicate(race, key)
                if decision["action"] in {"extend", "replicate", "competitive"} and key != protocol["baseline_configuration_id"]:
                    self._extend_configuration(race, key)
                    if not decision["mature"] or decision["action"] == "replicate" or key not in leaders:
                        for cell in race["cells"]:
                            if cell["configuration_id"] == key and cell["phase"] == "development" and cell["status"] == "pending":
                                cell["exploration"] = True
                                cell["exploration_start_seconds"] = self._summary(cell["trial_id"])["elapsed_seconds"] if cell.get("trial_id") else 0
                elif decision["action"] == "deprioritize":
                    race["configurations"][key]["status"] = "deprioritized"
                self._decision(race, decision["action"], decision["reason"], [key], assessment=decision)
            followups = [cell for cell in race["cells"] if cell["phase"] == "development" and cell["status"] == "pending"]
            protected = sum(cell["rung_seconds"] - cell.get("exploration_start_seconds", rungs[1]) for cell in followups if cell.get("exploration"))
            total = sum(cell["rung_seconds"] - (self._summary(cell["trial_id"])["elapsed_seconds"] if cell.get("trial_id") else 0) for cell in followups)
            if total and protected / total < protocol["exploration_fraction"]:
                key = next((key for key in sorted(assessment, key=lambda key: -(assessment[key]["mean_score"] or 0))
                    if key not in leaders and not any(cell["configuration_id"] == key for cell in followups)), protocol["baseline_configuration_id"])
                race["configurations"][key]["status"] = "active"
                self._extend_configuration(race, key)
                for cell in race["cells"]:
                    if cell["configuration_id"] == key and cell["phase"] == "development" and cell["status"] == "pending":
                        cell["exploration"] = True
                        cell["exploration_start_seconds"] = self._summary(cell["trial_id"])["elapsed_seconds"] if cell.get("trial_id") else 0
                self._decision(race, "exploration_reserve", "Preserve at least twenty percent of follow-up compute for uncertain or deprioritized evidence", [key])
            race["stage"] = "adaptive_followup"
            return
        replicas = [cell for cell in cells if cell["seed"] in protocol["additional_development_seeds"] and cell["rung_seconds"] < rungs[-1]]
        if replicas:
            for cell in replicas:
                cell.update(rung_seconds=rungs[-1], status="pending")
            self._decision(race, "replica_continuation", "Compare extra development seeds at the same finalist-selection horizon")
            return
        scores = defaultdict(dict)
        for row in endpoints:
            if row["phase"] == "development" and row["rung_seconds"] == rungs[-1] and row["eligible"]:
                scores[row["configuration_id"]][row["seed"]] = row["score"]
        eligible = [key for key, values in scores.items() if len(values) >= 3 and key != protocol["baseline_configuration_id"]]
        finalists = sorted(eligible, key=lambda key: (-float(np.mean(list(scores[key].values()))), key))[:2]
        if len(finalists) == 2:
            self._decision(race, "finalist_selection", "Freeze the two strongest complete equal-horizon development means; preserve activity limitations", finalists)
            self._freeze_confirmation(race, finalists)
        else:
            race.update(status="paused", reason="Insufficient eligible full-horizon development evidence for two finalists")

    def _observation_for_score(self, trial_id, score):
        if score is None:
            return None
        path = self.workspace.job_dir(trial_id) / "observations.jsonl"
        if not path.exists():
            return None
        with path.open() as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                objective = row.get("objectives", {}).get("mean_plus1_transmission")
                if row.get("status") == "ok" and objective is not None and abs(objective - score) <= 1e-12:
                    return row
        return None

    def _validation_candidate(self, trial_id, score):
        observation = self._observation_for_score(trial_id, score)
        if observation:
            return observation["candidate"]
        raise ValueError("The fixed-horizon incumbent has no authoritative candidate observation")

    def _final_validation_plan(self, race):
        """Freeze a validation ladder above the actual search truncation.

        A fidelity amendment changes neither the frozen physical condition nor
        the optimizer's score/time endpoint. This separate plan makes the final
        device check follow the *amended* numerical condition.
        """
        identity = "race_validation_plan_" + race["id"]
        try:
            return self.store.get(identity, "race_validation_plan")
        except KeyError:
            pass
        protocol = self._protocol(race)
        task = self.store.get(race["task_id"], "task")
        current = dict(task["problem"]["fidelity"])
        accepted = race.get("preflight", {}).get("highest_fidelities", []) or [current]
        highest_accepted = max(accepted, key=harmonics)
        parameters = {"absolute_tolerance": .005, "energy_tolerance": .001, "repeat_tolerance": 1e-6, "repeats": 2,
            **protocol["numerical_parameters"]}
        declared = parameters.get("fidelities", [{"rcwa_order_x": x, "rcwa_order_y": y} for x, y in ((10, 5), (12, 6), (14, 7))])
        limit = FINAL_VALIDATION_LIMIT
        greater = lambda fidelity: all(fidelity[key] >= current[key] for key in current) and any(fidelity[key] > current[key] for key in current)
        if greater(declared[-1]) and all(declared[-1][key] >= highest_accepted[key] and declared[-1][key] <= limit[key] for key in current):
            ladder = [fidelity for fidelity in declared if all(fidelity[key] >= current[key] for key in current)]
            if not ladder or ladder[0] != current:
                ladder.insert(0, current)
        else:
            higher = highest_accepted if greater(highest_accepted) else {
                "rcwa_order_x": min(current["rcwa_order_x"] + 4, limit["rcwa_order_x"]),
                "rcwa_order_y": min(current["rcwa_order_y"] + 2, limit["rcwa_order_y"])}
            ladder = [current, higher] if greater(higher) and all(higher[key] <= limit[key] for key in limit) else []
        parameters["fidelities"] = ladder
        samples = [sample for sample in race["profile"].get("validation_memory_samples", [])
            if sample.get("phase") == "validation" and sample.get("completed_evaluations", 0) > 0
            and sample.get("peak_rss_bytes", 0) > 0 and sample.get("trial_id") in race["preflight"].get("evidence_trial_ids", [])
            and isinstance(sample.get("fidelity"), dict) and all(type(sample["fidelity"].get(key)) is int and sample["fidelity"][key] > 0 for key in current)]
        if samples:
            top = max(harmonics(sample["fidelity"]) for sample in samples)
            measured = [sample for sample in samples if harmonics(sample["fidelity"]) == top]
            prediction = math.ceil(max(2 * sample["peak_rss_bytes"] * (harmonics(ladder[-1]) / harmonics(sample["fidelity"])) ** 2
                for sample in measured)) if ladder else None
            basis = "Twice measured completed preflight worker VmHWM, scaled by squared harmonic-count ratio"
        else:
            reference = race["profile"].get("validation_peak_bytes", 5 * 1024**3)
            prediction = math.ceil(reference * (harmonics(ladder[-1]) / harmonics(highest_accepted)) ** 2) if ladder else None
            basis = "Conservative prior preflight admission ceiling scaled by squared harmonic-count ratio; measured RSS unavailable"
        prediction = max(5 * 1024**3, prediction) if prediction is not None else None
        plan = {"id": identity, "campaign_id": race["campaign_id"], "race_id": race["id"], "task_id": race["task_id"],
            "search_fidelity": current, "highest_accepted_preflight_fidelity": highest_accepted, "maximum_fidelity": limit,
            "parameters": parameters, "predicted_peak_bytes": prediction, "memory_headroom_bytes": 4 * 1024**3,
            "prediction_basis": basis, "measured_samples": len(samples), "evidence_trial_ids": race["preflight"].get("evidence_trial_ids", []),
            "status": "ready" if ladder else "higher_fidelity_unavailable", "created_at": now()}
        record = self.store.put_immutable("race_validation_plan", plan, "race.validation_plan_frozen")
        race["validation_plan_id"] = record["id"]
        race["numerical_parameters"] = parameters
        return record

    def _finish_confirmation(self, race):
        confirmation_cells = [cell for cell in race["cells"] if cell["phase"] == "confirmation"]
        if not confirmation_cells or any(cell["status"] != "finished" for cell in confirmation_cells):
            return
        protocol = self._protocol(race)
        endpoints = [row for row in self._endpoints(race) if row["phase"] == "confirmation"]
        groups = defaultdict(dict)
        for row in endpoints:
            if row["eligible"]:
                groups[row["configuration_id"]][row["seed"]] = row["score"]
        scores = {key: [rows[seed] for seed in protocol["confirmation_seeds"]] for key, rows in groups.items() if set(rows) == set(protocol["confirmation_seeds"])}
        race["confirmation"]["statistics"] = confirmation_statistics(scores, protocol["baseline_configuration_id"], margin=protocol["practical_margin"])
        race["confirmation"]["endpoint_summaries"] = {key: {name: float(np.mean([row["objectives"][name] for row in endpoints
            if row["eligible"] and row["configuration_id"] == key and name in row.get("objectives", {})]))
            for name in ("mean_plus1_transmission", "te_plus1_transmission", "tm_plus1_transmission", "min_plus1_transmission")
            if any(row["eligible"] and row["configuration_id"] == key and name in row.get("objectives", {}) for row in endpoints)} for key in groups}
        race["confirmation"]["status"] = "numerical_validation_pending"
        race["stage"] = "final_validation"
        self._queue_final_validation(race, endpoints)

    def _queue_final_validation(self, race, endpoints=None):
        protocol = self._protocol(race)
        plan = self._final_validation_plan(race)
        if plan["status"] != "ready":
            race["confirmation"].update(status="numerically_inconclusive", validation_hold="No permitted fidelity above the actual search truncation")
            race.update(status="completed", finished_at=now(), reason="Final higher-order validation is unassessed at the permitted fidelity ceiling")
            self._decision(race, "validation_censored", race["reason"], validation_plan_id=plan["id"])
            return
        available = memory_available_bytes()
        if available is None or available - plan["predicted_peak_bytes"] < plan["memory_headroom_bytes"]:
            reason = "Predicted higher-order validation memory would consume the required four GiB headroom"
            if race["confirmation"].get("validation_hold") != reason:
                self._decision(race, "validation_memory_hold", reason, validation_plan_id=plan["id"],
                    predicted_peak_bytes=plan["predicted_peak_bytes"], available_memory_bytes=available)
            race["confirmation"].update(status="numerical_validation_held", validation_hold=reason)
            return
        race["confirmation"].pop("validation_hold", None)
        race["confirmation"]["status"] = "numerical_validation_pending"
        endpoints = endpoints or [row for row in self._endpoints(race) if row["phase"] == "confirmation"]
        if not any(endpoint["eligible"] for endpoint in endpoints):
            race["confirmation"]["status"] = "numerically_inconclusive"
            race.update(status="completed", finished_at=now(), reason="The frozen confirmation roster produced no eligible time-budget endpoints")
            self._decision(race, "validation_censored", race["reason"])
            return
        existing = {cell.get("endpoint_id") for cell in race["cells"] if cell["phase"] == "validation"}
        for endpoint in endpoints:
            if not endpoint["eligible"] or endpoint["id"] in existing:
                continue
            parent = self.store.get(endpoint["trial_id"], "trial")
            candidate = self._validation_candidate(parent["id"], endpoint["score"])
            recipe = self.workspace.compile_recipe(parent, protocol["numerical_recipe_id"], plan["parameters"], [candidate])
            check = self.workspace._queue_recipe(parent, recipe, TrialInput(campaign_id=race["campaign_id"], task_id=race["task_id"],
                algorithm="recipe", seed=parent["seed"], max_steps=len(recipe["cases"]), wall_seconds=protocol["validation_wall_seconds"],
                question="Numerically check the confirmed incumbent at the frozen sixty-minute endpoint"), authority="researcher")
            cell = {"id": identifier("race_cell"), "configuration_id": endpoint["configuration_id"], "seed": endpoint["seed"],
                "phase": "validation", "trial_id": check["id"], "rung_seconds": protocol["validation_wall_seconds"],
                "initial_execution_seconds": 0., "status": "allocated", "endpoint_id": endpoint["id"]}
            race["cells"].append(cell)
            check["race_validation_peak_bytes"] = plan["predicted_peak_bytes"]
            self._tag(check, race, cell)
        self._decision(race, "final_validation", "Validate fixed-horizon candidates before releasing numerically stable performance claims")

    def _write_report(self, race):
        report = self.view(race["id"])
        directory = self.workspace.directory / "races" / race["id"]
        directory.mkdir(parents=True, exist_ok=True)
        atomic_json(directory / "status.json", report)
        rows = ["# Adaptive algorithm testing report", "", f"Status: **{race['status']}**; stage: **{race['stage']}**.", "",
            f"Elapsed: {report['elapsed_seconds'] / 3600:.2f} / {report['total_seconds'] / 3600:.0f} hours; new worker time: {report['worker_seconds_spent'] / 3600:.2f} hours.", "",
            "Scores are confirmed incumbents at cumulative worker-time checkpoints. Expected time exhaustion does not imply optimizer convergence.", "",
            "| Configuration | Status | Eligible endpoints | Mean ± sample SD | TE | TM | Minimum polarization | Activity |", "|---|---|---:|---:|---:|---:|---:|---|"]
        for configuration in report["configurations"]:
            score = configuration["mean_score"]
            objectives = configuration["objective_means"]
            polarizations = [f"{objectives[name]:.4f}" if name in objectives else "—" for name in ("te_plus1_transmission", "tm_plus1_transmission", "min_plus1_transmission")]
            score_text = f"{score:.4f} ± {configuration['sample_standard_deviation']:.4f}" if score is not None and configuration["sample_standard_deviation"] is not None else f"{score:.4f}" if score is not None else "—"
            rows.append(f"| {configuration['id']} | {configuration['status']} | {sum(r['eligible'] for r in configuration['endpoints'])} | {score_text} | {' | '.join(polarizations)} | {configuration['maturity']['eligible']} |")
        rows += ["", "## Allocation decisions", ""]
        for decision in report["decisions"]:
            rows.append(f"- {decision['created_at']}: {decision['action']} — {decision['rationale']}")
        if race["confirmation"].get("statistics"):
            rows += ["", "## Fresh-seed confirmation", "", "```json", json.dumps(race["confirmation"]["statistics"], indent=2), "```",
                "", "Statistical screening remains provisional until all endpoint numerical checks pass. Scope: the known paper condition and tested horizons."]
        (directory / "report.md").write_text("\n".join(rows) + "\n")

    def tick(self):
        with self.workspace.lock:
            for race in self.store.list("adaptive_race"):
                if race["status"] not in {"preflight", "running", "paused"}:
                    continue
                try:
                    with self.store.transaction():
                        self._tick(race)
                except (ValueError, KeyError, OSError) as exc:
                    race = self.store.get(race["id"], "adaptive_race")
                    if race.get("last_error") != str(exc):
                        race["last_error"] = str(exc)
                        self._decision(race, "allocation_blocked", str(exc))
                        self._save(race)

    def _tick(self, race):
        protocol = self._protocol(race)
        before = content_hash(race)
        if time.time() >= race["deadline_at"]:
            self._halt(race, "stop", "deadline", "Authorized global elapsed deadline reached")
            race.update(status="budget_exhausted", reason="Authorized global elapsed deadline reached")
            self._decision(race, "elapsed_deadline", race["reason"])
            self._save(race)
            self._write_report(race)
            return
        if time.time() >= race["batch_deadline_at"]:
            race["batch_index"] += 1
            race["batch_deadline_at"] = min(race["started_at"] + (race["batch_index"] + 1) * protocol["batch_seconds"], race["deadline_at"])
            self._lease(race)
            self._decision(race, "batch_checkpoint", "Saved batch evidence; continue within the already authorized global elapsed envelope")
        race["worker_seconds_spent"] = self._spent(race)
        if race["worker_seconds_spent"] >= protocol["worker_seconds"]:
            self._halt(race, "stop", "budget", "Authorized new worker-time envelope reached")
            race.update(status="budget_exhausted", reason="Authorized new worker-time envelope reached")
            self._decision(race, "worker_deadline", race["reason"])
        if race["status"] != "running":
            if content_hash(race) != before:
                self._save(race)
            if time.time() - race.get("report_updated_at", 0) >= 30:
                race["report_updated_at"] = time.time()
                self.store.put("adaptive_race", race)
                self._write_report(race)
            return
        for cell in race["cells"]:
            if cell["status"] != "allocated" or not cell.get("trial_id"):
                continue
            summary = self._summary(cell["trial_id"])
            if summary["status"] in TERMINAL:
                if summary["status"] == "paused":
                    # User pause/resume retains the fixed scientific allocation.
                    self.workspace.control(cell["trial_id"], ControlInput(action="resume"))
                    continue
                if cell["phase"] in {"development", "confirmation"}:
                    self._endpoint(race, cell, summary)
                cell["status"] = "finished"
        if race["stage"] in {"development", "adaptive_followup"}:
            self._adaptive(race)
        elif race["stage"] == "confirmation":
            self._finish_confirmation(race)
        elif race["stage"] == "final_validation":
            checks = [cell for cell in race["cells"] if cell["phase"] == "validation"]
            if not checks:
                self._queue_final_validation(race)
            if checks and all(cell["status"] == "finished" for cell in checks):
                passed = []
                for cell in checks:
                    trial = self.store.get(cell["trial_id"], "trial")
                    result = trial.get("result") or trial.get("progress") or {}
                    passed.append(bool(result.get("scientific_complete") and result.get("recipe_result", {}).get("verdict") == "passed"))
                complete = len(checks) == 30 and all(passed)
                race["confirmation"].update(status="validated" if complete else "numerically_inconclusive", numerical_checks_passed=sum(passed))
                race.update(status="completed", finished_at=now(), reason="Frozen roster and final numerical checks finished")
                self._decision(race, "complete", race["reason"], numerical_checks_passed=sum(passed))
        if race["stage"] in {"development", "adaptive_followup"} and race["status"] == "running":
            live = sum(self._summary(cell["trial_id"])["status"] in ACTIVE for cell in race["cells"] if cell.get("trial_id"))
            slots = max(0, race["profile"]["max_workers"] - live)
            preferred = {algorithm: i for i, algorithm in enumerate(PREFERRED)}
            configurations = {item["id"]: item for item in protocol["configurations"]}
            pending = sorted([cell for cell in race["cells"] if cell["phase"] == "development" and cell["status"] == "pending" and race["configurations"][cell["configuration_id"]]["status"] == "active"],
                key=lambda cell: (cell["rung_seconds"], cell["seed"], preferred.get(configurations[cell["configuration_id"]]["algorithm"], 5)))
            for cell in pending[:slots]:
                remaining_allocation = max(0, cell["rung_seconds"] - (self._summary(cell["trial_id"])["elapsed_seconds"] if cell.get("trial_id") else 0))
                committed = sum(max(0, item["rung_seconds"] - self._summary(item["trial_id"])["elapsed_seconds"]) for item in race["cells"]
                    if item.get("trial_id") and self._summary(item["trial_id"])["status"] in ACTIVE)
                if race["worker_seconds_spent"] + committed + remaining_allocation > protocol["worker_seconds"]:
                    break
                self._launch_cell(race, cell)
        if content_hash(race) != before:
            self._save(race)
        if race["revision"] - race.get("report_revision", 0) >= 30 or race["status"] == "completed":
            race["report_revision"] = race["revision"]
            self.store.put("adaptive_race", race)
            self._write_report(race)

    def view(self, race_id):
        race = self.store.get(race_id, "adaptive_race")
        protocol = self._protocol(race)
        endpoints = self._endpoints(race)
        configurations = []
        for configuration in protocol["configurations"]:
            rows = [row for row in endpoints if row["configuration_id"] == configuration["id"]]
            latest_rung = max((row["rung_seconds"] for row in rows if row["eligible"] and row["phase"] == "development"), default=0)
            latest = [row for row in rows if row["eligible"] and row["phase"] == "development" and row["rung_seconds"] == latest_rung]
            reasons = sorted({reason for row in latest for reason in row["maturity"]["reasons"]})
            configurations.append({"id": configuration["id"], "algorithm": configuration["algorithm"],
                "status": race["configurations"][configuration["id"]]["status"], "mean_score": float(np.mean([row["score"] for row in latest])) if latest else None,
                "sample_standard_deviation": float(np.std([row["score"] for row in latest], ddof=1)) if len(latest) > 1 else None,
                "objective_means": {name: float(np.mean([row["objectives"][name] for row in latest if name in row.get("objectives", {})]))
                    for name in ("mean_plus1_transmission", "te_plus1_transmission", "tm_plus1_transmission", "min_plus1_transmission")
                    if any(name in row.get("objectives", {}) for row in latest)},
                "seeds": len(latest), "rung_seconds": latest_rung, "maturity": {"eligible": len(latest) >= 3 and not reasons, "reasons": reasons or ([] if latest else ["No eligible endpoint yet"])},
                "endpoints": [{key: value for key, value in row.items() if key not in {"diagnostics", "content_hash", "campaign_id", "race_id"}} for row in rows],
                "trial_ids": [cell["trial_id"] for cell in race["cells"] if cell["configuration_id"] == configuration["id"] and cell.get("trial_id")]})
        decisions = [row for row in self.store.list("race_decision", race["campaign_id"]) if row["race_id"] == race_id][-50:]
        running = sum(self._summary(cell["trial_id"])["status"] == "running" for cell in race["cells"] if cell.get("trial_id"))
        spent = self._spent(race)
        return {**{key: race.get(key) for key in ("id", "campaign_id", "study_id", "revision", "status", "stage", "started_at", "deadline_at", "batch_deadline_at", "last_error", "reason", "finished_at")},
            "elapsed_seconds": min(protocol["total_seconds"], max(0, time.time() - race["started_at"])), "total_seconds": protocol["total_seconds"],
            "batch_seconds": protocol["batch_seconds"], "max_workers": race["profile"]["max_workers"], "running_workers": running,
            "worker_seconds_spent": spent, "worker_seconds_cap": protocol["worker_seconds"], "remaining_worker_seconds": max(0, protocol["worker_seconds"] - spent),
            "configurations": configurations, "decisions": decisions, "preflight": race["preflight"], "confirmation": race["confirmation"], "profile": race["profile"],
            "numerical_parameters": race.get("numerical_parameters", protocol["numerical_parameters"]),
            "validation_plan": self.store.get(race["validation_plan_id"], "race_validation_plan") if race.get("validation_plan_id") else None,
            "report_path": str(self.workspace.directory / "races" / race_id / "report.md")}
