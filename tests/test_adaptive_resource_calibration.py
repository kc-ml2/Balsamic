"""Calibration orchestration must stay accounted, safe, and restartable."""
import importlib.util
from copy import deepcopy
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import httpx


spec = importlib.util.spec_from_file_location("resource_calibration", Path(__file__).parents[1] / "scripts/run_adaptive_resource_calibration.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Clock:
    value = 1000.
    def __call__(self):
        return self.value


class Records:
    def __init__(self, directory):
        self.database = directory / "workspace.sqlite3"
        self.race = {"id": "race_one", "campaign_id": "campaign_one", "task_id": "task_original",
                     "protocol_id": "protocol_one", "deadline_at": 10000., "status": "preflight"}
        self.trials = {}
    def get(self, identity, kind=None):
        if kind == "adaptive_race":
            return self.race
        if kind == "race_protocol":
            return {"definition": {"configurations": [
                {"id": "motif", "algorithm": "motif_surgery", "algorithm_config": {}},
                {"id": "adam", "algorithm": "flrl_autograd_adam", "algorithm_config": {"rcwa_order_x": 10, "rcwa_order_y": 5}}]}}
        if kind == "task":
            return {"problem": {"fidelity": {"rcwa_order_x": 14, "rcwa_order_y": 7}}}
        raise AssertionError((identity, kind))


class Commands:
    def __init__(self, records):
        self.records = records; self.requests = []; self.outcomes = {}
    def execute(self, key, operation, payload):
        if key in self.outcomes:
            return self.outcomes[key]
        self.requests.append((key, operation, payload))
        if operation == "trial.create":
            identity = "trial_" + key
            self.records.trials[identity] = {"id": identity, "status": "running", "pid": 123, "process_identity": "17",
                "wall_seconds": payload["wall_seconds"], "numerical_threads": payload["numerical_threads"], "confirmed_observations": 5,
                "gradient_evaluations": 3 if payload["algorithm"] == "flrl_autograd_adam" else 0,
                "elapsed_seconds": 30., "unknown_worker_cost": False}
            path = self.records.database.parent / "trials" / identity / "observations.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"status": "ok", "objectives": {"mean_plus1_transmission": .3,
                "te_plus1_transmission": .2, "tm_plus1_transmission": .4}}) + "\n")
            result = {"trial_id": identity}
        elif operation == "trial.control":
            if payload["action"] == "extend":
                self.records.trials[payload["trial_id"]].update(wall_seconds=payload["wall_seconds"], status="running")
            else:
                self.records.trials[payload["trial_id"]]["status"] = "stopped"
            result = {"trial_id": payload["trial_id"]}
        elif operation == "study.race.decide":
            result = {"race": {"decisions": [{"action": "resource_evidence", "calibration_evidence_id": "calibration_" + key},
                                            {"action": payload["action"]}]}}
        elif operation == "study.race.control":
            self.records.race["status"] = "paused"
            result = {"race": self.records.race}
        else:
            raise AssertionError(operation)
        self.outcomes[key] = result
        return result


def setup_runner(tmp_path, monkeypatch, *, manifest=None, available=32 * 1024**3):
    clock, records = Clock(), Records(tmp_path)
    commands = Commands(records)
    monkeypatch.setattr(module, "read_trial", lambda records, identity: dict(records.trials[identity]))
    memory = {"available": available}
    def sampler(trials):
        return {"time": clock(), "available_memory_bytes": memory["available"], "swap_free_bytes": 8 * 1024**3,
                "peak_rss_bytes": 512 * 1024**2,
                "workers": [{"trial_id": trial["id"], "pid": trial["pid"], "process_identity": trial["process_identity"],
                             "cpu_seconds": clock() - 1000, "rss_bytes": 512 * 1024**2, "peak_rss_bytes": 512 * 1024**2}
                            for trial in trials if trial["status"] == "running"]}
    options = dict(race_id="race_one", campaign_id="campaign_one", clock=clock, sampler=sampler,
                   task_id=None if manifest else "task_selected", preflight_manifest=manifest)
    runner = module.CalibrationRunner(tmp_path / "calibration", records, commands, **options)
    return SimpleNamespace(runner=runner, records=records, commands=commands, clock=clock, sampler=sampler, memory=memory, options=options)


