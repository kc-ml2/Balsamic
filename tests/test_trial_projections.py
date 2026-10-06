"""Bounded read projections preserve geometry access and cost authority."""
import copy
import json
from types import SimpleNamespace

import pytest

from optimization_framework.analysis.tensorboard_view import ScalarExporter
from optimization_framework.api.app import compact_trial
from optimization_framework.execution.resources import ResourceLedger, budget_spent, spent
from optimization_framework.storage.sqlite import Store


def trial(identity, campaign="campaign_one", **updates):
    record = {"id": identity, "campaign_id": campaign, "algorithm": "optimizer", "seed": 17,
              "status": "completed", "wall_seconds": 20, "execution_seconds": 2,
              "progress": {"elapsed_seconds": 7, "best_design": [[1, 0]],
                           "archive": [{"candidate": [0, 1] * 50000}], "best_candidate": [0, 1] * 50000},
              "result": {"elapsed_seconds": 9, "archive": [{"candidate": [0, 1] * 50000}],
                         "best_candidate": [0, 1] * 50000, "best_design": [[1, 0]]}}
    record.update(updates)
    return record


def test_cost_projection_preserves_isolation_upper_bounds_exclusion_and_campaign(tmp_path, monkeypatch):
    store = Store(tmp_path)
    originals = [trial("normal", execution_grant_id="grant_one", execution_seconds_upper_bound=15),
                 trial("isolated", isolation_policy={"backend": "container"}, execution_grant_id="grant_one",
                       execution_seconds_upper_bound=30),
                 trial("empty_policy", isolation_policy={}),
                 trial("queued", status="queued", execution_seconds_upper_bound=25)]
    for record in originals:
        store.put("trial", record)
    store.put("trial", trial("other", campaign="campaign_two"))
    costs = store.list_trial_costs("campaign_one")
    assert [row["id"] for row in costs] == [row["id"] for row in originals]
    for projection, original in zip(costs, originals):
        assert spent(projection) == spent(original)
        assert budget_spent(projection) == budget_spent(original)
        assert "archive" not in projection["progress"]
        assert "best_design" not in projection["result"]
    assert [row["id"] for row in store.list_trial_costs("campaign_one", exclude="isolated")] == ["normal", "empty_policy", "queued"]
    expected = {}
    for original in originals:
        row = expected.setdefault(original.get("execution_grant_id"), {"actual": 0, "committed": 0})
        row["actual"] += spent(original)
        row["committed"] += max(budget_spent(original), original["wall_seconds"]) if original["status"] == "queued" else budget_spent(original)
    original_list = store.list
    def bounded_list(kind, campaign_id=None):
        assert kind != "trial", "Accounting must not decode archived numerical masks"
        return original_list(kind, campaign_id)
    monkeypatch.setattr(store, "list", bounded_list)
    assert ResourceLedger(store).members("campaign_one") == expected
    assert ResourceLedger(store).members("campaign_one", exclude="queued")[None]["actual"] == 9


def test_explicitly_unknown_cost_bound_is_not_silently_replaced_with_zero(tmp_path):
    store = Store(tmp_path)
    original = trial("unknown_bound", execution_seconds_upper_bound=None)
    store.put("trial", original)
    projection = store.list_trial_costs()[0]
    assert projection["execution_seconds_upper_bound"] is None
    with pytest.raises(TypeError):
        budget_spent(original)
    with pytest.raises(TypeError):
        budget_spent(projection)


@pytest.mark.parametrize("progress", [{"elapsed_seconds": 7, "best_design": [[1, 0]]}, {}, None])
def test_compact_projection_matches_state_geometry_and_keeps_evidence_references(tmp_path, progress):
    store = Store(tmp_path)
    original = trial("visible", progress=copy.deepcopy(progress), study_id="study_one", recipe={"id": "rcwa_check"},
                     validation={"status": "passed", "evidence_id": "evidence_one"}, parent_trial_id="parent_one",
                     experiment_spec={"identity": "frozen_spec"}, pid=123)
    store.put("trial", original)
    store.put("trial", trial("other", campaign="campaign_two"))
    projection = store.list_compact_trials("campaign_one")
    assert len(projection) == 1
    assert compact_trial(projection[0]) == compact_trial(original)
    assert store.get("visible", "trial") == original
    assert store.list("trial", "campaign_one") == [original]
    assert projection[0]["validation"]["evidence_id"] == "evidence_one"
    assert projection[0]["experiment_spec"] == {"identity": "frozen_spec"}


def test_masks_are_removed_before_python_decode_and_tensorboard_uses_headers(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.put("trial", trial("large"))
    decoded_lengths = []
    loads = json.loads
    def observed_loads(value, *args, **kwargs):
        decoded_lengths.append(len(value))
        return loads(value, *args, **kwargs)
    monkeypatch.setattr(json, "loads", observed_loads)
    projection = store.list_compact_trials()
    assert projection[0]["progress"]["best_design"] == [[1, 0]]
    assert decoded_lengths and max(decoded_lengths) < 1000
    decoded_lengths.clear()
    assert store.list_trial_headers() == [{"id": "large", "campaign_id": "campaign_one", "algorithm": "optimizer", "seed": 17}]
    assert decoded_lengths == []
    workspace = SimpleNamespace(directory=tmp_path, store=store, job_dir=lambda identity: tmp_path / "trials" / identity)
    def forbidden(*args, **kwargs):
        pytest.fail("TensorBoard must not fetch full trial records")
    monkeypatch.setattr(store, "list", forbidden)
    exporter = ScalarExporter(workspace)
    exporter.sync_once()
    exporter.stop()
