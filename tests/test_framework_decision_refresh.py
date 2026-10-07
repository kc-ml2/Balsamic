"""Reconsideration retains historical authority and never doubles as approval."""
from copy import deepcopy

import pytest

from optimization_framework.campaigns.decisions import public_decision
from optimization_framework.campaigns.manager import CampaignManager
from optimization_framework.contracts.commands import Command, DecisionRefreshInput
from optimization_framework.contracts.requests import CampaignInput, CampaignUpdate, ResearchInput, TaskInput
from optimization_framework.execution.service import Workspace
from optimization_framework.research.engine import _initial_roles, _safe_context
from optimization_framework.research.lifecycle import finalize, save_result
from optimization_framework.storage.sqlite import now


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    monkeypatch.setenv("GRATING_LLM_PROVIDER", "codex")
    monkeypatch.setenv("GRATING_LLM_DISABLED", "true")
    from optimization_framework.research.providers import provider_status
    frozen_provider = {**provider_status(), "configured": True}
    monkeypatch.setattr("optimization_framework.research.coordinator.provider_status", lambda: deepcopy(frozen_provider))
    workspace = Workspace(tmp_path)
    campaign = workspace.create_campaign(CampaignInput(name="Current decisions", autonomy="delegated",
        tasks=[TaskInput(name="Quadratic", problem_id="bounded_continuous")]))
    manager = CampaignManager(workspace)
    attempts = []
    monkeypatch.setattr(manager, "_thread", lambda run: attempts.append(run["id"]))
    return workspace, campaign, manager, attempts


def command(workspace, campaign, operation, payload, identity):
    current = workspace.store.get(campaign["id"], "campaign")
    return Command(id=identity, campaign_id=campaign["id"], operation=operation,
        expected_revision=current["version"], expected_guidance_revision=workspace.memory.state(campaign["id"])["guidance_revision"],
        expected_authority_hash=workspace.commands.authority_hash(current), payload=payload)


def proposal(workspace, campaign, identity="old", *, stale=True, plan=False):
    task = workspace.current_tasks(campaign["id"])[0]
    action = {"id": "action_" + identity, "campaign_id": campaign["id"], "charter_version": campaign["version"],
        "guidance_revision": 0, "status": "proposed", "kind": "probe" if plan else "command",
        "title": "Measure a strategy", "rationale": "Earlier scientific reasoning", "requires_researcher": True,
        "command_operation": "trial.create", "command_payload": {"task_id": task["id"], "algorithm": "coordinate", "max_steps": 2, "wall_seconds": 5}}
    if plan:
        action["probe_scope"] = "plan"
    decision = {"id": "decision_" + identity, "campaign_id": campaign["id"], "charter_version": campaign["version"],
        "action_id": action["id"], "title": action["title"], "context": "Keep this exact original explanation.",
        "status": "pending", "created_at": now(), "resolution_revision": 0,
        "options": [{"id": "accept", "label": "Proceed"}, {"id": "defer", "label": "Defer"}, {"id": "reject", "label": "Decline"}]}
    workspace.store.put("action", action)
    workspace.store.put("decision", decision)
    if stale and workspace.store.get(campaign["id"], "campaign")["version"] == campaign["version"]:
        workspace.update_campaign(campaign["id"], CampaignUpdate(objective="Current scientific scope"))
    return decision, action


def refresh_payload(*decisions):
    return {"decisions": [{"decision_id": d["id"], "expected_resolution_revision": d.get("resolution_revision", 0),
        "desired_choice": "accept", "comment": "Keep the original reserve; refine the test."} for d in decisions],
        "comment": "Compare these proposals with completed measurements."}