def complete_setting(context):
    setting = context.runner.manifest["settings"][-1]
    for _ in range(3):
        context.clock.value += 5; context.runner.step()
    for identity in setting["trial_ids"]:
        context.records.trials[identity]["status"] = "budget_exhausted"
    context.clock.value += 5; context.runner.step()


def test_waits_for_numerical_fidelity_then_uses_only_registered_api_trials(tmp_path, monkeypatch):
    preflight = tmp_path / "preflight.json"
    context = setup_runner(tmp_path, monkeypatch, manifest=preflight)
    assert context.runner.step() is False
    assert not context.commands.requests
    assert "started_at" not in context.runner.manifest
    preflight.write_text(json.dumps({"numerical_status": "passed", "selected_task_id": "task_selected"}))
    context.runner.step()
    requests = context.commands.requests
    assert [row[1] for row in requests] == ["trial.create", "trial.create", "study.race.decide"]
    assert all(row[2]["race_id"] == "race_one" and row[2]["race_phase"] == "calibration" for row in requests[:2])
    assert requests[1][2]["algorithm_config"] == {"rcwa_order_x": 14, "rcwa_order_y": 7}
    assert requests[1][2]["completion"] == {"unit": "optimizer_decisions", "count": 1}
    assert requests[0][2]["max_steps"] == 1
    assert requests[-1][2]["action"] == "register_calibration"
    complete_setting(context)
    record = next(row for row in requests if row[2].get("action") == "record_calibration")
    assert record[2]["profile"]["numerical_parity_passed"] is True
    assert record[2]["profile"]["sample_count"] == 3
    assert context.runner.manifest["calibration_evidence_ids"]


def test_restart_keeps_allocations_and_a_hard_deadline_stops_live_workers(tmp_path, monkeypatch):
    context = setup_runner(tmp_path, monkeypatch)
    context.runner.step()
    original_ids = context.runner.manifest["settings"][0]["trial_ids"][:]
    restarted = module.CalibrationRunner(context.runner.directory, context.records, context.commands, **context.options)
    restarted.step()
    assert restarted.manifest["settings"][0]["trial_ids"] == original_ids
    assert len([row for row in context.commands.requests if row[1] == "trial.create"]) == 2
    context.clock.value = restarted.manifest["deadline_at"] + 1
    restarted.step()
    stops = [row for row in context.commands.requests if row[1] == "trial.control"]
    assert {row[2]["trial_id"] for row in stops} == set(original_ids)


def test_memory_guard_skips_unsafe_concurrency_and_stops_a_running_profile(tmp_path, monkeypatch):
    context = setup_runner(tmp_path, monkeypatch, available=8 * 1024**3)
    assert context.runner.step() is True
    assert context.runner.manifest["status"] == "calibration_unresolved"
    assert context.commands.requests == [("unresolved_pause", "study.race.control", {"race_id": "race_one", "action": "pause"})]
    assert all("four-GiB" in row["reason"] for row in context.runner.manifest["skipped_settings"])
    assert not context.runner.profile_path.exists()
    assert (context.runner.directory / "calibration-report.md").exists()

    context = setup_runner(tmp_path / "running", monkeypatch)
    context.runner.step(); context.memory["available"] = module.HEADROOM - 1
    context.runner.step()
    assert context.runner.manifest["settings"][0]["status"] == "stopping"
    assert len([row for row in context.commands.requests if row[1] == "trial.control"]) == 2


def test_threading_discrepancies_are_rejected_and_profile_requires_real_work(tmp_path, monkeypatch):
    context = setup_runner(tmp_path, monkeypatch)
    context.runner.step(); complete_setting(context)
    context.runner.step()
    second = context.runner.manifest["settings"][-1]
    assert second["numerical_threads"] == 2
    path = context.records.database.parent / "trials" / second["trial_ids"][0] / "observations.jsonl"
    row = json.loads(path.read_text()); row["objectives"]["tm_plus1_transmission"] = .41
    path.write_text(json.dumps(row) + "\n")
    complete_setting(context)
    assert context.runner.manifest["measurements"][-1]["complete"] is False
    assert context.runner.manifest["measurements"][-1]["numerical_parity_passed"] is False
    assert len([row for row in context.commands.requests if row[2].get("action") == "record_calibration"]) == 1
    context.runner.finish()
    profile = json.loads(context.runner.profile_path.read_text())
    assert profile["calibrated"] is True
    assert profile["threads"] == 1
    assert profile["task_id"] == "task_selected"
    assert profile["calibration_trial_ids"] == context.runner.manifest["settings"][0]["trial_ids"]


