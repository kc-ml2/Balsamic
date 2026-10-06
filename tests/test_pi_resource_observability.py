"""The lead agent sees the same scoped resource telemetry without allocating work."""
from copy import deepcopy
from types import SimpleNamespace

from optimization_framework.agents.tools import PiTools


def tools_with_snapshot(snapshot):
    seen = []

    def sample(campaign_id):
        seen.append(campaign_id)
        return snapshot

    workspace = SimpleNamespace(resource_observer=SimpleNamespace(snapshot=sample))
    controller = SimpleNamespace(workspace=workspace, store=None)
    workspace.store = None
    return PiTools(controller), seen


def test_resource_read_is_scoped_and_does_not_modify_shared_snapshot():
    snapshot = {
        "sampled_at": "2026-10-02T03:00:00+00:00",
        "host": {"memory": {"available_bytes": 17 * 1024**3}},
        "workers": {"running_count": 0, "queued_count": 0,
                    "jobs": [{"id": f"trial_{i}"} for i in range(30)]},
        "plans": [{"race_id": f"race_{i}", "status": "budget_exhausted",
                   "blocked_reason": "Memory forecast exceeded capacity",
                   "memory_forecast": {"predicted_bytes": 27 * 1024**3,
                                       "measured_peak_bytes": 6 * 1024**3},
                   "decisions": [{"id": str(j)} for j in range(20)],
                   "memory_checks": [{"fidelity": j} for j in range(20)]}
                  for i in range(10)],
    }
    original = deepcopy(snapshot)
    tools, seen = tools_with_snapshot(snapshot)
    result = tools._execute({"campaign_id": "my_campaign"}, {}, "resource_inspect", {}, "read")
    assert seen == ["my_campaign"]
    assert result["plan_count"] == 10
    assert len(result["plans"]) == 4
    assert len(result["workers"]["jobs"]) == 16
    assert len(result["plans"][-1]["decisions"]) == 5
    assert len(result["plans"][-1]["memory_checks"]) == 8
    assert result["plans"][-1]["memory_forecast"]["predicted_bytes"] > result["plans"][-1]["memory_forecast"]["measured_peak_bytes"]
    assert snapshot == original


def test_resource_telemetry_available_to_pi_and_specialists_but_not_protected_builder():
    tools, _ = tools_with_snapshot({})
    assert "resource_inspect" in tools.names({"role": "lead"})
    assert "resource_inspect" in tools.names({"role": "methodology_specialist"})
    assert "resource_inspect" not in tools.names({"role": "implementation_builder", "grant_id": "protected"})


def test_records_from_before_the_lead_rename_are_migrated(tmp_path):
    from optimization_framework.execution.service import Workspace
    workspace = Workspace(tmp_path)
    workspace.store.put("agent_session", {"id": "pi_campaign_x", "campaign_id": "campaign_x", "role": "pi"})
    workspace.store.put("agent_campaign", {"id": "pi_campaign_campaign_x", "campaign_id": "campaign_x", "pi_id": "pi_campaign_x"})
    reopened = Workspace(tmp_path)
    assert reopened.store.get("pi_campaign_x", "agent_session")["role"] == "lead"
    config = reopened.store.get("pi_campaign_campaign_x", "agent_campaign")
    assert config["lead_id"] == "pi_campaign_x" and "pi_id" not in config