def test_batch_is_serialized_idempotent_and_keeps_exact_originals(prepared):
    workspace, campaign, manager, attempts = prepared
    first, action = proposal(workspace, campaign)
    second, _ = proposal(workspace, campaign, "second")
    payload = refresh_payload(first, second)
    request = command(workspace, campaign, "decision.refresh", payload, "refresh_batch")
    accepted = workspace.commands.execute(request)
    outcome = accepted["outcome"]
    assert len(attempts) == 1
    run = workspace.store.get(attempts[0], "research_run")
    record = workspace.store.get(outcome["refresh_id"], "decision_refresh")
    assert record["decisions"][0]["decision"] == first
    assert record["decisions"][0]["action"] == action
    assert record["decisions"][0]["desired_choice"] == "accept"
    assert record["decisions"][0]["comment"] == payload["decisions"][0]["comment"]
    assert record["comment"] == payload["comment"]
    assert _safe_context(run["context_snapshot"])["decision_refresh"]["decisions"][0]["decision"] == first
    assert run["request"]["max_calls"] == 2
    assert _initial_roles(run["request"]["mode"], run["context_snapshot"]) == ["comparative_reviewer", "research_synthesizer"]
    assert workspace.commands.execute(request) == accepted
    again = workspace.commands.execute(command(workspace, campaign, "decision.refresh", payload, "duplicate_click"))
    assert again["outcome"]["manager_command_id"] == outcome["manager_command_id"]
    assert again["outcome"]["reused"]
    assert workspace.memory.state(campaign["id"])["guidance_revision"] == 1
    assert len(workspace.store.list("manager_command")) == 1
    assert not workspace.store.list("trial")
    assert workspace.store.get(action["id"], "action") == action
    view = public_decision(workspace, workspace.store.get(first["id"], "decision"))["freshness"]
    assert view["state"] == "updating" and not view["can_accept"] and not view["can_refresh"]
    assert view["blocked_choice_ids"] == ["accept"]


def test_queued_review_uses_current_guidance_and_reports_later_resolution(prepared):
    workspace, campaign, manager, attempts = prepared
    decision, _ = proposal(workspace, campaign)
    manager.start(ResearchInput(campaign_id=campaign["id"], message="Existing manager turn"))
    active = workspace.store.get(attempts[0], "research_run")
    accepted = workspace.commands.execute(command(workspace, campaign, "decision.refresh", refresh_payload(decision), "queued_review"))
    assert len(attempts) == 1
    workspace.commands.execute(command(workspace, campaign, "decision.resolve",
        {"decision_id": decision["id"], "expected_resolution_revision": 0, "choice": "defer", "comment": "Archive this direction for now."}, "defer_original"))
    active["status"] = "completed"
    workspace.store.put("research_run", active)
    manager.tick(campaign["id"])
    run = workspace.store.get(attempts[-1], "research_run")
    assert run["manager_command_id"] == accepted["outcome"]["manager_command_id"]
    assert run["guidance_revision"] == workspace.memory.state(campaign["id"])["guidance_revision"]
    item = run["context_snapshot"]["decision_refresh"]["decisions"][0]
    assert item["decision"]["status"] == "pending"
    assert item["current_status"] == "resolved"
    assert item["current_resolution"]["choice"] == "defer"


@pytest.mark.parametrize("status", ["completed", "awaiting_researcher"])
def test_valid_review_links_outputs_without_resolving_or_executing_original(prepared, status):
    workspace, campaign, manager, attempts = prepared
    decision, action = proposal(workspace, campaign)
    payload = refresh_payload(decision)
    workspace.commands.execute(command(workspace, campaign, "decision.refresh", payload, "review"))
    run_id = attempts[0]
    fresh = {**deepcopy(action), "id": "fresh_action", "requires_researcher": False}
    result = {"status": status, "mode": "llm", "usage": {"calls": 2}, "messages": [{"role": "assistant", "content": "Use this updated bounded experiment."}],
        "hypotheses": [], "decisions": [], "actions": [fresh],
        "trace": [{"role": "research_synthesizer", "status": "completed"}]}
    finalize(manager, run_id, save_result(workspace, run_id, result))
    original = workspace.store.get(decision["id"], "decision")
    assert original["status"] == "pending" and original["charter_version"] == decision["charter_version"]
    assert not workspace.store.list("trial")
    assert not [e for e in workspace.store.list("outbox") if e["kind"] == "manager_action"]
    assert workspace.store.get("fresh_action", "action")["requires_researcher"] is True
    fresh_decision = workspace.store.get("decision_fresh_action", "decision")
    assert fresh_decision["decision_refresh_id"] == original["refresh_id"]
    view = public_decision(workspace, original)["freshness"]
    assert view["state"] == "reviewed" and view["review_completed"]
    assert view["review_decision_ids"] == [fresh_decision["id"]]
    assert not view["can_refresh"] and not view["can_accept"]
    repeated = workspace.commands.execute(command(workspace, campaign, "decision.refresh", payload, "same_completed_review"))
    assert repeated["outcome"]["reused"] and len(attempts) == 1
    memory = workspace.memory.state(campaign["id"])
    memory["guidance_revision"] += 1
    workspace.store.put("manager_state", memory)
    assert public_decision(workspace, original)["freshness"]["can_refresh"]


