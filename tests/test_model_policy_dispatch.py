"""Pinned role assignments reach transports and independent implementation jobs."""
from copy import deepcopy
import json

import httpx
import pytest

from optimization_framework.research import codex_provider, engine, providers
from optimization_framework.research.model_policy import ModelPolicy


def policy():
    return ModelPolicy.model_validate({
        "default": {"model": "gpt-6-sol", "reasoning_effort": "xhigh"},
        "roles": {
            "campaign_manager": {"model": "gpt-6-astra", "reasoning_effort": "xhigh"},
            "literature_investigator": {"model": "gpt-6-luna", "reasoning_effort": "xhigh"},
            "methodology_specialist": {"model": "gpt-6-luna", "reasoning_effort": "xhigh"},
        },
    }).model_dump(mode="json")


@pytest.fixture
def codex(monkeypatch):
    for key in list(providers.os.environ):
        if key.startswith(("GRATING_LLM_", "GRATING_CODEX_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("GRATING_LLM_PROVIDER", "codex")
    monkeypatch.setenv("GRATING_LLM_ENABLED", "true")
    monkeypatch.setattr(providers.shutil, "which", lambda name: "/fixture/codex")
    calls = []

    def invoke(system, content, schema, config, on_progress=None):
        calls.append(deepcopy(config))
        return {"text": json.dumps({"analysis": "A scientific rationale requires numerical tests."}),
                "usage": {"input_tokens": 100, "output_tokens": 30}}

    monkeypatch.setattr(codex_provider, "run_codex", invoke)
    monkeypatch.setattr(engine.httpx, "Client", lambda **kwargs: pytest.fail("Unexpected paid API call"))
    return calls


def test_pinned_roles_reach_codex_and_receipts_without_override_bleed(codex):
    chosen = policy()
    events = []
    adapter = engine.LLMAdapter(config={**providers.provider_status(), "model_policy": chosen},
                                budget_usd=0, reservation_callback=events.append)
    # Editing the caller's configuration cannot alter this accepted work.
    chosen["default"]["model"] = "later-model"
    for role in ("literature_investigator", "unplanned_researcher", "research_synthesizer",
                 "implementation_builder", "methodology_specialist"):
        adapter.call(role, {})
    expected = ["gpt-6-luna", "gpt-6-sol", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna"]
    assert [call["model"] for call in codex] == expected
    assert all(call["reasoning_effort"] == "xhigh" for call in codex)
    requests = [event for event in events if event["type"] == "provider_request"]
    assert [event["model"] for event in requests] == expected
    assert all(event["reasoning_effort"] == "xhigh" for event in requests)
    assert adapter.usage["subscription_calls"] == 5 and adapter.usage["api_cost_usd"] == 0


def test_legacy_manager_and_specialists_use_frozen_request_policy(codex):
    context = {"campaign": {"id": "campaign", "charter": {"objective": "Optimize the declared problem"}},
               "tasks": [{"id": "dev", "split": "development"}], "hypotheses": [], "trials": []}
    result = engine.run_research({"mode": "plan", "max_calls": 3, "model_policy": policy(),
                                  "provider_snapshot": providers.provider_status(), "llm_budget_usd": 0}, context)
    assert [call["model"] for call in codex] == ["gpt-6-sol", "gpt-6-sol", "gpt-6-astra"]
    assert [step["model"] for step in result["trace"]] == ["gpt-6-sol", "gpt-6-sol", "gpt-6-astra"]
    assert all(step["reasoning_effort"] == "xhigh" for step in result["trace"])


def test_role_prices_accumulate_and_unknown_price_never_reuses_another_model(monkeypatch):
    chosen = policy()
    chosen["default"].update(input_usd_per_million=2., output_usd_per_million=4.)
    chosen["roles"]["literature_investigator"].update(input_usd_per_million=.1, output_usd_per_million=.5)
    config = {"configured": True, "enabled": True, "provider": "compatible", "model": "unknown-base",
              "transport": "chat_completions", "billing_mode": "api", "pricing_known": False,
              "base_url": "http://localhost:8123/v1", "reasoning_effort": "low",
              "input_usd_per_million": None, "output_usd_per_million": None, "model_policy": chosen}
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"usage": {"prompt_tokens": 100, "completion_tokens": 30},
            "choices": [{"message": {"content": json.dumps({"analysis": "Measured evidence is still needed."})}}]})

    client = httpx.Client
    monkeypatch.setattr(engine.httpx, "Client", lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    monkeypatch.setattr(engine, "_api_key", lambda base_url: "")
    adapter = engine.LLMAdapter(config=config, budget_usd=1)
    adapter.call("problem_analyst", {})
    adapter.call("literature_investigator", {})
    assert adapter.usage["api_cost_usd"] == pytest.approx(.000345)
    assert adapter.usage["cost_usd"] == pytest.approx(.000345)
    with pytest.raises(engine.BudgetUnavailable, match="prices"):
        adapter.call("campaign_manager", {})
    assert [call["model"] for call in calls] == ["gpt-6-sol", "gpt-6-luna"]
    assert adapter.usage["calls"] == 2


def test_implementation_request_preserves_old_identity_and_restricts_model_fields():
    from optimization_framework.implementations.models import JobRequest

    payload = {"workspace_id": "ws", "campaign_id": "campaign", "idempotency_key": "job", "grant_id": "grant",
               "spec": {"name": "Scientific method", "mechanism": "Change one coordinate", "acceptance_criteria": ["Feasible candidates"]}}
    assert "model_policy" not in JobRequest.model_validate(payload).model_dump()
    assert JobRequest.model_validate({**payload, "model_policy": policy()}).model_policy.default.model == "gpt-6-sol"
    with pytest.raises(ValueError, match="Extra inputs"):
        JobRequest.model_validate({**payload, "model_policy": {**policy(), "enabled": True}})


def test_commission_freezes_policy_and_idempotent_replay_keeps_it(tmp_path, monkeypatch):
    from optimization_framework.execution.service import Workspace
    from optimization_framework.contracts.requests import CampaignInput, HypothesisInput, ResearchInput
    from optimization_framework.campaigns.hypotheses import create
    from optimization_framework.campaigns.manager import CampaignManager
    from optimization_framework.implementations.models import CapabilityUnavailable, JobRequest
    from optimization_framework.implementations.service import ImplementationService

    workspace = Workspace(tmp_path / "workspace")
    campaign = workspace.create_campaign(CampaignInput(name="Models", implementation_compute_budget_seconds=300,
        tasks=[{"name": "Grating", "physics": {"n_cells": 8, "fourier_order": 2}}]))
    chosen = policy()
    monkeypatch.setattr(workspace.models, "snapshot", lambda cid: deepcopy(chosen))
    manager = CampaignManager(workspace)
    # Legacy admission persists the selected policy before any model thread.
    from optimization_framework.research.coordinator import ResearchCoordinator
    run = ResearchCoordinator.start(manager, ResearchInput(campaign_id=campaign["id"], message="Inspect the problem"), dispatch=False)
    assert run["request"]["model_policy"] == chosen
    hypothesis = create(workspace, campaign["id"], HypothesisInput(campaign_id=campaign["id"],
        title="Candidate method", algorithm="hillclimb", mechanism="Local changes", rationale="A development proposal"), identity="hypothesis_fixture")
    spec = {"name": "Scientific method", "mechanism": "Change one bit", "acceptance_criteria": ["Feasible candidates"]}
    arguments = {"hypothesis_id": hypothesis["id"], "spec": spec, "compute_seconds": 100, "idempotency_key": "first"}
    first = workspace.implementations.reserve_commission(**arguments)
    frozen = deepcopy(first["request"]["model_policy"])
    chosen["default"]["model"] = "future-sol"
    assert workspace.implementations.reserve_commission(**arguments)["request"]["model_policy"] == frozen
    second = workspace.implementations.reserve_commission(**{**arguments, "idempotency_key": "second"})
    assert second["request"]["model_policy"]["default"]["model"] == "future-sol"
    assert workspace.store.get(run["id"], "research_run")["request"]["model_policy"] == frozen

    captured = []
    def inspect_adapter(**kwargs):
        captured.append(kwargs["config"])
        raise CapabilityUnavailable("Stopped fixture after model configuration delivery")

    library = ImplementationService(tmp_path / "library", adapter_factory=inspect_adapter)
    request = JobRequest.model_validate(first["request"])
    job = library.run_job(library.submit(request)["id"])
    assert job["status"] == "blocked"
    assert captured[0]["model_policy"] == frozen
    assert "deadline_monotonic" in captured[0]
