"""Commands can commit once, and committed controls survive failed delivery."""
import pytest
import time

from optimization_framework.contracts.requests import CampaignInput, TaskInput, TrialInput, ControlInput
from optimization_framework.execution.service import Workspace
from optimization_framework.storage.sqlite import Store, read_json


def prepare(tmp_path):
    workspace = Workspace(tmp_path)
    campaign = workspace.create_campaign(CampaignInput(name="Execution", compute_budget_seconds=200, validation_reserve_seconds=0,
        tasks=[TaskInput(name="Quadratic", problem_id="bounded_continuous", configuration={})]))
    task = workspace.current_tasks(campaign["id"])[0]
    request = TrialInput(campaign_id=campaign["id"], task_id=task["id"], algorithm="coordinate", max_steps=5, wall_seconds=10)
    return workspace, request


def test_nested_repository_transactions_roll_back_all_related_records(tmp_path):
    store = Store(tmp_path)
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.put("test", {"id": "a"}, "created")
            with store.transaction():
                store.put("test", {"id": "b"}, "created")
            raise RuntimeError("Simulated failure before commit")
    assert store.list("test") == [] and store.events() == []


def test_control_outbox_retries_latest_intent_after_delivery_failure(tmp_path, monkeypatch):
    workspace, request = prepare(tmp_path)
    trial = workspace.create_trial(request)
    with monkeypatch.context() as patch:
        def fail(_):
            raise OSError("Temporarily unavailable control path")
        patch.setattr(workspace, "_write_control", fail)
        workspace.control(trial["id"], ControlInput(action="pause"))
    assert workspace.store.get(trial["id"], "trial")["status"] == "paused"
    assert workspace.store.list("outbox")[0]["status"] == "pending"
    workspace.control(trial["id"], ControlInput(action="stop"))
    control = read_json(workspace.job_dir(trial["id"]) / "control.json")
    assert control["command"] == "stop"
    assert all(item["status"] == "completed" for item in workspace.store.list("outbox"))
    assert len(workspace.store.list("manager_issue", trial["campaign_id"])) == 1


def test_transaction_rollback_never_delivers_an_uncommitted_worker_control(tmp_path):
    workspace, request = prepare(tmp_path)
    trial = workspace.create_trial(request)
    with pytest.raises(RuntimeError):
        with workspace.store.transaction():
            workspace.control(trial["id"], ControlInput(action="stop"))
            assert not (workspace.job_dir(trial["id"]) / "control.json").exists()
            raise RuntimeError("Command could not commit")
    assert workspace.store.get(trial["id"], "trial")["status"] == "queued"
    assert workspace.store.list("outbox") == []


def test_dependencies_require_scientific_completion_not_a_clean_exit(tmp_path):
    workspace, request = prepare(tmp_path)
    parent = workspace.create_trial(request)
    child = workspace.create_trial(request.model_copy(update={"dependencies": [parent["id"]]}))
    assert not workspace.dependencies_ready(child)
    parent.update(status="completed", result={"scientific_complete": False, "process_exit": 0})
    workspace.store.put("trial", parent)
    assert not workspace.dependencies_ready(child)
    parent["result"]["scientific_complete"] = True
    workspace.store.put("trial", parent)
    assert workspace.dependencies_ready(child)
    assert child["experiment_spec"]["dependencies"] == [parent["id"]]