def test_plan_only_fresh_action_requires_reconsideration_before_guidance_mutation(prepared):
    workspace, campaign, manager, attempts = prepared
    decision, _ = proposal(workspace, campaign, stale=False, plan=True)
    view = public_decision(workspace, decision)["freshness"]
    assert view["state"] == "blocked" and not view["stale"]
    assert view["can_refresh"] and view["blocked_choice_ids"] == ["accept"]
    with pytest.raises(ValueError):
        workspace.commands.execute(command(workspace, campaign, "decision.resolve",
            {"decision_id": decision["id"], "expected_resolution_revision": 0, "choice": "accept"}, "bad_accept"))
    assert workspace.memory.state(campaign["id"])["guidance_revision"] == 0
    assert not workspace.store.list("trial")
    workspace.commands.execute(command(workspace, campaign, "decision.refresh", refresh_payload(decision), "fix_design"))
    assert len(attempts) == 1


def test_stale_original_accept_stays_rejected_and_safe_decline_works(prepared):
    workspace, campaign, _, _ = prepared
    decision, _ = proposal(workspace, campaign)
    with pytest.raises(ValueError, match="older charter"):
        workspace.commands.execute(command(workspace, campaign, "decision.resolve",
            {"decision_id": decision["id"], "expected_resolution_revision": 0, "choice": "accept"}, "stale_accept"))
    workspace.commands.execute(command(workspace, campaign, "decision.resolve",
        {"decision_id": decision["id"], "expected_resolution_revision": 0, "choice": "reject"}, "decline"))
    assert workspace.store.get(decision["id"], "decision")["status"] == "resolved"
    assert not workspace.store.list("trial")


def test_cross_campaign_and_changed_decision_are_rejected_atomically(prepared):
    workspace, campaign, _, attempts = prepared
    decision, _ = proposal(workspace, campaign)
    second = workspace.create_campaign(CampaignInput(name="Other campaign", tasks=[TaskInput(name="Other", problem_id="bounded_continuous")]))
    with pytest.raises(ValueError, match="belonging"):
        workspace.commands.execute(command(workspace, second, "decision.refresh", refresh_payload(decision), "foreign"))
    payload = refresh_payload(decision)
    payload["decisions"][0]["expected_resolution_revision"] = 1
    with pytest.raises(ValueError, match="resolution changed"):
        workspace.commands.execute(command(workspace, campaign, "decision.refresh", payload, "changed"))
    assert not workspace.store.list("decision_refresh") and not attempts
    assert workspace.memory.state(campaign["id"])["guidance_revision"] == 0


def test_stale_guidance_uses_source_run_when_legacy_action_omitted_revision(prepared):
    workspace, campaign, _, _ = prepared
    decision, action = proposal(workspace, campaign, stale=False)
    action.pop("guidance_revision")
    workspace.store.put("action", action)
    decision["research_run_id"] = "source_run"
    workspace.store.put("decision", decision)
    workspace.store.put("research_run", {"id": "source_run", "campaign_id": campaign["id"], "guidance_revision": 0, "status": "completed"})
    memory = workspace.memory.state(campaign["id"])
    memory["guidance_revision"] = 1
    workspace.store.put("manager_state", memory)
    assert public_decision(workspace, decision)["freshness"]["stale"]
    with pytest.raises(ValueError):
        workspace.commands.execute(command(workspace, campaign, "decision.resolve",
            {"decision_id": decision["id"], "expected_resolution_revision": 0, "choice": "accept"}, "missing_guidance"))
    assert workspace.memory.state(campaign["id"])["guidance_revision"] == 1


def test_duplicate_selection_and_manager_authority_are_not_accepted(prepared):
    workspace, campaign, _, _ = prepared
    decision, _ = proposal(workspace, campaign)
    with pytest.raises(ValueError, match="only once"):
        DecisionRefreshInput(**refresh_payload(decision, decision))
    with pytest.raises(ValueError):
        workspace.commands.execute(command(workspace, campaign, "decision.refresh", refresh_payload(decision), "manager_refresh"), actor="manager")
    assert not workspace.store.list("decision_refresh")


