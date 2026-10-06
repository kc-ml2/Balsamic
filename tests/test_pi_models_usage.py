"""Model choice in dev mode, the model-family lock, and per-call LLM usage accounting."""
import json

import pytest

from optimization_framework.agents import usage
from optimization_framework.agents.families import family_of
from optimization_framework.contracts.commands import Command
from optimization_framework.contracts.requests import CampaignInput, TaskInput
from optimization_framework.execution.service import Workspace
from optimization_framework.storage.sqlite import now


def test_families_follow_vendors_through_routers():
    assert family_of("deepseek", "deepseek-v4-pro") == family_of("openrouter", "deepseek/deepseek-v4-pro") == "deepseek"
    assert family_of("openai-codex", "gpt-6-sol") == family_of("openrouter", "openai/gpt-5.5") == "openai"
    assert family_of("anthropic", "claude-opus") == family_of("openrouter", "anthropic/claude-opus") == "anthropic"
    assert family_of("strixhalo-llama", "qwen3.6-35b") == "qwen"
    assert family_of("local", "mystery") == "local:mystery"


def campaign(tmp_path, monkeypatch, *, profile=True, budget=5):
    monkeypatch.setenv("GRATING_LLM_ENABLED", "true")
    monkeypatch.setenv("GRATING_LLM_DISABLED", "false")
    if profile:
        directory = tmp_path / "profile"
        directory.mkdir()
        (directory / "settings.json").write_text(json.dumps({"defaultProvider": "deepseek", "defaultModel": "deepseek-v4-pro"}))
        monkeypatch.setenv("GRATING_PI_PROFILE", str(directory))
    else:
        monkeypatch.delenv("GRATING_PI_PROFILE", raising=False)
    workspace = Workspace(tmp_path / "workspace")
    record = workspace.create_campaign(CampaignInput(name="Models", compute_budget_seconds=100, validation_reserve_seconds=10, llm_budget_usd=budget,
        tasks=[TaskInput(name="Quadratic", problem_id="bounded_continuous", configuration={})]))
    workspace.commands.execute(Command(id="activate", campaign_id=record["id"], expected_revision=1,
        operation="agent.activate", payload={"objective": "Work within the allocation"}))
    lead = workspace.store.get(workspace.pi.configuration(record["id"])["lead_id"], "agent_session")
    return workspace, record, lead


def configure(workspace, record, identity, agent_id, provider, model, effort=None):
    return workspace.commands.execute(Command(id=identity, campaign_id=record["id"],
        expected_revision=workspace.store.get(record["id"], "campaign")["version"], operation="agent.configure",
        payload={"agent_id": agent_id, "model": {"provider": provider, "model": model, "effort": effort}}))


def test_dev_mode_activation_uses_the_profile_default_and_locks_its_family(tmp_path, monkeypatch):
    workspace, record, lead = campaign(tmp_path, monkeypatch)
    config = workspace.pi.configuration(record["id"])
    assert config["llm_family"] == "deepseek"
    assert (lead["provider"], lead["model"], lead["reasoning_effort"]) == ("deepseek", "deepseek-v4-pro", None)
    configure(workspace, record, "switch", lead["id"], "deepseek", "deepseek-flash", "max")
    lead = workspace.store.get(lead["id"], "agent_session")
    assert (lead["model"], lead["reasoning_effort"]) == ("deepseek-flash", "max")
    assert lead["model_history"][-1]["model"] == "deepseek-v4-pro"
    with pytest.raises(ValueError, match="model family"):
        configure(workspace, record, "cross", lead["id"], "anthropic", "claude-opus")
    with pytest.raises(ValueError, match="model family"):
        configure(workspace, record, "cross_default", None, "openai", "gpt-5.5")
    configure(workspace, record, "default", None, "deepseek", "deepseek-flash", "high")
    child = workspace.pi.create_agent(record["id"], "methodology_specialist", "Review", "child", parent_id=lead["id"])
    assert (child["model"], child["reasoning_effort"]) == ("deepseek-flash", "high")


