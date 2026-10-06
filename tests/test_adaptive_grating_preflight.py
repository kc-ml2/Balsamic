"""The preflight operator preserves fixture identity and reconciles real receipts."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sqlite3
import time

import httpx
import pytest

from optimization_framework.contracts.commands import Command


spec = importlib.util.spec_from_file_location("grating_preflight", Path(__file__).parents[1] / "scripts/run_adaptive_grating_preflight.py")
operator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(operator)


@pytest.fixture(autouse=True)
def stable_memory_admission(monkeypatch):
    monkeypatch.setattr(operator, "available_memory_bytes", lambda: 64 * 1024**3)


def source_results():
    return [{"algorithm": algorithm, "config_label": algorithm + " config", "seed": seed,
             "trial_id": f"source_{algorithm}_{seed}", "best_observation_id": f"observation_{algorithm}_{seed}",
             "best_mean": score, "best_te": score, "best_tm": score}
            for algorithm in sorted(operator.ALGORITHMS) for seed, score in ((17, .2), (41, .3), (73, .4))]


class MemoryRecords:
    def __init__(self):
        self.version = 6
        self.rows = {"campaign": {"version": self.version},
                     "race": {"id": "race", "campaign_id": "campaign", "task_id": "task", "study_id": "study",
                              "status": "preflight", "deadline_at": time.time() + 10000, "preflight": {"status": "pending"}},
                     "task": {"id": "task", "name": "2D grating", "problem": {
                         "definition_id": "meent_2d_dual_polarization_deflector", "configuration": {},
                         "fidelity": {"rcwa_order_x": 10, "rcwa_order_y": 5}}}}

    def get(self, identity, kind=None):
        return deepcopy(self.rows[identity])

    def campaign_revision(self, identity):
        return self.version

    def solution_for_fixture(self, fixture):
        if fixture["fixture_id"] == "flrl_autograd_adam_median":
            raise operator.WaitingForEvidence("Missing archive: use exact journal snapshot")
        identity = "asset_" + fixture["fixture_id"]
        self.rows[identity] = {"id": identity, "producer_id": fixture["source_trial_id"],
                               "payload": {"candidate": [0, 1, 1, 0], "observation_id": fixture["source_observation_id"]}}
        return deepcopy(self.rows[identity])

    def _query(self, sql, parameters):
        name = parameters[-1]
        return [(json.dumps(row),) for row in self.rows.values()
                if row.get("name") == name and not row.get("archived")]


class MemoryCommands:
    def __init__(self, records):
        self.records = records
        self.calls = []
        self.receipts = {}
        self.first_failure = False
        self.fidelity_shift = False
        self.minimum_passing_order = 14

    def execute(self, key, operation, payload):
        if key in self.receipts:
            return self.receipts[key]
        self.calls.append((key, operation, deepcopy(payload)))
        if operation == "asset.snapshot":
            identity = "snapshot_" + key
            self.records.rows[identity] = {"id": identity, "producer_id": payload["trial_id"],
                                           "payload": {"candidate": [0, 1, 1, 0], "observation_id": payload["observation_id"]}}
            result = {"asset_id": identity}
        elif operation == "asset.reuse":
            result = {"reuse_decision_id": "reuse_" + key}
        elif operation == "trial.create":
            identity = "trial_" + key
            self.records.rows[identity] = {"id": identity, "status": "completed", "control_revision": 0,
                "result": {"scientific_complete": True, "best_efficiency": .3}}
            result = {"trial_id": identity}
        elif operation == "validation.run":
            identity = "trial_" + key
            highest = payload["parameters"]["fidelities"][-1]
            passed = not ((self.first_failure and highest["rcwa_order_x"] == 14 or
                          highest["rcwa_order_x"] < self.minimum_passing_order) and "flrl_ppo_median" in key)
            subject = {"complete": True, "verdict": "passed" if passed else "failed",
                "max_energy_error": 1e-12, "invalid_evidence": [], "missing_evidence": [],
                "last_two_absolute_differences": {name: .001 if passed else .01 for name in
                    ("mean_plus1_transmission", "te_plus1_transmission", "tm_plus1_transmission")},
                "repeat_max_absolute_differences": {name: 0 for name in
                    ("mean_plus1_transmission", "te_plus1_transmission", "tm_plus1_transmission")},
                "highest_fidelity": highest, "observations": [{"fidelity": fidelity,
                    "objectives": {name: .3 for name in ("mean_plus1_transmission", "te_plus1_transmission", "tm_plus1_transmission")}}
                    for fidelity in payload["parameters"]["fidelities"]]}
            if self.fidelity_shift:
                for name in subject["observations"][-1]["objectives"]:
                    subject["observations"][-1]["objectives"][name] += .02
            self.records.rows[identity] = {"id": identity, "status": "completed", "result": {
                "scientific_complete": True, "elapsed_seconds": 20, "recipe_result": {"recipe_id": operator.ORDER_CONVERGENCE_RECIPE,
                    "complete": True, "verdict": subject["verdict"], "subjects": [subject]}}}
            result = {"trial_id": identity}
        elif operation == "study.race.decide":
            if payload["action"] == "accept_preflight":
                self.records.rows["race"]["preflight"]["status"] = "passed"
            result = {"race_id": "race"}
        elif operation == "campaign.update":
            task = payload["tasks"][-1]
            self.records.rows["higher_task"] = {"id": "higher_task", "name": task["name"], "problem": {
                "definition_id": task["problem_id"], "configuration": task["configuration"], "fidelity": task["fidelity"]}}
            result = {"campaign_id": "campaign"}
        elif operation == "study.race.control":
            self.records.rows["race"]["status"] = "paused"
            result = {"race_id": "race"}
        else:
            raise AssertionError(f"Unexpected mutation {operation}")
        self.receipts[key] = result
        return result


def make_runner(tmp_path, *, race=True, profile=True):
    sources = tmp_path / "results.json"
    sources.write_text(json.dumps(source_results()))
    records = MemoryRecords()
    commands = MemoryCommands(records)
    resource_path = tmp_path / "profile.json"
    if profile:
        resource_path.write_text(json.dumps({"calibrated": True, "calibration_evidence_ids": ["calibration_measured"],
            "task_id": "task", "max_workers": 2, "numerical_threads": 1, "memory_headroom_bytes": 4 * 1024**3}))
    runner = operator.PreflightRunner(tmp_path / "preflight", records, commands, campaign_id="campaign",
        task_id="task", pilot_results=sources, race_id="race" if race else None, resource_profile=resource_path)
    return runner, records, commands


def test_selects_eight_median_masks_and_distinct_adam_high_outlier():
    fixtures = operator.select_fixtures(source_results())
    assert len(fixtures) == 9
    assert all(fixture["seed"] == 41 for fixture in fixtures[:-1])
    assert fixtures[-1]["fixture_id"] == "flrl_autograd_adam_outlier" and fixtures[-1]["seed"] == 73


def test_different_configurations_cannot_be_pooled_to_choose_fixture():
    rows = source_results()
    rows[0]["config_label"] = "other configuration"
    with pytest.raises(ValueError, match="Different configurations"):
        operator.select_fixtures(rows)


def test_prepare_only_publishes_missing_exact_observed_snapshot_without_jobs(tmp_path):
    runner, records, commands = make_runner(tmp_path, race=False)
    for _ in range(15):
        if not runner.step(prepare_only=True):
            break
    assert runner.manifest["status"] == "prepared"
    assert len({fixture["asset_id"] for fixture in runner.manifest["fixtures"]}) == 9
    assert [operation for _, operation, _ in commands.calls] == ["asset.snapshot"]


def test_resuming_preserves_fixture_roster_and_does_not_republish_source(tmp_path):
    runner, records, commands = make_runner(tmp_path)
    for _ in range(9):
        runner.step(prepare_only=True)
    resumed = operator.PreflightRunner(runner.directory, records, commands, campaign_id="campaign", task_id="task",
        pilot_results=tmp_path / "results.json", race_id="race", resource_profile=tmp_path / "profile.json")
    assert resumed.manifest["fixtures"] == runner.manifest["fixtures"]
    for _ in range(100):
        if not resumed.step():
            break
    assert resumed.manifest["status"] == "completed"
    assert sum(operation == "asset.snapshot" for _, operation, _ in commands.calls) == 1
    assert sum(operation == "trial.create" for _, operation, _ in commands.calls) == 9
    assert sum(operation == "validation.run" for _, operation, _ in commands.calls) == 9
    decision = next(payload for _, operation, payload in commands.calls
                    if operation == "study.race.decide" and payload["action"] == "accept_preflight")
    assert len(set(decision["evidence_trial_ids"])) == 9


def test_no_second_fixture_job_allocated_while_first_job_is_running(tmp_path):
    runner, records, commands = make_runner(tmp_path)
    for _ in range(9 + 3):
        runner.step()
    first = runner.manifest["fixtures"][0]
    records.rows[first["measurement_trial_id"]]["status"] = "running"
    with pytest.raises(operator.WaitingForEvidence, match="Measuring"):
        runner.step()
    assert sum(operation == "trial.create" for _, operation, _ in commands.calls) == 1
    assert sum(operation == "validation.run" for _, operation, _ in commands.calls) == 0


def test_resource_profile_waits_until_numerical_selected_task_is_published(tmp_path):
    runner, records, commands = make_runner(tmp_path, profile=False)
    with pytest.raises(operator.WaitingForEvidence, match="measured resource calibration"):
        for _ in range(100):
            runner.step()
    saved = json.loads(runner.path.read_text())
    assert saved["numerical_status"] == "passed" and saved["selected_task_id"] == "task"
    assert not any(operation == "study.race.decide" and payload["action"] == "accept_preflight"
                   for _, operation, payload in commands.calls)


def test_failed_actual_check_runs_common_higher_fidelity_for_every_mask(tmp_path):
    runner, records, commands = make_runner(tmp_path)
    commands.first_failure = True
    for _ in range(200):
        if not runner.step():
            break
    assert runner.manifest["status"] == "completed"
    checks = [payload for _, operation, payload in commands.calls if operation == "validation.run"]
    assert len(checks) == 18
    assert sum(payload["parameters"]["fidelities"][-1] == {"rcwa_order_x": 18, "rcwa_order_y": 9}
               for payload in checks) == 9
    assert all(len(fixture["validation_attempts"]) == 2 for fixture in runner.manifest["fixtures"])
    assert "failed" in (runner.directory / "preflight-report.md").read_text()


def test_elapsed_deadline_closes_operator_without_allocating_more_jobs(tmp_path):
    runner, records, commands = make_runner(tmp_path)
    for _ in range(9):
        runner.step(prepare_only=True)
    records.rows["race"]["deadline_at"] = time.time() - 1
    assert not runner.step()
    assert runner.manifest["status"] == "execution_closed"
    assert not any(operation == "trial.create" for _, operation, _ in commands.calls)


def test_material_fidelity_shift_creates_one_new_task_and_waits_for_its_calibration(tmp_path):
    runner, records, commands = make_runner(tmp_path)
    commands.fidelity_shift = True
    with pytest.raises(operator.WaitingForEvidence, match="another fidelity"):
        for _ in range(100):
            runner.step()
    saved = json.loads(runner.path.read_text())
    assert saved["selected_task_id"] == "higher_task"
    assert saved["currenttask_fidelity"] == {"rcwa_order_x": 14, "rcwa_order_y": 7}
    assert sum(operation == "campaign.update" for _, operation, _ in commands.calls) == 1
    profile = json.loads((tmp_path / "profile.json").read_text())
    profile["task_id"] = "higher_task"
    (tmp_path / "profile.json").write_text(json.dumps(profile))
    runner.step()
    assert runner.manifest["status"] == "completed"
    assert sum(operation == "campaign.update" for _, operation, _ in commands.calls) == 1


def test_readonly_catalog_uses_exact_observation_not_another_seed(tmp_path):
    database = tmp_path / "workspace.sqlite3"
    fixture = operator.select_fixtures(source_results())[0]
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE records (id TEXT, kind TEXT, data TEXT)")
    wrong = {"id": "wrong", "producer_id": fixture["source_trial_id"], "kind": "solution",
             "payload": {"candidate": [0, 1], "observation_id": "another_observation"}}
    right = {**wrong, "id": "right", "title": "optimizer: archived solution 1",
             "payload": {"candidate": [0, 1], "observation_id": fixture["source_observation_id"]}}
    for asset in (wrong, right):
        connection.execute("INSERT INTO records VALUES (?, 'asset', ?)", (asset["id"], json.dumps(asset)))
    connection.commit()
    connection.close()
    records = operator.ReadOnlyRecords(database)
    assert records.solution_for_fixture(fixture)["id"] == "right"


def test_uncertain_post_reconciles_same_saved_receipt_without_duplicate_dispatch(tmp_path):
    records = MemoryRecords()
    accepted = {}
    posts = []

    def handler(request):
        identity = request.url.path.rsplit("/", 1)[-1]
        if request.method == "GET":
            return httpx.Response(200, json=accepted[identity]) if identity in accepted else httpx.Response(404)
        raw = json.loads(request.content)
        posts.append(raw)
        accepted[raw["id"]] = {"actor": "researcher", "status": "completed", "request": Command(**raw).model_dump(mode="json"),
                               "outcome": {"reuse_decision_id": "one_accepted_decision"}}
        raise httpx.ReadTimeout("Accepted response was lost", request=request)

    http = httpx.Client(base_url="http://workspace", transport=httpx.MockTransport(handler))
    commands = operator.DurableCommands("http://workspace", "campaign", tmp_path / "receipts", records, http=http)
    payload = {"asset_id": "asset", "study_id": "study", "decision": "reuse", "intended_use": "optimizer_input", "rationale": "exact fixture"}
    with pytest.raises(httpx.ReadTimeout):
        commands.execute("reuse_once", "asset.reuse", payload)
    assert commands.execute("reuse_once", "asset.reuse", payload)["reuse_decision_id"] == "one_accepted_decision"
    assert len(posts) == 1


def test_proven_stale_campaign_rejection_retries_new_revision_without_changing_payload(tmp_path):
    records = MemoryRecords()
    posts = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(404)
        raw = json.loads(request.content)
        posts.append(raw)
        if raw["expected_revision"] == 6:
            return httpx.Response(409, text="Campaign revision changed")
        return httpx.Response(200, json={"actor": "researcher", "status": "completed",
            "request": Command(**raw).model_dump(mode="json"), "outcome": {"asset_id": "observed_snapshot"}})

    http = httpx.Client(base_url="http://workspace", transport=httpx.MockTransport(handler))
    commands = operator.DurableCommands("http://workspace", "campaign", tmp_path / "receipts", records, http=http)
    payload = {"trial_id": "source", "observation_id": "observation"}
    with pytest.raises(operator.CommandRejected):
        commands.execute("snapshot", "asset.snapshot", payload)
    records.version = 7
    assert commands.execute("snapshot", "asset.snapshot", payload)["asset_id"] == "observed_snapshot"
    assert posts[0]["id"] != posts[1]["id"] and posts[0]["payload"] == posts[1]["payload"]


def test_incomplete_recipe_never_qualifies_as_a_pass():
    result = {"id": "check", "status": "completed", "result": {"scientific_complete": False,
        "recipe_result": {"recipe_id": operator.ORDER_CONVERGENCE_RECIPE, "complete": False,
            "verdict": "passed", "subjects": [{"complete": True, "verdict": "passed"}]}}}
    assert not operator.recipe_passed(result)


def test_truncation_disagreement_can_progress_beyond_eighteen_with_memory_headroom(tmp_path):
    runner, records, commands = make_runner(tmp_path)
    commands.minimum_passing_order = 22
    runner.manifest["worker_memory_samples"] = [{"phase": "validation", "completed_evaluations": 3,
        "peak_rss_bytes": 1024**3, "fidelity": {"rcwa_order_x": 14, "rcwa_order_y": 7}}]
    for _ in range(300):
        if not runner.step():
            break
    assert runner.manifest["status"] == "completed"
    checks = [payload for _, operation, payload in commands.calls if operation == "validation.run"]
    assert len(checks) == 27
    assert sum(payload["parameters"]["fidelities"][-1]["rcwa_order_x"] == 22 for payload in checks) == 9
    assert checks[-1]["parameters"]["fidelities"] == [
        {"rcwa_order_x": 10, "rcwa_order_y": 5}, {"rcwa_order_x": 18, "rcwa_order_y": 9},
        {"rcwa_order_x": 22, "rcwa_order_y": 11}]
    assert len(runner.manifest["memory_predictions"]) == 2


def test_insufficient_memory_pauses_honestly_without_allocating_higher_jobs(tmp_path, monkeypatch):
    runner, records, commands = make_runner(tmp_path)
    commands.first_failure = True
    monkeypatch.setattr(operator, "available_memory_bytes", lambda: 6 * 1024**3)
    for _ in range(150):
        if not runner.step():
            break
    assert runner.manifest["status"] == "numerically_unresolved"
    assert runner.manifest["hold_category"] == "memory_headroom"
    assert records.rows["race"]["status"] == "paused"
    assert sum(operation == "validation.run" for _, operation, _ in commands.calls) == 9


def test_energy_failure_does_not_trigger_pointless_order_escalation(tmp_path):
    runner, records, commands = make_runner(tmp_path)
    commands.first_failure = True
    for _ in range(150):
        failing = runner.manifest["fixtures"][3]
        if failing["phase"] == "numerical_followup":
            identity = failing["validation_attempts"][-1]["trial_id"]
            records.rows[identity]["result"]["recipe_result"]["subjects"][0]["max_energy_error"] = .02
        if not runner.step():
            break
    assert runner.manifest["hold_category"] == "solver_consistency"
    assert sum(operation == "validation.run" for _, operation, _ in commands.calls) == 9


def test_owned_process_sampling_rejects_reused_pid_and_records_real_high_water(tmp_path):
    process = tmp_path / "123"
    process.mkdir()
    fields = ["S"] + ["0"] * 18 + ["456"]
    (process / "stat").write_text("123 (worker) " + " ".join(fields))
    (process / "status").write_text("VmHWM:\t1024 kB\nVmRSS:\t768 kB\nVmSwap:\t0 kB\n")
    sample = operator.trusted_worker_memory({"pid": 123, "process_identity": "456"}, tmp_path)
    assert sample["peak_rss_bytes"] == 1024**2 and sample["rss_bytes"] == 768 * 1024
    assert operator.trusted_worker_memory({"pid": 123, "process_identity": "old_owner"}, tmp_path) is None


def test_prediction_uses_highest_completed_fidelity_and_factor_two():
    samples = [{"phase": "validation", "completed_evaluations": 1, "peak_rss_bytes": 1024**3,
                "fidelity": {"rcwa_order_x": 10, "rcwa_order_y": 5}},
               {"phase": "validation", "completed_evaluations": 3, "peak_rss_bytes": 2 * 1024**3,
                "fidelity": {"rcwa_order_x": 14, "rcwa_order_y": 7}}]
    target = {"rcwa_order_x": 22, "rcwa_order_y": 11}
    prediction = operator.predict_validation_memory(target, samples, floor_bytes=0)
    expected = 4 * 1024**3 * (operator.harmonic_count(target) / operator.harmonic_count(samples[-1]["fidelity"])) ** 2
    assert prediction["predicted_peak_bytes"] == pytest.approx(expected, abs=1)
    assert prediction["memory_headroom_bytes"] == 4 * 1024**3
    assert prediction["measured_samples"] == 1