def test_refresh_preserves_manager_synthesis_slot_when_reviewer_requests_more_roles(prepared, monkeypatch):
    from optimization_framework.research import engine
    from test_workspace_research import configure, mock_provider
    workspace, campaign, _, attempts = prepared
    decision, _ = proposal(workspace, campaign)
    workspace.commands.execute(command(workspace, campaign, "decision.refresh", refresh_payload(decision), "bounded_review"))
    run = workspace.store.get(attempts[0], "research_run")
    monkeypatch.delenv("GRATING_LLM_DISABLED")
    configure(monkeypatch)
    calls = mock_provider(monkeypatch, lambda payload, count: {
        "analysis": "Reconsidered the exact original decision under current authority.",
        "next_roles": ["problem_analyst", "experiment_designer"]})
    result = engine.run_research({"mode": "compare", "message": run["request"]["message"], "max_calls": 2}, run["context_snapshot"])
    assert len(calls) == 2
    assert [(step["role"], step["status"]) for step in result["trace"]] == [
        ("comparative_reviewer", "completed"), ("research_synthesizer", "completed")]


@pytest.mark.parametrize("kind", ["failed", "stale", "budget_without_manager"])
def test_failed_stale_or_unsynthesized_reviews_remain_retryable(prepared, kind):
    workspace, campaign, manager, attempts = prepared
    decision, _ = proposal(workspace, campaign)
    workspace.commands.execute(command(workspace, campaign, "decision.refresh", refresh_payload(decision), "old_review"))
    run = workspace.store.get(attempts[0], "research_run")
    if kind == "failed":
        run.update(status="failed", error="Provider failed")
        workspace.store.put("research_run", run)
    else:
        if kind == "stale":
            memory = workspace.memory.state(campaign["id"])
            memory["guidance_revision"] += 1
            workspace.store.put("manager_state", memory)
        finalize(manager, run["id"], save_result(workspace, run["id"], {
            "status": "awaiting_researcher", "mode": "llm", "usage": {"calls": 2},
            "messages": [{"role": "assistant", "content": "Partial analysis only"}],
            "hypotheses": [], "decisions": [], "actions": [],
            "trace": [{"role": "research_synthesizer", "status": "completed" if kind == "stale" else "budget_blocked"}]}))
    original = workspace.store.get(decision["id"], "decision")
    view = public_decision(workspace, original)["freshness"]
    assert view["state"] == "blocked" and view["can_refresh"]
    assert original["status"] == "pending"
    workspace.commands.execute(command(workspace, campaign, "decision.refresh", refresh_payload(decision), "retry_review"))
    assert len(attempts) == 2


def test_queued_review_rechecks_protected_evidence_before_provider_dispatch(prepared):
    workspace, campaign, manager, attempts = prepared
    decision, _ = proposal(workspace, campaign)
    manager.start(ResearchInput(campaign_id=campaign["id"], message="In-flight manager turn"))
    active = workspace.store.get(attempts[0], "research_run")
    accepted = workspace.commands.execute(command(workspace, campaign, "decision.refresh", refresh_payload(decision), "queued_protected"))
    current = workspace.store.get(decision["id"], "decision")
    current["locked"] = True
    workspace.store.put("decision", current)
    active["status"] = "completed"
    workspace.store.put("research_run", active)
    manager.tick(campaign["id"])
    turn = workspace.store.get(accepted["outcome"]["manager_command_id"], "manager_command")
    assert turn["status"] == "blocked" and "protected" in turn["error"]
    assert len(attempts) == 1


def test_selected_record_and_shared_text_projection_round_trips_exact_snapshots():
    from optimization_framework.research.context import _share_decision_context
    text = "Preserve the user's exact scientific qualification. " * 20
    original = {"id": "selected_decision", "status": "pending", "charter_version": 1,
        "rationale": text, "context": text, "options": [{"id": "accept", "description": text}],
        "removed_after_resolution": "historical field"}
    current = {**original, "status": "resolved", "choice": "defer", "comment": text,
        "resolution_revision": 1, "refresh_id": "refresh_record"}
    current.pop("removed_after_resolution")
    action = {"id": "action", "rationale": text, "charter_version": 1, "guidance_revision": 0}
    selected = {"decision": original, "action": action, "comment": text, "desired_choice": "accept"}
    context = {"decisions": [deepcopy(current)], "decision_refresh": {"decisions": [deepcopy(selected)]}}
    _share_decision_context(context)

    def expand(value):
        if isinstance(value, dict):
            if set(value) == {"$ref"}:
                cursor = context
                for token in value["$ref"][2:].split("/"):
                    cursor = cursor[int(token)] if isinstance(cursor, list) else cursor[token]
                return expand(cursor)
            return {key: expand(item) for key, item in value.items()}
        if isinstance(value, list):
            return [expand(item) for item in value]
        return value

    assert expand(context["decision_refresh"]["decisions"][0]) == selected
    reference = context["decisions"][0]["supplied_record_reference"]
    reconstructed = expand({"$ref": reference["json_pointer"]})
    for key in reference["removed_fields"]:
        reconstructed.pop(key)
    reconstructed.update(expand(reference["overrides"]))
    assert reconstructed == current
    assert original["charter_version"] == 1 and action["guidance_revision"] == 0


