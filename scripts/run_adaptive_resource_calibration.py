"""Measure native MEENT worker profiles through registered, accounted trials.

This operator never invokes a simulator or worker subprocess itself. Every
forward/gradient execution is an ordinary researcher command admitted by the
adaptive study controller. The coordinator can be restarted with the same
output directory without duplicating trials or their allocations.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import fcntl
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time

import httpx

try:
    from run_adaptive_grating_preflight import CommandRejected, DurableCommands, ReadOnlyRecords, read_json
except ModuleNotFoundError:
    from scripts.run_adaptive_grating_preflight import CommandRejected, DurableCommands, ReadOnlyRecords, read_json

from optimization_framework.storage.artifacts import atomic_json


HEADROOM = 4 * 1024**3
LIVE = {"queued", "running", "pausing", "stopping", "resuming"}
SUCCESS = {"completed", "budget_exhausted"}
THREADS = (1, 2, 4)


def native_sample(trials, *, proc=Path("/proc"), now=None):
    """Read Linux host measurements; start ticks reject recycled worker PIDs."""
    memory = {line.split(':')[0]: int(line.split()[1]) * 1024
              for line in (proc / "meminfo").read_text().splitlines() if line.startswith(("MemAvailable:", "SwapFree:"))}
    workers = []
    for trial in trials:
        pid, expected = trial.get("pid"), trial.get("process_identity")
        if not pid or not expected:
            continue
        try:
            raw = (proc / str(pid) / "stat").read_text()
            fields = raw[raw.rfind(")") + 2:].split()
            if fields[0] == "Z" or fields[19] != str(expected):
                continue
            rss = max(0, int(fields[21])) * os.sysconf("SC_PAGE_SIZE")
            peak = rss
            for line in (proc / str(pid) / "status").read_text().splitlines():
                if line.startswith("VmHWM:"):
                    peak = max(peak, int(line.split()[1]) * 1024)
            workers.append({"trial_id": trial["id"], "pid": pid, "process_identity": str(expected),
                            "cpu_seconds": (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK"),
                            "rss_bytes": rss, "peak_rss_bytes": peak})
        except (OSError, ValueError, IndexError):
            continue  # A worker may exit between the two native reads.
    return {"time": time.time() if now is None else now,
            "available_memory_bytes": memory["MemAvailable"], "swap_free_bytes": memory.get("SwapFree", 0),
            "peak_rss_bytes": max((row["peak_rss_bytes"] for row in workers), default=0), "workers": workers}


def read_trial(records, trial_id):
    fields = {"status": "$.status", "pid": "$.pid", "process_identity": "$.process_identity", "wall_seconds": "$.wall_seconds",
              "numerical_threads": "$.numerical_threads", "execution_seconds": "$.execution_seconds",
              "confirmed_observations": "$.progress.confirmed_observations", "result_observations": "$.result.confirmed_observations",
              "gradient_evaluations": "$.progress.diagnostics.gradient_evaluations",
              "result_gradients": "$.result.diagnostics.gradient_evaluations", "elapsed_seconds": "$.progress.elapsed_seconds",
              "result_elapsed": "$.result.elapsed_seconds", "unknown_worker_cost": "$.progress.unknown_worker_cost",
              "unknown_solver_cost": "$.progress.unknown_solver_cost", "checkpoint_available": "$.progress.checkpoint_available",
              "resume_supported": "$.progress.resume_supported", "execution_seconds_upper_bound": "$.execution_seconds_upper_bound",
              "result_unknown_worker_cost": "$.result.unknown_worker_cost", "result_unknown_solver_cost": "$.result.unknown_solver_cost"}
    rows = records._query("SELECT " + ",".join("json_extract(data,?)" for _ in fields) + " FROM records WHERE id=? AND kind='trial'",
                          (*fields.values(), trial_id))
    if not rows:
        raise RuntimeError("Missing registered calibration trial " + trial_id)
    result = dict(zip(fields, rows[0])); result["id"] = trial_id
    result["confirmed_observations"] = result.pop("result_observations") or result["confirmed_observations"] or 0
    result["gradient_evaluations"] = result.pop("result_gradients") or result["gradient_evaluations"] or 0
    result["elapsed_seconds"] = max(result.pop("result_elapsed") or 0, result["elapsed_seconds"] or 0, result["execution_seconds"] or 0)
    result["unknown_worker_cost"] = bool(result["unknown_worker_cost"] or result.pop("result_unknown_worker_cost"))
    result["unknown_solver_cost"] = bool(result["unknown_solver_cost"] or result.pop("result_unknown_solver_cost"))
    return result


def first_objectives(records, trial_id):
    path = records.database.parent / "trials" / trial_id / "observations.jsonl"
    if not path.exists():
        return None
    with path.open() as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("status") == "ok":
                values = {key: row.get("objectives", {}).get(key) for key in
                          ("mean_plus1_transmission", "te_plus1_transmission", "tm_plus1_transmission")}
                return values if all(type(value) in (int, float) and math.isfinite(value) for value in values.values()) else None
    return None


def setting_summary(setting):
    samples, trials = setting["samples"], setting["trials"]
    native = [row for row in samples if row["workers"]]
    elapsed = max(1e-9, samples[-1]["time"] - native[0]["time"]) if len(native) > 1 else 0
    counts = {kind: sum(row["confirmed_observations"] if kind == "forward" else row["gradient_evaluations"]
                        for row in trials if row["workload"] == kind) for kind in ("forward", "gradient")}
    rates = {kind: counts[kind] / elapsed if elapsed else 0 for kind in counts}
    balanced = 2 / sum(1 / value for value in rates.values()) if all(rates.values()) else 0
    peaks = {row["id"]: max(worker["peak_rss_bytes"] for sample in native for worker in sample["workers"]
                                   if worker["trial_id"] == row["id"])
             for row in trials if any(worker["trial_id"] == row["id"] for sample in native for worker in sample["workers"])}
    cpu = sum(max(worker["cpu_seconds"] for sample in native for worker in sample["workers"] if worker["trial_id"] == identity)
              for identity in peaks)
    minimum = min((row["available_memory_bytes"] for row in samples), default=0)
    swap_drop = max(0, samples[0]["swap_free_bytes"] - min(row["swap_free_bytes"] for row in samples)) if samples else 0
    valid = (len(native) >= 3 and minimum >= HEADROOM and swap_drop <= 256 * 1024**2
             and all(row["status"] in SUCCESS and not row.get("unknown_worker_cost") and row["confirmed_observations"] > 0 for row in trials)
             and counts["gradient"] > 0 and all(row["id"] in peaks for row in trials))
    return {"max_workers": setting["max_workers"], "numerical_threads": setting["numerical_threads"],
            "task_id": setting["task_id"], "elapsed_seconds": elapsed, "cpu_seconds": cpu,
            "sample_count": len(native), "min_available_memory_bytes": minimum, "swap_consumed_bytes": swap_drop,
            "peak_rss_bytes": max(peaks.values(), default=0), "worker_peaks": peaks,
            "forward_observations": counts["forward"], "gradient_evaluations": counts["gradient"],
            "forward_per_second": rates["forward"], "gradient_per_second": rates["gradient"],
            "balanced_throughput": balanced, "complete": valid}


def choose_profile(measurements):
    safe = [row for row in measurements if row.get("complete") and row.get("balanced_throughput", 0) > 0]
    if not safe:
        raise RuntimeError("No measured profile established safe forward and gradient work")
    # Near-equal throughput favors fewer concurrent workers and less threading.
    best = max(row["balanced_throughput"] for row in safe)
    return min((row for row in safe if row["balanced_throughput"] >= .95 * best),
               key=lambda row: (row["max_workers"], row["numerical_threads"]))


class CalibrationRunner:
    def __init__(self, directory, records, commands, *, race_id, campaign_id, preflight_manifest=None,
                 task_id=None, setting_seconds=90., max_elapsed=900., clock=time.time, sampler=native_sample):
        if not 20 <= setting_seconds <= 90 or not 60 <= max_elapsed <= 900:
            raise ValueError("Calibration settings are 20–90 seconds and the overall allowance is at most fifteen minutes")
        self.directory, self.records, self.commands = Path(directory), records, commands
        self.directory.mkdir(parents=True, exist_ok=True)
        self.clock, self.sampler = clock, sampler
        self.path, self.profile_path = self.directory / "calibration.json", self.directory / "profile.json"
        self.preflight_manifest = Path(preflight_manifest) if preflight_manifest else None
        race = records.get(race_id, "adaptive_race")
        if race["campaign_id"] != campaign_id:
            raise ValueError("Calibration race belongs to another campaign")
        protocol_record = records.get(race["protocol_id"], "race_protocol")
        protocol = protocol_record.get("definition", protocol_record)
        configurations = protocol["configurations"]
        self.configurations = {}
        for workload, algorithm in (("forward", "motif_surgery"), ("gradient", "flrl_autograd_adam")):
            match = next((item for item in configurations if item["algorithm"] == algorithm), None)
            if not match:
                raise ValueError("Calibration needs approved motif_surgery and flrl_autograd_adam configurations")
            self.configurations[workload] = match
        identity = {"race_id": race_id, "campaign_id": campaign_id, "requested_task_id": task_id,
                    "setting_seconds": setting_seconds, "max_elapsed": max_elapsed}
        saved = read_json(self.path)
        if saved and saved["identity"] != identity:
            raise ValueError("A resumed calibration must retain its exact race and resource allowance")
        self.manifest = saved or {"schema_version": 1, "identity": identity, "status": "waiting_for_numerical_checks",
            "task_id": task_id, "settings": [], "measurements": [], "skipped_settings": [], "calibration_evidence_ids": []}
        self.race = race
        self.save()

    def save(self):
        self.manifest["updated_at"] = self.clock()
        atomic_json(self.path, self.manifest)

    def _task(self):
        if self.manifest.get("task_id"):
            return self.manifest["task_id"]
        if self.preflight_manifest:
            preflight = read_json(self.preflight_manifest, {})
            if preflight.get("numerical_status") != "passed" or not preflight.get("selected_task_id"):
                return None
            return preflight["selected_task_id"]
        return self.race["task_id"]

    def _new_setting(self):
        tested = {(row["max_workers"], row["numerical_threads"]) for row in self.manifest["settings"]}
        skipped = {(row["max_workers"], row["numerical_threads"]) for row in self.manifest["skipped_settings"]}
        base = [(workers, threads) for workers in (1, 2) for threads in THREADS]
        pending = [item for item in base if item not in tested | skipped]
        if not pending:
            two = [row for row in self.manifest["measurements"] if row["max_workers"] == 2 and row["complete"]]
            if not two:
                return None
            best = max(two, key=lambda row: row["balanced_throughput"])
            for workers in (3, 4):
                for threads in THREADS:
                    match = next((row for row in two if row["numerical_threads"] == threads), None)
                    if (workers, threads) not in tested | skipped and match and match["balanced_throughput"] < .9 * best["balanced_throughput"]:
                        self.manifest["skipped_settings"].append({"max_workers": workers, "numerical_threads": threads,
                            "reason": "Measured two-worker balanced throughput is over 10% slower than the leading thread setting"})
                        skipped.add((workers, threads))
            pending = [(workers, threads) for workers in (3, 4) for threads in sorted(THREADS, key=lambda value: value != best["numerical_threads"])
                       if (workers, threads) not in tested | skipped]
        if not pending:
            return None
        workers, threads = pending[0]
        available = self.sampler([])["available_memory_bytes"]
        measured_peak = max((row["peak_rss_bytes"] for row in self.manifest["measurements"]), default=0)
        prediction = max(math.ceil(measured_peak * 1.25), 512 * 1024**2) if measured_peak else 5 * 1024**3
        if available - workers * prediction < HEADROOM:
            self.manifest["skipped_settings"].append({"max_workers": workers, "numerical_threads": threads,
                "reason": "Projected peak worker memory would consume the required four-GiB headroom",
                "available_memory_bytes": available, "predicted_worker_peak_bytes": prediction})
            self.save(); return self._new_setting()
        forecast = self.manifest["identity"]["setting_seconds"]
        valid = [row for row in self.manifest["measurements"] if row["complete"]]
        if valid:
            forward = max(row.get("forward_work_seconds", 0) for row in valid)
            gradient = max(row.get("gradient_work_seconds", 0) for row in valid)
            forecast = max(forecast, forward + gradient if workers == 1 else max(forward, gradient))
        if self.clock() + forecast + 20 >= self.manifest["deadline_at"]:
            self.manifest["skipped_settings"].append({"max_workers": workers, "numerical_threads": threads,
                "reason": "Measured first-work duration will not fit the remaining fifteen-minute calibration envelope",
                "forecast_seconds": forecast})
            self.save(); return self._new_setting()
        setting = {"id": f"w{workers}_t{threads}", "max_workers": workers, "numerical_threads": threads,
                   "task_id": self.manifest["task_id"], "status": "creating", "trial_ids": [], "trials": [], "samples": [],
                   "registration_profile": {"max_workers": workers, "numerical_threads": threads, "task_id": self.manifest["task_id"],
                       "memory_headroom_bytes": HEADROOM, "worker_peak_bytes": prediction}}
        self.manifest["settings"].append(setting); self.save(); return setting

    def _create(self, setting):
        workloads = ["forward", "gradient"] if setting["max_workers"] < 3 else ["forward", "gradient", "forward", "gradient"][:setting["max_workers"]]
        seconds = self.manifest["identity"]["setting_seconds"] / (2 if setting["max_workers"] == 1 else 1)
        for index, workload in enumerate(workloads):
            if index < len(setting["trial_ids"]):
                continue
            configuration = self.configurations[workload]
            parameters = deepcopy(configuration["algorithm_config"])
            task = self.records.get(setting["task_id"], "task")
            for key in ("rcwa_order_x", "rcwa_order_y"):
                if key in parameters:
                    parameters[key] = task["problem"]["fidelity"][key]
            payload = {"task_id": setting["task_id"], "algorithm": configuration["algorithm"],
                "algorithm_config": parameters, "hypothesis_id": configuration.get("hypothesis_id"),
                "seed": 173 + index % 2, "max_steps": 100000 if workload == "gradient" else 1,
                "completion": {"unit": "optimizer_decisions", "count": 1} if workload == "gradient" else {"unit": "evaluation_requests", "count": 1},
                "schedule_steps": 100000, "wall_seconds": seconds,
                "numerical_threads": setting["numerical_threads"], "race_id": self.race["id"], "race_phase": "calibration",
                "question": "Measure registered native MEENT forward/gradient resource throughput; exclude from scientific rankings"}
            outcome = self.commands.execute(setting["id"] + f"_trial_{index}", "trial.create", payload)
            setting["trial_ids"].append(outcome.get("trial_id") or outcome["trial"]["id"])
            self.save()
        self.commands.execute(setting["id"] + "_register", "study.race.decide", {"race_id": self.race["id"],
            "action": "register_calibration", "evidence_trial_ids": setting["trial_ids"], "profile": setting["registration_profile"],
            "rationale": "Measure this bounded native forward/gradient resource profile before scientific optimization"})
        setting["status"] = "running"; self.save()

    def _stop(self, setting, reason):
        for trial_id in setting["trial_ids"]:
            trial = read_trial(self.records, trial_id)
            if trial["status"] in LIVE:
                self.commands.execute(setting["id"] + "_stop_" + trial_id, "trial.control",
                    {"trial_id": trial_id, "action": "stop", "rationale": reason})
        setting["status"] = "stopping"; setting["stop_reason"] = reason; self.save()

    def _extend_first_work(self, setting, trials):
        """Give an expensive first gradient room inside the existing 900s cap."""
        extensions = setting.setdefault("extensions", [])
        reconciled = set()
        for extension in extensions:
            if extension["status"] == "prepared":
                self.commands.execute(extension["command_key"], "trial.control", extension["payload"])
                extension["status"] = "applied"; self.save()
                reconciled.add(extension["payload"]["trial_id"])
        for trial in trials:
            if trial["id"] in reconciled:
                if trial["status"] == "budget_exhausted":
                    trial["status"] = "queued"
                continue
            count = trial["gradient_evaluations"] if trial["workload"] == "gradient" else trial["confirmed_observations"]
            if count > 0 or trial.get("unknown_worker_cost") or trial.get("unknown_solver_cost"):
                continue
            current = trial.get("wall_seconds") or self.manifest["identity"]["setting_seconds"] / (2 if setting["max_workers"] == 1 else 1)
            current = max(current, max((item["payload"]["wall_seconds"] for item in extensions
                                       if item["payload"]["trial_id"] == trial["id"] and item["status"] == "applied"), default=0))
            spent = max(trial["elapsed_seconds"], trial.get("execution_seconds_upper_bound") or 0)
            resumable = trial["status"] == "budget_exhausted" and trial.get("checkpoint_available") and trial.get("resume_supported") is not False
            if trial["status"] != "running" and not resumable:
                continue
            if trial["status"] == "running" and spent < .55 * current:
                continue
            remaining = self.manifest["deadline_at"] - self.clock() - 15
            target = min(next((value for value in (180., 300., 600.) if value > current), spent + remaining), spent + remaining)
            if remaining <= 15 or target <= max(current, spent) + 10:
                continue
            payload = {"trial_id": trial["id"], "action": "extend", "wall_seconds": target,
                       "rationale": "Allow the first actual resource-calibration proposal to finish within the unchanged fifteen-minute total cap"}
            extension = {"status": "prepared", "created_at": self.clock(), "previous_wall_seconds": current,
                         "command_key": setting["id"] + "_extend_" + trial["id"] + "_" + str(len(extensions)), "payload": payload}
            extensions.append(extension); self.save()
            self.commands.execute(extension["command_key"], "trial.control", payload)
            extension["status"] = "applied"; self.save()
            # A recovered terminal trial has been queued by the same command;
            # keep this probe active until its new state is observed.
            if resumable:
                trial["status"] = "queued"

    def _measure(self, setting):
        trials = [read_trial(self.records, identity) for identity in setting["trial_ids"]]
        for index, trial in enumerate(trials):
            trial["workload"] = "gradient" if index % 2 else "forward"
        setting["trials"] = trials
        sample = self.sampler(trials)
        setting["samples"].append(sample)
        with (self.directory / "native_samples.jsonl").open("a") as stream:
            stream.write(json.dumps({"setting_id": setting["id"], **sample}, allow_nan=False) + "\n")
        if self.clock() < self.manifest["deadline_at"] and sample["available_memory_bytes"] >= HEADROOM:
            self._extend_first_work(setting, trials)
        if any(row["status"] in LIVE for row in trials):
            if self.clock() >= self.manifest["deadline_at"] or sample["available_memory_bytes"] < HEADROOM:
                self._stop(setting, "Calibration deadline or four-GiB memory headroom reached")
            else:
                self.save()
            return
        summary = setting_summary(setting)
        summary["trial_ids"] = setting["trial_ids"]
        for workload in ("forward", "gradient"):
            summary[workload + "_work_seconds"] = max((trial["elapsed_seconds"] for trial in trials if trial["workload"] == workload), default=0)
        summary["allowance_extensions"] = setting.get("extensions", [])
        objectives = {trial["id"]: first_objectives(self.records, trial["id"]) for trial in trials}
        references = self.manifest.setdefault("reference_objectives", {})
        if setting["id"] == "w1_t1":
            references.update({trial["workload"]: objectives[trial["id"]] for trial in trials if objectives[trial["id"]] is not None})
        parity = all(objectives[trial["id"]] is not None and references.get(trial["workload"]) is not None and
                     all(abs(value - references[trial["workload"]][key]) <= 1e-6
                         for key, value in objectives[trial["id"]].items()) for trial in trials)
        summary.update(first_objectives=objectives, numerical_parity_passed=parity, numerical_parity_tolerance=1e-6)
        summary["complete"] &= parity
        if summary["complete"]:
            # Freeze a completed probe before submitting its immutable record.
            # A timeout must retry exactly these samples, without appending a
            # new polling measurement and changing the command's payload.
            setting.update(status="recording", summary=summary,
                recording_profile={**summary, "memory_headroom_bytes": HEADROOM, "samples": deepcopy(setting["samples"])})
            self.save(); self._record(setting)
            return
        summary["reason"] = setting.get("stop_reason") or "Insufficient work, native measurements, numerical consistency, or safe memory/swap behavior"
        self.manifest["measurements"].append(summary)
        setting["status"] = "rejected"; self.save()

    def _record(self, setting):
        outcome = self.commands.execute(setting["id"] + "_record", "study.race.decide", {"race_id": self.race["id"],
            "action": "record_calibration", "evidence_trial_ids": setting["trial_ids"], "profile": setting["recording_profile"],
            "rationale": "Native host measurements from completed accounted MEENT forward and gradient trials"})
        view = outcome.get("race", outcome)
        evidence_id = outcome.get("calibration_evidence_id") or next(row["calibration_evidence_id"]
            for row in reversed(view["decisions"]) if row.get("calibration_evidence_id"))
        summary = {**setting["summary"], "calibration_evidence_id": evidence_id}
        self.manifest["calibration_evidence_ids"].append(evidence_id)
        self.manifest["measurements"].append(summary)
        setting["status"] = "completed"; self.save()

    def finish(self):
        try:
            chosen = choose_profile(self.manifest["measurements"])
        except RuntimeError as exc:
            self.manifest.update(status="calibration_unresolved", reason=str(exc))
            self.save()
            return self._unresolved()
        trial_ids = [identity for row in self.manifest["measurements"] if row["complete"] for identity in row["trial_ids"]]
        profile = {"schema_version": 1, "race_id": self.race["id"], "campaign_id": self.race["campaign_id"],
            "task_id": self.manifest["task_id"], "calibrated": True, "threads": chosen["numerical_threads"],
            "numerical_threads": chosen["numerical_threads"], "max_workers": chosen["max_workers"],
            "memory_headroom_bytes": HEADROOM, "worker_peak_bytes": max(512 * 1024**2, math.ceil(chosen["peak_rss_bytes"] * 1.25)),
            "calibration_trial_ids": trial_ids, "calibration_evidence_ids": self.manifest["calibration_evidence_ids"],
            "measurements": self.manifest["measurements"], "skipped_settings": self.manifest["skipped_settings"],
            "elapsed_seconds": self.clock() - self.manifest["started_at"], "finished_at": self.clock(),
            "selection_rule": "Highest measured balanced first-work forward/gradient throughput; profiles within five percent prefer fewer workers/threads",
            "limitations": ["Finite first-proposal measurements include cold initialization and are noisy; they do not establish sustained steady-state throughput",
                            "Slow first gradients can consume the calibration envelope; untested settings remain explicitly skipped"]}
        atomic_json(self.profile_path, profile)
        self.manifest["status"] = "completed"; self.save(); return True

    def _unresolved(self):
        """Preserve rejected measurements and pause only this protocol."""
        report = {"status": "calibration_unresolved", "reason": self.manifest.get("reason"),
                  "race_id": self.race["id"], "task_id": self.manifest.get("task_id"),
                  "measurements": self.manifest["measurements"], "skipped_settings": self.manifest["skipped_settings"],
                  "calibration_evidence_ids": self.manifest["calibration_evidence_ids"],
                  "native_samples": str(self.directory / "native_samples.jsonl")}
        atomic_json(self.directory / "unresolved-report.json", report)
        (self.directory / "calibration-report.md").write_text("# Resource calibration unresolved\n\n" +
            str(report["reason"]) + "\n\nNo safe runtime profile was published. The saved measurements remain available for diagnosis.\n\n" +
            "```json\n" + json.dumps(report, indent=2, allow_nan=False) + "\n```\n")
        self.race = self.records.get(self.race["id"], "adaptive_race")
        if self.race["status"] in {"preflight", "running"} and self.clock() < self.race["deadline_at"]:
            self.commands.execute("unresolved_pause", "study.race.control", {"race_id": self.race["id"], "action": "pause"})
            self.manifest["protocol_pause_recorded"] = True
            self.save()
        return True

    def step(self):
        if self.manifest["status"] == "calibration_unresolved":
            return self._unresolved()
        if self.manifest["status"] in {"completed", "blocked"}:
            return True
        self.race = self.records.get(self.race["id"], "adaptive_race")
        task_id = self._task()
        if task_id is None:
            preflight = read_json(self.preflight_manifest, {}) if self.preflight_manifest else {}
            if (preflight.get("status") in {"numerically_unresolved", "failed", "stopped"}
                    or self.race["status"] in {"stopped", "completed", "budget_exhausted"}
                    or self.clock() >= self.race["deadline_at"]):
                self.manifest.update(status="blocked", reason="Numerical preflight did not establish a task within the authorized elapsed envelope")
                self.save(); return True
            return False
        if "started_at" not in self.manifest:
            self.manifest.update(task_id=task_id, started_at=self.clock(),
                deadline_at=min(self.clock() + self.manifest["identity"]["max_elapsed"], self.race["deadline_at"]), status="calibrating")
            self.save()
        setting = next((row for row in self.manifest["settings"] if row["status"] in {"creating", "running", "stopping", "recording"}), None)
        if setting:
            if setting["status"] == "recording":
                self._record(setting)
            elif setting["status"] == "creating":
                if self.clock() >= self.manifest["deadline_at"] or self.race["status"] in {"stopped", "completed", "budget_exhausted"}:
                    self._stop(setting, "Calibration envelope closed before registration completed")
                elif self.race["status"] != "paused":
                    self._create(setting)
            else:
                self._measure(setting)
            return False
        if self.race["status"] in {"stopped", "completed", "budget_exhausted"} or self.clock() >= self.manifest["deadline_at"]:
            return self.finish()
        if self.race["status"] == "paused":
            return False
        setting = self._new_setting()
        if setting is None:
            return self.finish()
        self._create(setting)
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--race-id", required=True); parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--output", type=Path, required=True); parser.add_argument("--task-id")
    parser.add_argument("--preflight-manifest", type=Path)
    parser.add_argument("--database", type=Path, default=Path("runs/workspace/workspace/workspace.sqlite3"))
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--setting-seconds", type=float, default=90); parser.add_argument("--max-elapsed", type=float, default=900)
    parser.add_argument("--poll-seconds", type=float, default=.5)
    args = parser.parse_args(); args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / "operator.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        records = ReadOnlyRecords(args.database)
        commands = DurableCommands(args.url, args.campaign_id, args.output / "commands", records,
                                  namespace="resource_calibration_" + args.race_id)
        runner = CalibrationRunner(args.output, records, commands, race_id=args.race_id, campaign_id=args.campaign_id,
            preflight_manifest=args.preflight_manifest, task_id=args.task_id, setting_seconds=args.setting_seconds, max_elapsed=args.max_elapsed)
        try:
            while True:
                try:
                    if runner.step():
                        break
                    runner.manifest.pop("last_transient_error", None)
                except (httpx.HTTPError, sqlite3.OperationalError, CommandRejected) as exc:
                    if isinstance(exc, CommandRejected) and not re.search(r"HTTP (?:5\d\d|429|408):", str(exc)):
                        runner.manifest.update(status="calibration_unresolved", reason="Calibration command was rejected: " + str(exc))
                        runner.save()
                        if runner._unresolved():
                            break
                    # The exact researcher request is already durable; the next
                    # step reconciles its receipt instead of duplicating work.
                    runner.manifest["last_transient_error"] = str(exc)
                    runner.save()
                except (ValueError, RuntimeError) as exc:
                    runner.manifest.update(status="calibration_unresolved", reason="Calibration could not establish valid evidence: " + str(exc))
                    runner.save()
                    if runner._unresolved():
                        break
                time.sleep(max(.5, args.poll_seconds))
            print(json.dumps(read_json(runner.profile_path) or runner.manifest, indent=2))
            if runner.manifest["status"] != "completed":
                raise SystemExit(2)
        finally:
            commands.close()


if __name__ == "__main__":
    main()
