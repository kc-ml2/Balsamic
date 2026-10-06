"""Researcher ideas and saved feedback have durable independent command identities."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from optimization_framework.api.app import create_app
from optimization_framework.contracts.commands import Command
from optimization_framework.contracts.requests import CampaignInput, TaskInput
from optimization_framework.execution.service import Workspace


def prepare(path):
    workspace = Workspace(path)
    campaign = workspace.create_campaign(CampaignInput(name="Idea commands", autonomy="delegated", compute_budget_seconds=100,
        validation_reserve_seconds=0, tasks=[TaskInput(name="Quadratic", problem_id="bounded_continuous")]))
    return workspace, campaign


def request(workspace, campaign, operation, payload, identity):
    return Command(id=identity, campaign_id=campaign["id"], expected_revision=workspace.store.get(campaign["id"], "campaign")["version"],
        operation=operation, payload=payload)


def idea(workspace, campaign, **changes):
    return request(workspace, campaign, "hypothesis.create", {"title": "Specialized search", "mechanism": "Use a stagnation detector",
        "rationale": "Test the restart trigger independently", "algorithm": "", **changes}, "create_idea")


def test_missing_code_idea_is_saved_once_with_explicit_origin_and_parent_scope(tmp_path):
    workspace, campaign = prepare(tmp_path)
    command = idea(workspace, campaign)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(workspace.commands.execute, [command, command]))
    assert results[0] == results[1]
    record = results[0]["outcome"]["hypothesis"]
    assert record["origin"] == "researcher" and record["claim_level"] == "rationale_only"
    assert record["status"] == "proposed" and not record["executable"]
    assert not workspace.implementations.readiness(record)["runnable"]
    assert workspace.store.list("trial") == workspace.store.list("research_run") == []
    assert workspace.memory.state(campaign["id"])["guidance_revision"] == 1
    assert Workspace(tmp_path).commands.execute(command) == results[0]
    other = workspace.create_campaign(CampaignInput(name="Other", tasks=[TaskInput(name="Other", problem_id="bounded_continuous")]))
    with pytest.raises(ValueError, match="Parents must belong"):
        workspace.commands.execute(request(workspace, other, "hypothesis.create", {**command.payload, "parent_ids": [record["id"]]}, "cross_campaign"))
    with pytest.raises(ValueError, match="different request"):
        workspace.commands.execute(command.model_copy(update={"payload": {**command.payload, "title": "A changed idea"}}))
    assert len([h for h in workspace.store.list("hypothesis") if h["id"] == record["id"]]) == 1


def test_feedback_appends_once_concurrently_and_invalidates_old_manager_guidance(tmp_path):
    workspace, campaign = prepare(tmp_path)
    target = workspace.commands.execute(idea(workspace, campaign))["outcome"]["hypothesis"]
    stale = request(workspace, campaign, "trial.create", {"task_id": workspace.current_tasks(campaign["id"])[0]["id"],
        "algorithm": "coordinate", "max_steps": 2, "wall_seconds": 5}, "stale_manager").model_copy(update={
            "expected_guidance_revision": 1, "expected_authority_hash": workspace.commands.authority_hash(campaign)})
    first = request(workspace, campaign, "hypothesis.review", {"hypothesis_id": target["id"], "text": "Preserve the baseline."}, "first_comment")
    second = request(workspace, campaign, "hypothesis.review", {"hypothesis_id": target["id"], "text": "Explain the stopping rule."}, "second_comment")
    with ThreadPoolExecutor(max_workers=3) as pool:
        replies = list(pool.map(workspace.commands.execute, [first, second, first]))
    assert replies[0] == replies[2]
    reviews = workspace.store.get(target["id"], "hypothesis")["reviews"]
    assert {row["text"] for row in reviews} == {first.payload["text"], second.payload["text"]}
    assert all(row["author"] == "researcher" for row in reviews)
    assert workspace.memory.state(campaign["id"])["guidance_revision"] == 3
    with pytest.raises(ValueError, match="guidance changed"):
        workspace.commands.execute(stale, actor="manager")
    assert workspace.store.list("research_run") == workspace.store.list("manager_command") == workspace.store.list("trial") == []
    restarted = Workspace(tmp_path)
    assert restarted.commands.execute(first) == replies[0]
    assert restarted.memory.state(campaign["id"])["guidance_revision"] == 3
    # An agent may comment, but its note is attributed to it and is not researcher guidance.
    restarted.commands.execute(first.model_copy(update={"id": "agent_comment", "expected_guidance_revision": 3,
        "expected_authority_hash": restarted.commands.authority_hash(campaign)}), actor="manager")
    reviews = restarted.store.get(target["id"], "hypothesis")["reviews"]
    assert [row["author"] for row in reviews].count("manager") == 1
    assert restarted.memory.state(campaign["id"])["guidance_revision"] == 3


def test_status_revisions_reject_aba_edits_and_nomination_remains_frozen(tmp_path):
    workspace, campaign = prepare(tmp_path)
    target = workspace.commands.execute(idea(workspace, campaign, algorithm="coordinate"))["outcome"]["hypothesis"]
    def status(value, revision, identity):
        return request(workspace, campaign, "hypothesis.status", {"hypothesis_id": target["id"], "status": value,
            "expected_status_revision": revision}, identity)
    workspace.commands.execute(status("archived", 0, "archive"))
    workspace.commands.execute(status("proposed", 1, "revive"))
    with pytest.raises(ValueError, match="Idea status changed"):
        workspace.commands.execute(status("finalist", 0, "stale_nomination"))
    nomination = workspace.commands.execute(status("finalist", 2, "nominate"))
    frozen = nomination["outcome"]["hypothesis"]
    assert frozen["status_revision"] == 3 and frozen["frozen_identity"]["algorithm"] == "coordinate"
    workspace.commands.execute(status("archived", 3, "archive_finalist"))
    assert workspace.commands.execute(nomination["request"]) == nomination
    current = workspace.store.get(target["id"], "hypothesis")
    assert current["status"] == "archived" and current["frozen_identity"] == frozen["frozen_identity"]
    assert current["frozen_at"] == frozen["frozen_at"]


def test_failed_feedback_transaction_leaves_no_comment_guidance_or_projection(tmp_path, monkeypatch):
    workspace, campaign = prepare(tmp_path)
    target = workspace.commands.execute(idea(workspace, campaign))["outcome"]["hypothesis"]
    before = workspace.memory.state(campaign["id"])
    projection = tmp_path / "campaigns" / campaign["id"] / "manager/context.md"
    text = projection.read_text()
    prior_outbox = workspace.store.list("outbox")
    apply = workspace.commands._apply
    def fail(command, actor):
        apply(command, actor)
        raise RuntimeError("Failed before command acceptance")
    monkeypatch.setattr(workspace.commands, "_apply", fail)
    with pytest.raises(RuntimeError):
        workspace.commands.execute(request(workspace, campaign, "hypothesis.review", {"hypothesis_id": target["id"], "text": "Keep this draft."}, "feedback"))
    assert workspace.store.get(target["id"], "hypothesis") == target
    assert workspace.memory.state(campaign["id"])["guidance_revision"] == before["guidance_revision"]
    assert workspace.store.list("outbox") == prior_outbox
    assert projection.read_text() == text


def test_implementation_binding_is_prepared_before_transaction_and_not_repeated_on_retry(tmp_path, monkeypatch):
    workspace, campaign = prepare(tmp_path)
    prepared = []
    def prepare_version(version_id, task, parameters):
        assert not workspace.store.in_transaction
        prepared.append(version_id)
        return {"version": {"id": version_id, "exposed_conditions": []}}
    monkeypatch.setattr(workspace.implementations, "prepare", prepare_version)
    command = idea(workspace, campaign, implementation_version_id="version_example")
    accepted = workspace.commands.execute(command)
    assert accepted["outcome"]["hypothesis"]["algorithm"] == "package"
    assert workspace.commands.execute(command) == accepted
    assert prepared == ["version_example"]


def test_compatibility_feedback_and_status_retries_preserve_saved_response(tmp_path, monkeypatch):
    monkeypatch.setenv("GRATING_LLM_DISABLED", "true")
    app = create_app(tmp_path, start_workers=False)
    workspace = app.state.workspace
    campaign = workspace.create_campaign(CampaignInput(name="Feedback HTTP", tasks=[TaskInput(name="Quadratic", problem_id="bounded_continuous")]))
    with TestClient(app) as client:
        body = {"campaign_id": campaign["id"], "title": "A useful idea", "mechanism": "A bounded search", "rationale": "Compare a restart rule", "algorithm": "coordinate"}
        created = client.post("/api/hypotheses", json=body, headers={"Idempotency-Key": "idea"})
        assert created.status_code == 201, created.text
        target = created.json()
        review = f"/api/hypotheses/{target['id']}/review"
        first = client.post(review, json={"text": "Keep the baseline fixed."}, headers={"Idempotency-Key": "comment"})
        assert first.status_code == 200, first.text
        assert client.post(review, json={"text": "Use a second seed."}).status_code == 200
        status = f"/api/hypotheses/{target['id']}/status"
        archived = client.post(status, json={"status": "archived"}, headers={"Idempotency-Key": "archive", "X-Status-Revision": "0"})
        assert archived.status_code == 200
        assert client.post(status, json={"status": "proposed"}, headers={"X-Status-Revision": "0"}).status_code == 409
        assert client.post(status, json={"status": "proposed"}).status_code == 200
        assert client.post(status, json={"status": "archived"}, headers={"Idempotency-Key": "archive"}).json() == archived.json()
        assert client.post(review, json={"text": "Keep the baseline fixed."}, headers={"Idempotency-Key": "comment"}).json() == first.json()
        assert client.post("/api/hypotheses", json=body, headers={"Idempotency-Key": "idea"}).json() == target
        assert client.post(review, json={"text": "A changed comment"}, headers={"Idempotency-Key": "comment"}).status_code == 409
        assert client.post(review, json={"text": "Forged", "author": "agent"}).status_code == 422
    assert len(workspace.store.get(target["id"], "hypothesis")["reviews"]) == 2
    assert workspace.store.get(target["id"], "hypothesis")["status"] == "proposed"
    assert workspace.store.list("research_run") == []
