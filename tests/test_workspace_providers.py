"""Provider selection and billing boundaries without network or real Codex calls."""
import copy
import json

from fastapi.testclient import TestClient
import pytest

from dqn_meent.workspace import codex_provider, providers, research
from dqn_meent.workspace.api import create_app
from dqn_meent.workspace.coordinator import ResearchCancelled
from dqn_meent.workspace.models import ResearchInput


@pytest.fixture(autouse=True)
def clean_configuration(monkeypatch, tmp_path):
    for key in list(providers.os.environ):
        if key.startswith(("GRATING_LLM_", "GRATING_CODEX_")):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(providers.shutil, "which", lambda name: "/test/bin/codex")
    monkeypatch.setattr(codex_provider, "run_codex", lambda *args, **kwargs: pytest.fail("Unexpected Codex execution"))


def context():
    return {"campaign": {"id": "campaign", "charter": {"objective": "binary grating efficiency"}},
            "tasks": [{"id": "dev", "split": "development"},
                      {"id": "SECRET_TEST", "split": "test"}],
            "hypotheses": research.seed_hypotheses(),
            "trials": [{"id": "SECRET_TRIAL", "task_id": "SECRET_TEST", "best_efficiency": .999}]}


def enable_codex(monkeypatch, responder=None):
    monkeypatch.setenv("GRATING_LLM_PROVIDER", "codex")
    monkeypatch.setenv("GRATING_LLM_ENABLED", "true")
    calls = []

    def call(system, content, schema, config, on_progress=None):
        calls.append({"system": system, "content": json.loads(content), "schema": schema,
                      "config": copy.deepcopy(config)})
        if responder:
            return responder(calls[-1])
        return {"text": json.dumps({"analysis": "This mechanism requires a discriminating numerical probe."}),
                "usage": {"input_tokens": 110, "cached_input_tokens": 20, "output_tokens": 30},
                "elapsed_seconds": .25}

    monkeypatch.setattr(codex_provider, "run_codex", call)
    monkeypatch.setattr(research.httpx, "Client", lambda **kwargs: pytest.fail("No paid HTTP fallback is permitted"))
    return calls


def test_default_selects_no_provider_even_when_api_key_exists(tmp_path, monkeypatch):
    (tmp_path / ".key").write_text("test-secret-never-use")
    monkeypatch.setenv("GRATING_LLM_API_KEY", "another-test-secret")
    status = providers.provider_status()
    assert status["provider"] == "none" and status["model"] is None
    assert status["billing_mode"] == "none"
    assert status["enabled"] is False and status["configured"] is False
    monkeypatch.setenv("GRATING_LLM_ENABLED", "true")
    assert providers.provider_status()["configured"] is False
    monkeypatch.delenv("GRATING_LLM_ENABLED")
    monkeypatch.setenv("GRATING_LLM_PROVIDER", "codex")
    assert providers.provider_status()["model"] == "gpt-6-sol"
    assert status["input_usd_per_million"] is None
    assert "test-secret" not in json.dumps(status)
    output = research.run_research({"mode": "discuss"}, context())
    assert output["mode"] == "curated" and output["usage"]["calls"] == 0


@pytest.mark.parametrize("selected", ["openai_api", "compatible"])
def test_api_requires_explicit_provider_and_activation(selected, tmp_path, monkeypatch):
    (tmp_path / ".key").write_text("fake-openai-key")
    monkeypatch.setenv("GRATING_LLM_PROVIDER", selected)
    if selected == "compatible":
        monkeypatch.setenv("GRATING_LLM_BASE_URL", "http://localhost:8123/v1")
    assert providers.provider_status()["configured"] is False
    monkeypatch.setenv("GRATING_LLM_ENABLED", "true")
    status = providers.provider_status()
    assert status["configured"] is True
    assert status["billing_mode"] == "api"
    assert status["transport"] == ("responses" if selected == "openai_api" else "chat_completions")
    monkeypatch.setenv("GRATING_LLM_DISABLED", "true")
    assert providers.provider_status()["configured"] is False


def test_remote_compatible_endpoint_never_receives_default_key(tmp_path, monkeypatch):
    (tmp_path / ".key").write_text("private-openai-key")
    monkeypatch.setenv("GRATING_LLM_PROVIDER", "compatible")
    monkeypatch.setenv("GRATING_LLM_ENABLED", "true")
    monkeypatch.setenv("GRATING_LLM_BASE_URL", "https://compatible.example/v1")
    assert providers.provider_status()["configured"] is False
    assert providers.api_key("https://compatible.example/v1") == ""
    monkeypatch.setenv("GRATING_LLM_API_KEY", "explicit-compatible-key")
    assert providers.provider_status()["configured"] is True


def test_invalid_provider_does_not_fallback_and_missing_codex_stays_unconfigured(monkeypatch):
    monkeypatch.setenv("GRATING_LLM_ENABLED", "true")
    monkeypatch.setenv("GRATING_LLM_PROVIDER", "unknown")
    assert providers.provider_status()["configured"] is False
    monkeypatch.setenv("GRATING_LLM_PROVIDER", "codex")
    monkeypatch.setattr(providers.shutil, "which", lambda name: None)
    assert providers.provider_status()["configured"] is False