def test_known_models_and_thinking_levels_are_validated(tmp_path, monkeypatch):
    workspace, record, lead = campaign(tmp_path, monkeypatch)
    workspace.pi.harness_status = {"models": [{"provider": "deepseek", "id": "deepseek-v4-pro", "thinking_levels": ["off", "high", "max"]}]}
    with pytest.raises(ValueError, match="thinking levels"):
        configure(workspace, record, "level", lead["id"], "deepseek", "deepseek-v4-pro", "xhigh")
    with pytest.raises(ValueError, match="not available"):
        configure(workspace, record, "missing", lead["id"], "deepseek", "deepseek-v9")


def test_locked_mode_keeps_legacy_models_and_refuses_changes(tmp_path, monkeypatch):
    workspace, record, lead = campaign(tmp_path, monkeypatch, profile=False)
    assert (lead["provider"], lead["model"]) == ("openai-codex", "gpt-6-astra")
    assert workspace.pi.configuration(record["id"])["llm_family"] == "openai"
    with pytest.raises(ValueError, match="dev profile"):
        configure(workspace, record, "switch", lead["id"], "openai-codex", "gpt-6-sol")


def message(seq, cost, billing="api", model="deepseek-v4-pro"):
    return {"seq": seq, "occurred_at": now(), "type": "assistant.message", "text": "done", "provider": "deepseek",
            "model": model, "thinking_level": "high", "billing": billing,
            "usage": {"input": 100, "output": 20, "cacheRead": 50, "cacheWrite": 0, "reasoning": 5, "totalTokens": 170, "cost_usd": cost}}


def test_calls_are_recorded_once_and_only_api_billing_is_charged(tmp_path, monkeypatch):
    workspace, record, lead = campaign(tmp_path, monkeypatch)
    remote = {"runs": {}, "cursor": 2, "events": [message(1, 0.25), message(2, 0.5, billing="subscription")]}
    workspace.pi._receive(lead["id"], remote)
    workspace.pi._receive(lead["id"], remote)
    summary = usage.summary(workspace.store, record["id"])
    assert summary["totals"]["calls"] == 2 and summary["totals"]["total_tokens"] == 340
    assert summary["totals"]["cost_usd"] == pytest.approx(0.75) and summary["totals"]["charged_usd"] == pytest.approx(0.25)
    assert {row["billing"] for row in summary["by_model"]} == {"api", "subscription"}
    assert summary["by_agent"][0]["agent_id"] == lead["id"] and len(summary["series"]) == 1
    saved = workspace.store.get(lead["id"], "agent_session")["usage"]
    assert saved["calls"] == 2 and saved["charged_usd"] == pytest.approx(0.25)


def test_api_spending_at_the_cap_holds_new_turns(tmp_path, monkeypatch):
    workspace, record, lead = campaign(tmp_path, monkeypatch, budget=1)
    assert workspace.pi._within_llm_budget(record["id"])
    workspace.pi._receive(lead["id"], {"runs": {}, "cursor": 1, "events": [message(1, 1.5)]})
    assert not workspace.pi._within_llm_budget(record["id"])
    issues = workspace.store.list("manager_issue", record["id"])
    assert any(issue["code"] == "llm_budget" for issue in issues)


def test_calls_recorded_before_accounting_are_backfilled_from_the_agent_log(tmp_path, monkeypatch):
    workspace, record, lead = campaign(tmp_path, monkeypatch)
    event = {"seq": 7, "occurred_at": now(), "type": "assistant.message", "text": "old",
             "usage": {"input": 10, "output": 2, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 12}}
    workspace.agent_log.record(record["id"], "pi.assistant.message", agent_id=lead["id"], role=lead["role"],
                               event_key=f"pi:{lead['id']}:7", payload=event)
    with workspace.store.connection() as db:
        db.execute("DELETE FROM llm_usage")
    reopened = Workspace(tmp_path / "workspace")
    rows = usage.summary(reopened.store, record["id"])["by_model"]
    assert rows[0]["total_tokens"] == 12 and rows[0]["cost_usd"] is None and rows[0]["charged_usd"] == 0