def test_profile_selection_balances_workloads_and_rejects_unsafe_or_empty_results():
    measurements = [{"complete": True, "balanced_throughput": 2, "max_workers": 4, "numerical_threads": 4},
                    {"complete": True, "balanced_throughput": 1.96, "max_workers": 3, "numerical_threads": 2},
                    {"complete": False, "balanced_throughput": 10, "max_workers": 4, "numerical_threads": 1}]
    assert module.choose_profile(measurements)["max_workers"] == 3
    with pytest.raises(RuntimeError, match="No measured profile"):
        module.choose_profile([])


def test_native_sampling_rejects_pid_reuse_and_reads_cpu_peak_memory(tmp_path):
    proc = tmp_path / "proc"; worker = proc / "123"; worker.mkdir(parents=True)
    (proc / "meminfo").write_text("MemAvailable: 10000000 kB\nSwapFree: 8000000 kB\n")
    fields = ["S"] + ["0"] * 21
    fields[11], fields[12], fields[19], fields[21] = "100", "50", "17", "20"
    (worker / "stat").write_text("123 (worker (with spaces)) " + " ".join(fields))
    (worker / "status").write_text("VmHWM: 200 kB\n")
    matching = module.native_sample([{"id": "trial_one", "pid": 123, "process_identity": "17"}], proc=proc, now=1)
    assert matching["workers"][0]["cpu_seconds"] == 150 / os.sysconf("SC_CLK_TCK")
    assert matching["workers"][0]["peak_rss_bytes"] == max(200 * 1024, 20 * os.sysconf("SC_PAGE_SIZE"))
    recycled = module.native_sample([{"id": "trial_one", "pid": 123, "process_identity": "18"}], proc=proc, now=1)
    assert recycled["workers"] == []


@pytest.mark.parametrize("failure", ["numerically_unresolved", "deadline"])
def test_no_old_fidelity_fallback_when_numerical_gate_or_global_clock_closes(tmp_path, monkeypatch, failure):
    preflight = tmp_path / "preflight.json"
    context = setup_runner(tmp_path, monkeypatch, manifest=preflight)
    if failure == "deadline":
        context.clock.value = context.records.race["deadline_at"] + 1
    else:
        preflight.write_text(json.dumps({"status": failure}))
    assert context.runner.step() is True
    assert context.runner.manifest["status"] == "blocked"
    assert not context.commands.requests
    assert not context.runner.profile_path.exists()


def test_uncertain_calibration_record_retries_frozen_measurements_without_new_trials(tmp_path, monkeypatch):
    context = setup_runner(tmp_path, monkeypatch)
    execute = context.commands.execute
    records = []
    def uncertain(key, operation, payload):
        outcome = execute(key, operation, payload)
        if payload.get("action") == "record_calibration":
            records.append(deepcopy(payload))
            if len(records) == 1:
                raise httpx.ReadTimeout("Accepted record acknowledgement was lost")
        return outcome
    monkeypatch.setattr(context.commands, "execute", uncertain)
    context.runner.step()
    with pytest.raises(httpx.ReadTimeout):
        complete_setting(context)
    assert context.runner.manifest["settings"][0]["status"] == "recording"
    context.clock.value += 7
    context.runner.step()
    assert records[0] == records[1]
    assert context.runner.manifest["settings"][0]["status"] == "completed"
    assert len([row for row in context.commands.requests if row[1] == "trial.create"]) == 2


