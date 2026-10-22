import json

from fastapi.testclient import TestClient
from tensorboard.backend.event_processing.event_accumulator import EventAccumulator

from optimization_framework.analysis.tensorboard_view import ScalarExporter
from balsamic.app import create_app
from optimization_framework.contracts.requests import CampaignInput, TaskInput


class _Store:
    def list(self, kind):
        return [{"id": "trial_one", "campaign_id": "campaign_one", "algorithm": "optimizer", "seed": 7}] if kind == "trial" else []


class _Workspace:
    def __init__(self, directory):
        self.directory = directory
        self.store = _Store()

    def job_dir(self, trial_id):
        return self.directory / "trials" / trial_id


def test_scalar_exporter_tracks_new_observations_without_repeating_old_ones(tmp_path):
    workspace = _Workspace(tmp_path)
    journal = workspace.job_dir("trial_one") / "metrics.jsonl"
    journal.parent.mkdir(parents=True)
    first = {"updated_at": "2026-10-01T00:00:00+00:00", "evaluations": 1, "objective": 0.2,
             "best_objective": 0.2, "elapsed_seconds": 2.4, "solver_calls": 2, "cache_hits": 0}
    second = {**first, "evaluations": 2, "objective": 0.1, "best_objective": 0.2,
              "elapsed_seconds": 4.1, "solver_calls": 4}
    journal.write_text(json.dumps(first) + "\n")
    exporter = ScalarExporter(workspace)
    exporter.sync_once()
    with journal.open("a") as stream:
        stream.write(json.dumps(second) + "\n")
    exporter.sync_once()
    exporter.sync_once()
    exporter.stop()
    run = exporter.directory / "campaign_one" / "optimizer" / "seed-7-trial_one"
    accumulator = EventAccumulator(str(run))
    accumulator.Reload()
    assert [(point.step, round(point.value, 3)) for point in accumulator.Scalars("efficiency/best_by_evaluation")] == [(1, 0.2), (2, 0.2)]
    assert [(point.step, round(point.value, 3)) for point in accumulator.Scalars("efficiency/best_by_worker_second")] == [(2, 0.2), (4, 0.2)]


def test_tensorboard_is_mounted_and_experiment_reads_keep_design_access(tmp_path):
    app = create_app(tmp_path / "workspace", start_workers=False)
    client = TestClient(app)
    assert client.get("/tensorboard/").status_code == 200
    assert client.get("/tensorboard/data/plugins_listing").status_code == 200
    workspace = app.state.workspace
    campaign = workspace.create_campaign(CampaignInput(name="Design access", compute_budget_seconds=100,
        validation_reserve_seconds=10, tasks=[TaskInput(name="Quadratic", problem_id="bounded_continuous", configuration={})]))
    trial = {"id": "trial_one", "campaign_id": campaign["id"], "status": "completed", "pid": 123, "wall_seconds": 5,
             "progress": {"best_candidate": [[1]], "best_design": [[1]], "archive": [{"candidate": [[1]]}], "best_objective": 0.2},
             "result": {"best_design": [[1]], "best_objective": 0.2}}
    workspace.store.put("trial", trial)
    shown = client.get(f"/api/v1/experiments/{trial['id']}").json()["trial"]
    assert shown["progress"]["best_design"] == [[1]] and shown["result"]["best_objective"] == 0.2
    assert "pid" not in shown
    summary = client.get(f"/api/v1/campaigns/{campaign['id']}/summary").json()
    listed = summary["trials"]
    assert [row["id"] for row in listed] == [trial["id"]] and "pid" not in listed[0] and summary["counts"]["trial"] == 1
    # Pages poll the summary and the experiment list on every event: their runs leave the designs to the run's own record.
    assert listed[0]["progress"] == {"best_objective": 0.2} and listed[0]["result"] == {"best_objective": 0.2}
    rows = client.get(f"/api/v1/campaigns/{campaign['id']}/experiments").json()
    assert all("best_design" not in (row.get("progress") or {}) for group in ("short", "long", "checks") for row in rows[group])


def test_scoped_reads_change_their_cursor_on_a_committed_event(tmp_path):
    app = create_app(tmp_path / "workspace", start_workers=False)
    workspace = app.state.workspace
    campaign = workspace.create_campaign(CampaignInput(name="Cache check", compute_budget_seconds=100,
        validation_reserve_seconds=10, tasks=[TaskInput(name="Quadratic", problem_id="bounded_continuous", configuration={})]))
    client = TestClient(app)
    path = f"/api/v1/campaigns/{campaign['id']}/experiments"
    before = client.get(path).json()
    assert not before["unchanged"]
    assert client.get(path, params={"after": before["event_cursor"]}).json()["unchanged"]
    workspace.store.put("manager_note", {"id": "note_one", "campaign_id": campaign["id"],
        "kind": "finding", "content": "New evidence"}, "manager.note")
    after = client.get(path, params={"after": before["event_cursor"]}).json()
    assert after["event_cursor"] > before["event_cursor"] and not after["unchanged"]