def test_selected_review_scopes_only_unselected_model_narratives():
    from optimization_framework.research.context import _share_decision_context
    selected = {"id": "chosen", "status": "pending", "action_id": "old_action", "rationale": "Selected original rationale."}
    question = {"id": "unselected_question", "status": "pending", "title": "Keep these assumptions?",
        "question": "Exact scientific question", "options": [{"id": "0", "label": "Keep"}],
        "context": "Old model analysis " * 100, "rationale": "Old model rationale " * 100,
        "comment": "Exact user instruction; retain it."}
    executable = {"id": "unselected_action", "status": "pending", "action_id": "other_action", "context": "Another executable action's basis."}
    extension = {"id": "unselected_extension", "status": "pending", "incremental_solver_calls": 12, "context": "The exact extension basis."}
    context = {"decisions": deepcopy([selected, question, executable, extension]),
        "decision_refresh": {"decisions": [{"decision": deepcopy(selected), "action": {"id": "old_action"}}]}}
    _share_decision_context(context)
    projected = context["decisions"][1]
    assert projected["omitted_narrative_fields"] == ["rationale", "context"]
    assert projected["narrative_record_id"] == question["id"]
    for key in ("id", "status", "title", "question", "options", "comment"):
        assert projected[key] == question[key]
    assert "context" not in projected and "rationale" not in projected
    assert context["decision_refresh"]["decisions"][0]["decision"] == selected
    assert context["decisions"][2] == executable
    assert context["decisions"][3] == extension


def test_compacted_refresh_keeps_latest_scientific_request_and_source_authority(prepared, monkeypatch):
    from optimization_framework.research import context as context_module
    workspace, campaign, manager, attempts = prepared
    decision, _ = proposal(workspace, campaign)
    scientific_request = "Choose the top two families and test different wavelengths and angles to assess generalization."
    manager.start(ResearchInput(campaign_id=campaign["id"], message=scientific_request))
    source_run = workspace.store.get(attempts[0], "research_run")
    source_turn = workspace.store.get(source_run["manager_command_id"], "manager_command")
    source_run["status"] = "completed"
    workspace.store.put("research_run", source_run)
    # Automatic reconsiderations and earlier procedural refreshes are not a
    # replacement for the researcher's actual scientific direction.
    workspace.store.put("manager_command", {"id": "later_automatic", "campaign_id": campaign["id"],
        "created_at": "9998", "automatic": True, "status": "completed", "request": {"message": "Automatic housekeeping"}})
    workspace.store.put("manager_command", {"id": "later_refresh", "campaign_id": campaign["id"],
        "created_at": "9999", "automatic": False, "status": "completed", "decision_refresh_id": "earlier_refresh",
        "request": {"message": "Procedural refresh"}})
    monkeypatch.setattr(context_module, "WORKING_LIMIT", 1)
    workspace.commands.execute(command(workspace, campaign, "decision.refresh", refresh_payload(decision), "preserve_science"))
    run = workspace.store.get(attempts[-1], "research_run")
    context = _safe_context(run["context_snapshot"])
    assert context["history"] == []
    pinned = context["decision_refresh"]["continuing_researcher_request"]
    assert pinned["message"] == scientific_request
    assert pinned["manager_command_id"] == source_turn["id"]
    assert pinned["message_id"] == "message_" + source_turn["id"]
    assert pinned["source_charter_version"] == source_run["charter_version"]
    assert pinned["source_guidance_revision"] == source_turn["admitted_guidance_revision"]
    assert pinned["source_guidance_revision"] < run["guidance_revision"]
    assert pinned["source_authority_hash"]
