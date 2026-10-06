"""Scientific controls have one authority, frozen procedures and stable replies."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from optimization_framework.api.app import create_app
from optimization_framework.contracts.base import content_hash
from optimization_framework.contracts.commands import Command
from optimization_framework.contracts.requests import CampaignInput, TaskInput
from optimization_framework.execution.service import Workspace
from optimization_framework.storage.sqlite import read_json


def prepare(directory, *, meent=False):
    workspace = Workspace(directory)
    task = (TaskInput(name="Small grating", physics={"n_cells": 4, "fourier_order": 1}) if meent
        else TaskInput(name="Quadratic", problem_id="bounded_continuous", configuration={}))
    campaign = workspace.create_campaign(CampaignInput(name="Command lifecycle", compute_budget_seconds=200,
        validation_reserve_seconds=0, autonomy="delegated", tasks=[task]))
    task = workspace.current_tasks(campaign["id"])[0]
    request = command(workspace, campaign["id"], "trial.create", {
        "task_id": task["id"], "algorithm": "random" if meent else "coordinate", "max_steps": 3, "wall_seconds": 10}, "create_trial")
    trial = workspace.commands.execute(request)["outcome"]["trial"]
    return workspace, campaign, trial


def command(workspace, campaign_id, operation, payload, identity):
    return Command(id=identity, campaign_id=campaign_id, operation=operation,
        expected_revision=workspace.store.get(campaign_id, "campaign")["version"], payload=payload)


def control(workspace, trial, action, identity, **values):
    return command(workspace, trial["campaign_id"], "trial.control", {
        "trial_id": trial["id"], "expected_control_revision": trial["control_revision"], "action": action, **values}, identity)


def test_control_revisions_concurrent_replay_and_amendments_preserve_science(tmp_path):
    workspace, campaign, original = prepare(tmp_path)
    frozen = workspace.store.get(original["experiment_spec_id"], "experiment_spec")
    pause = control(workspace, original, "pause", "pause_once")
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(workspace.commands.execute, [pause, pause]))
    assert replies[0] == replies[1]
    paused = replies[0]["outcome"]["trial"]
    assert paused["status"] == "paused" and paused["control_revision"] == 1
    with pytest.raises(ValueError, match="controls changed"):
        workspace.commands.execute(control(workspace, original, "extend", "stale_extend", wall_seconds=15))
    extended = workspace.commands.execute(control(workspace, paused, "extend", "extend_once", max_steps=6,
        wall_seconds=15, rationale="Authorize another bounded segment"))
    assert extended["outcome"]["trial"]["status"] == "queued"
    assert workspace.store.get(original["experiment_spec_id"], "experiment_spec") == frozen
    assert extended["outcome"]["trial"]["schedule_steps"] == original["schedule_steps"]
    assert len(workspace.store.list("budget_amendment")) == 1
    workspace.commands.execute(command(workspace, campaign["id"], "campaign.update", {"name": "Later charter"}, "rename"))
    restarted = Workspace(tmp_path)
    assert restarted.commands.execute(pause) == replies[0]
    assert restarted.commands.execute(extended["request"]) == extended
    assert restarted.store.get(original["id"], "trial")["control_revision"] == 2
    assert len(restarted.store.list("budget_amendment")) == 1
    text = workspace.memory._text("work_command", replies[0])
    assert '"operation": "trial.control"' in text
    assert "experiment_spec" not in text and "execution_manifest" not in text


def test_controls_cannot_target_another_campaign_or_escalate_manager_authority(tmp_path):
    workspace, campaign, trial = prepare(tmp_path)
    other = workspace.create_campaign(CampaignInput(name="Another campaign", tasks=[
        TaskInput(name="Other quadratic", problem_id="bounded_continuous", configuration={})]))
    request = control(workspace, trial, "stop", "wrong_campaign")
    with pytest.raises(ValueError, match="another campaign"):
        workspace.commands.execute(request.model_copy(update={"campaign_id": other["id"], "expected_revision": other["version"]}))
    pinned = {"expected_guidance_revision": 0, "expected_authority_hash": workspace.commands.authority_hash(campaign)}
    request = request.model_copy(update=pinned)
    # The manager may stop or pause within an allocation, but only the researcher enlarges one.
    with pytest.raises(ValueError, match="need researcher approval"):
        workspace.commands.execute(control(workspace, trial, "extend", "agent_escalation", wall_seconds=20)
            .model_copy(update=pinned), actor="manager")
    with pytest.raises(ValueError, match="expected_control_revision"):
        workspace.commands.execute(request.model_copy(update={"payload": {"trial_id": trial["id"], "action": "pause"}}))
    assert workspace.store.get(trial["id"], "trial")["status"] == "queued"
    assert workspace.store.list("outbox") == []
    assert len(workspace.store.list("command_rejection")) == 3
    filed = workspace.commands.execute(command(workspace, campaign["id"], "trial.extension_request", {"trial_id": trial["id"],
        "additional_seconds": 20, "rationale": "The curve is still improving at the time limit."}, "agent_extension_request")
        .model_copy(update=pinned), actor="manager")
    decision = workspace.store.get(filed["outcome"]["decision_id"], "decision")
    assert decision["requested_by"] == "manager" and decision["status"] == "pending"
    assert workspace.store.get(trial["id"], "trial")["wall_seconds"] == 10
    workspace.commands.execute(command(workspace, campaign["id"], "decision.resolve", {"decision_id": decision["id"],
        "choice": "0", "comment": "Worth it", "expected_resolution_revision": 0}, "approve_extension"))
    assert workspace.store.get(trial["id"], "trial")["wall_seconds"] == 30
    assert [row["authority"] for row in workspace.store.list("budget_amendment")] == ["researcher"]
    current = {"expected_guidance_revision": workspace.memory.state(campaign["id"])["guidance_revision"],
        "expected_authority_hash": workspace.commands.authority_hash(workspace.store.get(campaign["id"], "campaign"))}
    workspace.commands.execute(control(workspace, workspace.store.get(trial["id"], "trial"), "stop", "agent_stop")
        .model_copy(update=current), actor="manager")
    stopped = workspace.store.get(trial["id"], "trial")
    assert (stopped["status"], stopped["stopped_by"], stopped["reason"]) == ("stopped", "manager", "Stopped by the campaign manager")
    schemas = workspace.commands.describe()
    assert "expected_control_revision" in schemas["trial.control"]["payload_schema"]["required"]
    assert not schemas["trial.validate"]["delegable"]


def test_control_commit_and_restart_delivery_are_one_effect(tmp_path, monkeypatch):
    workspace, _, trial = prepare(tmp_path)
    pause = control(workspace, trial, "pause", "pause_once")
    apply = workspace.commands._apply
    def fail(request, actor):
        apply(request, actor)
        raise RuntimeError("Lost the transaction before committing the reply")
    with monkeypatch.context() as patch:
        patch.setattr(workspace.commands, "_apply", fail)
        with pytest.raises(RuntimeError):
            workspace.commands.execute(pause)
    assert workspace.store.get(trial["id"], "trial") == trial
    assert workspace.store.list("outbox") == []
    assert not (workspace.job_dir(trial["id"]) / "control.json").exists()
    with monkeypatch.context() as patch:
        patch.setattr(workspace, "dispatch_outbox", lambda: None)
        accepted = workspace.commands.execute(pause)
    assert workspace.store.list("outbox")[0]["status"] == "pending"
    restarted = Workspace(tmp_path)
    restarted.dispatch_outbox()
    assert read_json(restarted.job_dir(trial["id"]) / "control.json")["command"] == "pause"
    assert restarted.commands.execute(pause) == accepted
    assert len(restarted.store.list("outbox")) == 1
    assert restarted.store.list("outbox")[0]["status"] == "completed"
    assert restarted.store.list("execution_attempt") == []


def test_http_create_control_and_study_keep_original_replies_after_newer_edits(tmp_path, monkeypatch):
    monkeypatch.setenv("GRATING_LLM_DISABLED", "true")
    app = create_app(tmp_path, start_workers=False)
    workspace = app.state.workspace
    campaign = workspace.create_campaign(CampaignInput(name="HTTP controls", compute_budget_seconds=200, validation_reserve_seconds=0,
        tasks=[TaskInput(name="Quadratic", problem_id="bounded_continuous", configuration={})]))
    task = workspace.current_tasks(campaign["id"])[0]
    payload = {"campaign_id": campaign["id"], "task_id": task["id"], "algorithm": "coordinate", "wall_seconds": 10, "max_steps": 2}
    with TestClient(app) as client:
        first = client.post("/api/trials", json=payload, headers={"Idempotency-Key": "create"})
        assert first.status_code == 201, first.text
        original = first.json()
        path = f"/api/trials/{original['id']}/control"
        paused = client.post(path, json={"action": "pause"}, headers={"Idempotency-Key": "pause", "X-Control-Revision": "0"})
        assert paused.status_code == 200, paused.text
        stale = client.post(path, json={"action": "resume"}, headers={"X-Control-Revision": "0"})
        assert stale.status_code == 409
        assert client.post(path, json={"action": "stop"}).json()["status"] == "stopped"
        study_path = f"/api/v1/campaigns/{campaign['id']}/studies"
        study = client.post(study_path, json={"goal": "A new declared comparison"}, headers={"Idempotency-Key": "study"})
        assert study.status_code == 201, study.text
        assert study.json()["parent_study_id"] == campaign["active_study_id"]
        assert workspace.store.get(campaign["id"], "campaign")["version"] == 2
        assert client.post(study_path, json={"goal": "A new declared comparison"}, headers={"Idempotency-Key": "study"}).json() == study.json()
        assert client.post("/api/trials", json=payload, headers={"Idempotency-Key": "create"}).json() == original
        assert client.post(path, json={"action": "pause"}, headers={"Idempotency-Key": "pause"}).json() == paused.json()
        assert client.post("/api/trials", json={**payload, "seed": 9}, headers={"Idempotency-Key": "create"}).status_code == 409
        assert client.post(study_path, json={"goal": "Stale science"}, headers={"X-Campaign-Revision": "1"}).status_code == 409
    assert len(workspace.store.list("trial")) == 1
    assert len(workspace.store.list("study")) == 2
    assert workspace.store.get(original["id"], "trial")["experiment_spec_hash"] == original["experiment_spec_hash"]


@pytest.mark.parametrize("route,operation,payload", [
    ("validate", "trial.validate", {"orders": [1, 2], "max_designs": 1, "wall_seconds": 10}),
    ("recipes", "validation.run", {"recipe_id": "fourier_convergence:v1", "parameters": {"orders": [1, 2], "tolerance": .005}, "wall_seconds": 10}),
])
def test_validation_routes_pin_captured_compiler_and_replay_original_subjects(tmp_path, monkeypatch, route, operation, payload):
    from optimization_framework.execution.worker import run
    from optimization_framework.evaluation import recipes
    workspace, campaign, trial = prepare(tmp_path, meent=True)
    result = run(workspace.job_dir(trial["id"]))
    assert result["status"] == "completed" and result["scientific_complete"]
    trial.update(status="completed", progress=result, result=result)
    workspace.store.put("trial", trial)
    original_spec = deepcopy(trial["experiment_spec"])
    monkeypatch.setenv("GRATING_LLM_DISABLED", "true")
    app = create_app(tmp_path, start_workers=False)
    def changed_compiler(*args, **kwargs):
        raise AssertionError("An installed compiler cannot replace this experiment's captured one")
    monkeypatch.setattr(recipes, "compile_recipe", changed_compiler)
    with TestClient(app) as client:
        path = f"/api/trials/{trial['id']}/{route}"
        first = client.post(path, json=payload, headers={"Idempotency-Key": "validate"})
        assert first.status_code == 201, first.text
        child = first.json()
        assert child["source_trial_id"] == trial["id"]
        assert child["execution_manifest"] == trial["execution_manifest"]
        assert child["study_id"] == trial["study_id"]
        assert child["recipe"]["subject_asset_ids"]
        child_result = run(workspace.job_dir(child["id"]))
        assert child_result["status"] == "completed" and child_result["scientific_complete"]
        assert child_result["evaluations"] == 2
        trial["progress"]["archive"] = [{"candidate": [1, 1, 1, 1], "objective": 0.0}]
        workspace.store.put("trial", trial)
        assert client.post(path, json=payload, headers={"Idempotency-Key": "validate"}).json() == child
    assert len(workspace.store.list("trial")) == 2
    assert workspace.store.get(trial["id"], "trial")["experiment_spec"] == original_spec
    receipt = next(row for row in workspace.store.list("work_command") if row["request"]["operation"] == operation)
    restarted = Workspace(tmp_path)
    assert restarted.commands.execute(receipt["request"]) == receipt
    assert len(restarted.store.list("trial")) == 2
    assert restarted.commands.execute(receipt["request"])["outcome"]["trial"]["recipe"] == child["recipe"]


def test_historical_control_envelope_and_outcome_replay_without_new_fields(tmp_path):
    workspace, campaign, trial = prepare(tmp_path)
    request = command(workspace, campaign["id"], "trial.control", {"trial_id": trial["id"], "action": "pause"}, "historical")
    envelope = request.model_dump(mode="json")
    accepted = {"id": request.id, "campaign_id": campaign["id"], "request": envelope,
        "request_hash": content_hash({"command": envelope, "actor": "researcher"}), "actor": "researcher",
        "status": "completed", "outcome": {"trial_id": trial["id"], "control_revision": 1}}
    workspace.store.put("work_command", accepted)
    assert Workspace(tmp_path).commands.execute(envelope) == accepted
    assert workspace.store.get(trial["id"], "trial")["control_revision"] == 0
    assert workspace.store.list("outbox") == []


def test_browser_requests_bind_the_workspace_across_restarts_and_port_reuse(tmp_path, monkeypatch):
    monkeypatch.setenv("GRATING_LLM_DISABLED", "true")
    first = create_app(tmp_path / "first", start_workers=False)
    with TestClient(first) as client:
        identity = client.get("/api/state").json()["workspace_id"]
    restarted = create_app(tmp_path / "first", start_workers=False)
    with TestClient(restarted) as client:
        assert client.get("/api/state").json()["workspace_id"] == identity
    second = create_app(tmp_path / "second", start_workers=False)
    with TestClient(second) as client:
        assert client.get("/api/state").json()["workspace_id"] != identity
        request = {"id": "old_workspace_create", "campaign_id": "requested_campaign", "expected_revision": 0,
            "operation": "campaign.create", "payload": {"name": "Retain original ownership", "tasks": [
                {"name": "Quadratic", "problem_id": "bounded_continuous"}]}}
        assert client.post("/api/v1/commands", json=request, headers={"X-Workspace-Id": identity}).status_code == 412
        assert client.get("/api/v1/commands/old_workspace_create", headers={"X-Workspace-Id": identity}).status_code == 412
    assert second.state.workspace.store.list("campaign") == []
    assert second.state.workspace.store.list("work_command") == []