def test_subscription_roles_use_sol_with_zero_api_budget_and_filter_heldout(monkeypatch):
    calls = enable_codex(monkeypatch)
    events = []
    result = research.run_research({"mode": "generate", "max_calls": 3, "llm_budget_usd": 0}, context(), events.append)
    assert len(calls) == 3
    assert all(call["config"]["model"] == "gpt-6-sol" for call in calls)
    assert all("SECRET_TEST" not in json.dumps(call) and "SECRET_TRIAL" not in json.dumps(call) for call in calls)
    usage = result["usage"]
    assert usage["provider"] == "codex" and usage["billing_mode"] == "subscription"
    assert usage["calls"] == usage["subscription_calls"] == 3
    assert usage["cost_usd"] is None and usage["api_cost_usd"] == 0
    assert usage["input_tokens"] == 330 and usage["output_tokens"] == 90
    assert usage["cached_input_tokens"] == 60
    reservations = [event for event in events if event["type"] == "provider_call_reserved"]
    assert len(reservations) == 3
    assert all(event["usage"]["pending_reservation"]["billing_mode"] == "subscription" for event in reservations)
    assert "pending_reservation" not in usage


@pytest.mark.parametrize("code,unknown", [("subscription_required", False), ("quota_exhausted", True)])
def test_codex_auth_or_quota_failure_retains_correct_usage_without_api_fallback(monkeypatch, code, unknown):
    def fail(call):
        raise codex_provider.CodexProviderError("Subscription requires researcher attention.", code=code, usage_unknown=unknown)

    calls = enable_codex(monkeypatch, fail)
    events = []
    result = research.run_research({"mode": "discuss", "llm_budget_usd": 0}, context(), events.append)
    assert len(calls) == 1 and result["status"] in {"partial", "awaiting_researcher"}
    assert result["usage"]["subscription_calls"] == int(unknown)
    assert result["usage"]["api_cost_usd"] == 0 and result["usage"]["cost_usd"] is None
    assert "pending_reservation" not in result["usage"]
    assert any(event["type"] == "provider_call_cancelled_before_send" for event in events) is not unknown
    assert result["trace"][0]["status"] == "failed"


def test_subscription_cancellation_before_send_releases_durable_call_reservation(monkeypatch):
    calls = enable_codex(monkeypatch)
    events = []

    def cancel(event):
        events.append(event)
        if event["type"] == "provider_call_reserved":
            raise ResearchCancelled()

    adapter = research.LLMAdapter(budget_usd=0, reservation_callback=cancel)
    with pytest.raises(ResearchCancelled):
        adapter.call("research_synthesizer", {})
    assert calls == []
    assert adapter.usage["calls"] == adapter.usage["subscription_calls"] == 0
    assert adapter.usage["api_cost_usd"] == 0
    assert "pending_reservation" not in adapter.usage
    assert events[-1]["type"] == "provider_call_cancelled_before_send"


@pytest.mark.parametrize("configured,deadline,expected", [
    (180, None, 180),
    (600, None, 600),
    (600, 1045, 45),
    (180, 1600, 180),
])
def test_codex_honors_configured_timeout_and_actual_task_deadline(monkeypatch, configured, deadline, expected):
    calls = enable_codex(monkeypatch)
    monkeypatch.setenv("GRATING_CODEX_TIMEOUT_SECONDS", str(configured))
    monkeypatch.setattr(research.time, "monotonic", lambda: 1000)
    config = providers.provider_status()
    if deadline is not None:
        config["deadline_monotonic"] = deadline
    adapter = research.LLMAdapter(config=config)
    adapter.call("research_synthesizer", {})
    assert len(calls) == 1
    assert calls[0]["config"]["timeout_seconds"] == expected


def test_expired_task_deadline_prevents_model_call_and_releases_reservation(monkeypatch):
    calls = enable_codex(monkeypatch)
    monkeypatch.setattr(research.time, "monotonic", lambda: 1000)
    events = []
    adapter = research.LLMAdapter(config={**providers.provider_status(), "deadline_monotonic": 999},
                                  reservation_callback=events.append)
    with pytest.raises(codex_provider.CodexProviderError, match="deadline expired") as raised:
        adapter.call("research_synthesizer", {})
    assert raised.value.code == "timeout"
    assert calls == []
    assert adapter.usage["calls"] == adapter.usage["subscription_calls"] == 0
    assert "pending_reservation" not in adapter.usage
    assert events[-1]["type"] == "provider_error"
    assert "no model call was sent" in events[-1]["error_message"]