def test_slow_first_gradient_gets_same_trial_allowance_then_records_actual_work(tmp_path, monkeypatch):
    context = setup_runner(tmp_path, monkeypatch)
    context.runner.step()
    setting = context.runner.manifest["settings"][0]
    forward_id, gradient_id = setting["trial_ids"]
    gradient = context.records.trials[gradient_id]
    gradient.update(gradient_evaluations=0, confirmed_observations=0, elapsed_seconds=28.)
    context.clock.value += 28; context.runner.step()
    extensions = [row for row in context.commands.requests if row[1] == "trial.control" and row[2]["action"] == "extend"]
    assert len(extensions) == 1
    assert extensions[0][2]["trial_id"] == gradient_id
    assert extensions[0][2]["wall_seconds"] == 180
    assert len([row for row in context.commands.requests if row[1] == "trial.create"]) == 2
    # The first gradient can take several minutes at the validated fidelity.
    gradient.update(elapsed_seconds=110.)
    context.clock.value += 82; context.runner.step()
    assert gradient["wall_seconds"] == 300
    gradient.update(elapsed_seconds=220.)
    context.clock.value += 110; context.runner.step()
    assert gradient["wall_seconds"] == 600
    gradient.update(status="completed", gradient_evaluations=1, confirmed_observations=1, elapsed_seconds=400.)
    context.records.trials[forward_id]["status"] = "completed"
    context.clock.value += 180; context.runner.step()
    measurement = context.runner.manifest["measurements"][0]
    assert measurement["complete"] is True
    assert measurement["gradient_evaluations"] == 1
    assert measurement["gradient_work_seconds"] == 400
    assert len(measurement["allowance_extensions"]) == 3
    # Remaining time cannot support another matched first-work probe.
    context.runner.manifest["deadline_at"] = context.clock() + 200
    assert context.runner.step() is True
    assert len([row for row in context.commands.requests if row[1] == "trial.create"]) == 2
    assert any("Measured first-work duration" in row["reason"] for row in context.runner.manifest["skipped_settings"])
    assert json.loads(context.runner.profile_path.read_text())["calibrated"] is True


def test_slow_gradient_extension_is_bounded_by_remaining_global_calibration_clock(tmp_path, monkeypatch):
    context = setup_runner(tmp_path, monkeypatch)
    context.runner.step()
    gradient_id = context.runner.manifest["settings"][0]["trial_ids"][1]
    gradient = context.records.trials[gradient_id]
    gradient.update(gradient_evaluations=0, confirmed_observations=0, elapsed_seconds=30.)
    context.runner.manifest["deadline_at"] = context.clock() + 80
    context.runner.step()
    assert gradient["wall_seconds"] == 95  # 30 already spent + 80 remaining - 15 shutdown reserve.
    context.clock.value += 81
    context.runner.step()
    assert gradient["status"] == "stopped"
    extensions = [row for row in context.commands.requests if row[1] == "trial.control" and row[2]["action"] == "extend"]
    assert len(extensions) == 1
    assert context.runner.manifest["deadline_at"] == 1080


def test_uncertain_gradient_cost_is_never_resumed_or_counted_as_profile_evidence(tmp_path, monkeypatch):
    context = setup_runner(tmp_path, monkeypatch)
    context.runner.step()
    setting = context.runner.manifest["settings"][0]
    gradient_id = setting["trial_ids"][1]
    context.records.trials[gradient_id].update(status="budget_exhausted", gradient_evaluations=0,
        confirmed_observations=0, elapsed_seconds=50, checkpoint_available=True, resume_supported=True, unknown_worker_cost=True)
    context.records.trials[setting["trial_ids"][0]]["status"] = "completed"
    context.runner.step()
    assert not [row for row in context.commands.requests if row[1] == "trial.control" and row[2]["action"] == "extend"]
    assert context.runner.manifest["measurements"][0]["complete"] is False
    assert not context.runner.manifest["calibration_evidence_ids"]


def test_missing_gradient_soft_lands_with_evidence_and_pauses_only_its_protocol(tmp_path, monkeypatch):
    context = setup_runner(tmp_path, monkeypatch)
    context.runner.step()
    setting = context.runner.manifest["settings"][0]
    for identity in setting["trial_ids"]:
        context.records.trials[identity].update(status="budget_exhausted", gradient_evaluations=0,
            confirmed_observations=0, unknown_worker_cost=True)
    context.runner.step()
    assert context.runner.finish() is True
    assert context.runner.manifest["status"] == "calibration_unresolved"
    assert context.records.race["status"] == "paused"
    assert not context.runner.profile_path.exists()
    report = json.loads((context.runner.directory / "unresolved-report.json").read_text())
    assert report["measurements"][0]["complete"] is False
    assert report["measurements"][0]["gradient_evaluations"] == 0
    assert [(operation, payload) for _, operation, payload in context.commands.requests if operation == "study.race.control"] == [
        ("study.race.control", {"race_id": "race_one", "action": "pause"})]
    # Resuming the operator retains the user's paused state without another control.
    context.runner.step()
    assert len([row for row in context.commands.requests if row[1] == "study.race.control"]) == 1