def test_worker_lease_adopts_child_when_pid_commit_was_lost(tmp_path):
    workspace, request = prepare(tmp_path)
    trial = workspace.create_trial(request.model_copy(update={"max_steps": 10000, "wall_seconds": 20}))
    workspace._start_trial(trial)
    process = workspace.processes.pop(trial["id"])
    try:
        deadline = time.monotonic() + 5
        lease_path = workspace.job_dir(trial["id"]) / "worker-lease.json"
        while not lease_path.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert lease_path.exists()
        lost = workspace.store.get(trial["id"], "trial")
        lost.pop("pid", None)
        lost.pop("process_identity", None)
        lost["lease_deadline"] = time.time() + 10
        workspace.store.put("trial", lost)
        workspace.reconcile()
        adopted = workspace.store.get(trial["id"], "trial")
        assert adopted["pid"] == process.pid
        assert adopted["attempt"] == 1
        workspace.control(trial["id"], ControlInput(action="stop"))
        process.wait(timeout=5)
        workspace.reconcile()
        assert workspace.store.get(trial["id"], "trial")["status"] == "stopped"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_derived_trial_queries_follow_commits_and_rollbacks(tmp_path):
    store = Store(tmp_path)
    trial = {"id": "trial_a", "campaign_id": "c", "status": "completed", "execution_contract": 1,
             "attempt": 1, "algorithm": "coordinate", "seed": 0}
    assert store.trials_requiring_capture() == [] and store.list_trial_headers() == []
    store.put("trial", trial)
    assert [row["id"] for row in store.trials_requiring_capture()] == ["trial_a"]
    assert store.list_trial_headers()[0]["algorithm"] == "coordinate"
    store.trials_requiring_capture()[0]["status"] = "mutated by a caller"
    assert store.trials_requiring_capture()[0]["status"] == "completed"
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.put("trial", {**trial, "asset_capture_attempt": 1})
            assert store.trials_requiring_capture() == []
            raise RuntimeError("Simulated failure before commit")
    assert [row["id"] for row in store.trials_requiring_capture()] == ["trial_a"]
    store.put("trial", {**trial, "asset_capture_attempt": 1})
    assert store.trials_requiring_capture() == []


def test_runs_waiting_on_a_time_capped_run_offer_the_researcher_a_way_on(tmp_path):
    workspace, request = prepare(tmp_path)
    parent = workspace.create_trial(request)
    first = workspace.create_trial(request.model_copy(update={"dependencies": [parent["id"]]}))
    second = workspace.create_trial(request.model_copy(update={"dependencies": [first["id"]]}))
    parent.update(status="completed", reason="wall_budget_exhausted", result={"scientific_complete": False, "allocation_stop": "wall_budget_exhausted"})
    workspace.store.put("trial", parent)
    assert not workspace.dependencies_ready(first)
    issue = next(row for row in workspace.store.list("manager_issue") if row["code"] == "dependency_incomplete")
    assert issue["affected"] == first["id"] and "wall budget exhausted" in issue["message"] and "1 run(s) queued after it" in issue["message"]
    assert [item["id"] for item in issue["actions"]] == ["start_anyway", "stop"]
    # An issue never holds a stop or a pause: an agent can always take back work it queued.
    from types import SimpleNamespace
    from optimization_framework.campaigns.inbox import check_issues
    check_issues(workspace, SimpleNamespace(operation="trial.control", payload={"trial_id": first["id"], "action": "pause"}, id="c1", campaign_id=first["campaign_id"]))
    with pytest.raises(ValueError, match="pending manager issue"):
        check_issues(workspace, SimpleNamespace(operation="trial.control", payload={"trial_id": first["id"], "action": "resume"}, id="c2", campaign_id=first["campaign_id"]))
    workspace.control(first["id"], ControlInput(action="pause"))
    with pytest.raises(ValueError, match="one of this issue's actions"):
        workspace.memory.resolve_issue(issue["id"], "action", "", action="delete_everything")
    resolved = workspace.memory.resolve_issue(issue["id"], "action", "", action="start_anyway")
    assert resolved["status"] == "resolved" and resolved["action"]["trial_ids"] == [first["id"], second["id"]]
    first = workspace.store.get(first["id"], "trial")
    assert first["status"] == "queued" and first["dependency_completion"] == "finished" and workspace.dependencies_ready(first)
    # The order stays: the second still waits for the first to end.
    assert not workspace.dependencies_ready(workspace.store.get(second["id"], "trial"))


def test_stopping_the_runs_that_wait_on_an_incomplete_run(tmp_path):
    workspace, request = prepare(tmp_path)
    parent = workspace.create_trial(request)
    child = workspace.create_trial(request.model_copy(update={"dependencies": [parent["id"]]}))
    parent.update(status="failed", reason="worker crashed")
    workspace.store.put("trial", parent)
    workspace.dependencies_ready(child)
    issue = next(row for row in workspace.store.list("manager_issue") if row["code"] == "dependency_incomplete")
    workspace.memory.resolve_issue(issue["id"], "action", "", action="stop")
    assert workspace.store.get(child["id"], "trial")["status"] == "stopped"