def test_codex_failure_receipt_preserves_safe_message_and_effective_time_limit(monkeypatch):
    def fail(call):
        raise codex_provider.CodexProviderError(
            "Codex inference exceeded its 600.0-second time limit; no fallback was used.",
            code="timeout", usage_unknown=True)

    calls = enable_codex(monkeypatch, fail)
    monkeypatch.setenv("GRATING_CODEX_TIMEOUT_SECONDS", "600")
    events = []
    adapter = research.LLMAdapter(reservation_callback=events.append)
    with pytest.raises(codex_provider.CodexProviderError):
        adapter.call("campaign_manager", {})
    assert len(calls) == 1, "Timeouts must not silently retry or change providers."
    error = events[-1]
    assert error["type"] == "provider_error"
    assert error["error"] == "timeout"
    assert error["error_message"] == "Codex inference exceeded its 600.0-second time limit; no fallback was used."
    assert error["timeout_seconds"] == 600
    assert error["elapsed_seconds"] >= 0
    assert adapter.usage["subscription_calls"] == 1
    assert "pending_reservation" not in adapter.usage


def test_interrupted_subscription_request_cannot_be_automatically_replayed(monkeypatch):
    def interrupt(call):
        raise ResearchCancelled()

    calls = enable_codex(monkeypatch, interrupt)
    adapter = research.LLMAdapter(budget_usd=0)
    with pytest.raises(ResearchCancelled):
        adapter.call("research_synthesizer", {})
    assert adapter.usage["subscription_calls"] == 1
    assert adapter.usage["pending_reservation"]["billing_mode"] == "subscription"
    with pytest.raises(ValueError, match="reconciliation"):
        research.run_research({"mode": "discuss", "resume_state": {"usage": adapter.usage}}, context())
    assert len(calls) == 1


@pytest.mark.parametrize("setting,value", [("GRATING_LLM_MODEL", "another-model"),
                                           ("GRATING_LLM_PROVIDER", "openai_api"),
                                           ("GRATING_LLM_ENABLED", "false")])
def test_resume_rejects_changed_provider_model_or_activation(monkeypatch, setting, value):
    calls = enable_codex(monkeypatch)
    request = {"mode": "discuss", "max_calls": 2, "llm_budget_usd": 0}
    events = []
    research.run_research(request, context(), events.append)
    checkpoint = next(event for event in events if event["type"] == "research_checkpoint")
    monkeypatch.setenv(setting, value)
    with pytest.raises(ValueError, match="checkpoint|provider|configuration"):
        research.run_research({**request, "resume_state": checkpoint["state"],
                              "resume_fingerprint": checkpoint["fingerprint"]}, context())
    assert len(calls) == 1


def test_api_accounting_excludes_subscription_and_preserves_historical_costs():
    assert providers.api_spend({"billing_mode": "subscription", "cost_usd": 200, "api_cost_usd": 200}) == 0
    assert providers.api_spend({"billing_mode": "api", "api_cost_usd": .2, "cost_usd": 3}) == .2
    assert providers.api_spend({"cost_usd": .4}) == .4
    assert providers.api_spend(None) == 0


def test_dashboard_reports_configure_later_and_coordinator_budgets_api_only(tmp_path, monkeypatch):
    (tmp_path / ".key").write_text("fake-key-not-to-be-used")
    app = create_app(tmp_path / "workspace", start_workers=False)
    with TestClient(app) as client:
        settings = client.get("/api/state").json()["settings"]
        assert settings["llm_configured"] is False
        assert settings["model"] is None and settings["provider"]["provider"] == "none"
        assert settings["provider"]["billing_mode"] == "none"
        response = client.post("/api/campaigns", json={"name": "Provider ledger", "llm_budget_usd": 1,
            "tasks": [{"name": "Development", "physics": {"n_cells": 6, "fourier_order": 1}}]})
        assert response.status_code == 201, response.text
        campaign = response.json()
        store = app.state.workspace.store
        for index, usage in enumerate(({"billing_mode": "subscription", "cost_usd": 200, "subscription_calls": 3},
                                       {"billing_mode": "api", "api_cost_usd": .2, "cost_usd": .9},
                                       {"cost_usd": .1})):
            store.put("research_run", {"id": f"historical-{index}", "campaign_id": campaign["id"],
                                      "status": "completed", "usage": usage})
        coordinator = app.state.coordinator
        monkeypatch.setattr(coordinator, "_thread", lambda record: None)
        monkeypatch.setattr("optimization_framework.research.coordinator.provider_status", lambda: {"configured": True})
        started = coordinator.start(ResearchInput(campaign_id=campaign["id"], mode="discuss", message="Compare mechanisms."))
        assert started["request"]["llm_budget_usd"] == pytest.approx(.7)
        reservation_usage = {"billing_mode": "subscription", "cost_usd": None,
                             "api_cost_usd": 0, "calls": 1, "subscription_calls": 1}
        coordinator._emit(started["id"], {"type": "provider_call_reserved", "role": "research_synthesizer", "usage": reservation_usage})
        assert store.get(started["id"], "research_run")["usage"] == reservation_usage
        budget = client.get("/api/state", params={"campaign_id": campaign["id"]}).json()["budget"]
        assert budget["llm_spent_usd"] == pytest.approx(.3)
