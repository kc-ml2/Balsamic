"""Resource polling distinguishes live processes, reservations and forecasts."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from optimization_framework.execution.observability import ResourceObservability, get_observer, selected_manifest
from optimization_framework.execution.resources import ResourceLedger
from optimization_framework.storage.sqlite import Store


GIB = 1024 ** 3


def make_observer(tmp_path, *, available_gib=16):
    directory = tmp_path / "workspace"
    store = Store(directory)
    store.put("campaign", {"id": "c", "compute_budget_seconds": 10000})
    store.put("campaign", {"id": "other", "compute_budget_seconds": 20000})
    workspace = SimpleNamespace(directory=directory, store=store, max_workers=4)
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "meminfo").write_text(f"MemTotal: {32 * GIB // 1024} kB\nMemAvailable: {available_gib * GIB // 1024} kB\n")
    (proc / "stat").write_text("cpu  100 0 100 800 0 0 0 0\n")
    (proc / "loadavg").write_text("1.00 0.75 0.50 1/12 100\n")
    clock = SimpleNamespace(value=2000., monotonic=100.)
    observer = ResourceObservability(workspace, proc_root=proc, sys_root=tmp_path / "sys", clock=lambda: clock.value,
                                     monotonic=lambda: clock.monotonic, cache_seconds=0)
    return observer, workspace, clock


def write_trial(workspace, identity="t", campaign_id="c", **changes):
    trial = {"id": identity, "campaign_id": campaign_id, "status": "completed", "wall_seconds": 100,
             "execution_seconds": 10, "progress": {}, "result": None, **changes}
    workspace.store.put("trial", trial, "trial.progress")
    return trial


def write_race(workspace, *, status="budget_exhausted", deadline=1900):
    workspace.store.put("task", {"id": "task", "campaign_id": "c", "problem": {"definition_id": "meent_2d_dual_polarization_deflector",
                                                                                      "configuration": {"grid_x": 256, "grid_y": 128}}})
    protocol = {"total_seconds": 1000, "worker_seconds": 4000, "confirmation_seeds": list(range(10)),
                "rungs_seconds": [100, 200, 400], "validation_wall_seconds": 60,
                "preflight_subject_trial_ids": ["t", "u"]}
    workspace.store.put("race_protocol", {"id": "protocol", "campaign_id": "c", "definition": protocol})
    race = {"id": "race", "campaign_id": "c", "task_id": "task", "protocol_id": "protocol", "status": status,
            "stage": "numerical_checks", "started_at": 900, "deadline_at": deadline, "profile": {"threads": 1, "max_workers": 2},
            "cells": [{"id": "cell", "phase": "development", "status": "pending", "trial_id": "t", "rung_seconds": 100, "initial_execution_seconds": 10},
                      {"id": "check", "phase": "preflight", "status": "allocated", "trial_id": "u", "rung_seconds": 60, "initial_execution_seconds": 0}]}
    workspace.store.put("adaptive_race", race)
    return race


def write_manifest(workspace, *, predicted=20 * GIB):
    directory = workspace.directory / "races/race"
    directory.mkdir(parents=True)
    manifest = {"campaign_id": "c", "race_id": "race", "status": "numerically_unresolved", "updated_at": 1000,
                "waiting_reason": "Higher fidelity did not fit the historical memory allowance",
                "fidelities": [{"rcwa_order_x": 18, "rcwa_order_y": 9}],
                "memory_predictions": [{"fidelity": {"rcwa_order_x": 22, "rcwa_order_y": 11},
                                        "predicted_peak_bytes": predicted, "available_memory_bytes": 16 * GIB,
                                        "memory_headroom_bytes": 4 * GIB, "basis": "Historical RSS extrapolation"}],
                "worker_memory_samples": [{"phase": "validation", "completed_evaluations": 4, "peak_rss_bytes": 6 * GIB,
                                           "fidelity": {"rcwa_order_x": 18, "rcwa_order_y": 9}, "sampled_at": 999,
                                           "trial_id": "u", "pid": 100, "process_identity": "500"}],
                "fixtures": [{"candidate": [0] * 1000}], "pause_outcome": {"result": {"geometry": [1] * 1000}}}
    path = directory / "preflight.json"
    path.write_text(json.dumps(manifest))
    return path


def write_process(observer, workspace, pid=999, *, identity="500", ticks=10, state="S", associated=True):
    directory = observer.proc_root / str(pid)
    directory.mkdir(exist_ok=True)
    values = ["0"] * 22
    values[0], values[11], values[12], values[19] = state, str(ticks), "0", identity
    (directory / "stat").write_text(f"{pid} (worker name) {' '.join(values)}")
    (directory / "status").write_text("Name:\tworker\nVmRSS:\t1024 kB\nVmHWM:\t2048 kB\nThreads:\t2\n")
    root = workspace.directory / "trials/t"
    root.mkdir(parents=True, exist_ok=True)
    (directory / "cmdline").write_text("python\0--directory\0" + (str(root) if associated else "/other/job") + "\0")
    return directory


def test_ended_blocked_race_has_no_running_or_queued_workers(tmp_path):
    observer, workspace, _ = make_observer(tmp_path)
    write_trial(workspace)
    write_trial(workspace, "u", execution_seconds=20)
    write_race(workspace)
    write_manifest(workspace)
    with workspace.store.connection() as database:
        before = tuple(database.execute("SELECT (SELECT COUNT(*) FROM records),(SELECT COUNT(*) FROM events)").fetchone())
    snapshot = observer.snapshot("c")
    plan = snapshot["plans"][0]
    assert snapshot["workers"]["running_count"] == snapshot["workers"]["queued_count"] == 0
    assert snapshot["workers"]["jobs"] == []
    assert plan["ended"] and plan["pending_jobs"] == 1
    assert plan["worker_seconds_spent"] == 20  # Old checkpoint cost is excluded.
    assert plan["blocked_reason"] == "Higher fidelity did not fit the historical memory allowance"
    assert plan["phases"][2]["state"] == "ended_before_start"
    with workspace.store.connection() as database:
        after = tuple(database.execute("SELECT (SELECT COUNT(*) FROM records),(SELECT COUNT(*) FROM events)").fetchone())
    assert before == after


def test_historical_forecast_is_not_actual_or_current_memory(tmp_path):
    observer, workspace, _ = make_observer(tmp_path, available_gib=30)
    write_trial(workspace)
    write_trial(workspace, "u", execution_seconds=20)
    write_race(workspace)
    write_manifest(workspace)
    snapshot = observer.snapshot("c")
    forecast = snapshot["plans"][0]["memory_forecast"]
    assert forecast["measured_peak_bytes"] == 6 * GIB
    assert forecast["predicted_bytes"] == 20 * GIB
    assert forecast["historical_available_bytes"] == 16 * GIB
    assert forecast["available_bytes"] == 30 * GIB
    assert forecast["historical_fits"] is False and forecast["fits_now"] is True
    assert snapshot["budget"]["allocated_seconds"] == 30  # Forecast does not reserve compute.
    check = next(row for row in snapshot["plans"][0]["memory_checks"] if row["fidelity"]["rcwa_order_x"] == 22)
    assert check["expanded_grid_shape"] == {"x": 23040, "y": 5888}
    assert check["analytical_lower_bound_bytes"] < check["predicted_bytes"]


def test_campaign_scope_and_shared_grants_match_authoritative_ledger(tmp_path):
    observer, workspace, _ = make_observer(tmp_path)
    write_trial(workspace, execution_grant_id="grant", status="queued", wall_seconds=100, execution_seconds=10)
    write_trial(workspace, "other-trial", campaign_id="other", execution_seconds=500)
    workspace.store.put("execution_grant", {"id": "grant", "campaign_id": "c", "worker_seconds": 1000, "deadline_at": 3000})
    workspace.store.put("diagnostic_grant", {"id": "diagnostic", "campaign_id": "c", "status": "reserved", "parent_trial_id": "t",
                                           "execution_grant_id": "grant", "reserved_seconds": 20})
    workspace.store.put("execution_check_grant", {"id": "execution-check", "campaign_id": "c", "status": "reserved", "trial_id": "t",
                                                "execution_grant_id": "grant", "reserved_seconds": 30})
    workspace.store.put("fixed_mask_job", {"id": "fixed", "campaign_id": "c", "status": "completed", "wall_seconds": 100, "execution_seconds": 7,
                                         "fixtures": [{"candidate": [0] * 2000}]})
    snapshot = observer.snapshot("c")
    ledger = ResourceLedger(workspace.store).assessment("c")
    assert snapshot["budget"]["actual_seconds"] == ledger["actual_seconds"] == 17
    assert snapshot["budget"]["allocated_seconds"] == ledger["allocated_seconds"] == 1007
    assert snapshot["budget"]["grants"] == ledger["grants"]
    assert snapshot["budget"]["limit_seconds"] == 10000
    assert len(snapshot["workers"]["jobs"]) == 1
    assert observer.snapshot()["budget"]["actual_seconds"] == 517
    assert observer.snapshot()["budget"]["limit_seconds"] == 30000
    with pytest.raises(KeyError):
        observer.snapshot("missing")


def test_worker_identity_directory_cpu_and_disappearance(tmp_path):
    observer, workspace, clock = make_observer(tmp_path)
    write_trial(workspace, status="running", pid=999, process_identity="500", attempt=1, attempt_started_at=1995)
    directory = write_process(observer, workspace)
    first = observer.snapshot("c")
    job = first["workers"]["jobs"][0]
    assert first["workers"]["running_count"] == 1
    assert job["rss_bytes"] == 1024 ** 2 and job["peak_rss_bytes"] == 2 * 1024 ** 2
    assert job["cpu_percent"] is None
    assert job["measurement_scope"].startswith("Verified owner process only")
    clock.monotonic += 2
    write_process(observer, workspace, ticks=10 + observer._tick_rate)
    second = observer.snapshot("c")
    assert second["workers"]["jobs"][0]["cpu_percent"] == 50
    (directory / "stat").unlink()
    clock.monotonic += 1
    final = observer.snapshot("c")
    assert final["workers"]["running_count"] == 0
    assert final["workers"]["jobs"][0]["rss_bytes"] is None
    assert final["workers"]["jobs"][0]["process_state"] == "exited"
    assert "worker is exited" in final["warnings"][0]


@pytest.mark.parametrize("identity,associated,state,expected", [("wrong", True, "S", "identity_mismatch"),
                                                                ("500", False, "S", "unverified_directory"),
                                                                ("500", True, "Z", "exited")])
def test_unowned_or_zombie_pid_never_counts_as_running(tmp_path, identity, associated, state, expected):
    observer, workspace, _ = make_observer(tmp_path)
    write_trial(workspace, status="running", pid=999, process_identity="500", attempt=1)
    write_process(observer, workspace, identity=identity, associated=associated, state=state)
    snapshot = observer.snapshot("c")
    assert snapshot["workers"]["running_count"] == 0
    assert snapshot["workers"]["jobs"][0]["process_state"] == expected


def test_cpu_unknown_then_nonblocking_delta_and_cgroup_capacity(tmp_path):
    observer, workspace, clock = make_observer(tmp_path)
    directory = observer.sys_root / "fs/cgroup/team"
    directory.mkdir(parents=True)
    (observer.proc_root / "self").mkdir()
    (observer.proc_root / "self/cgroup").write_text("0::/team\n")
    (directory / "memory.max").write_text(str(10 * GIB))
    (directory / "memory.current").write_text(str(4 * GIB))
    (directory / "cpu.max").write_text("150000 100000")
    first = observer.snapshot("c")
    assert first["host"]["cpu"]["utilization_percent"] is None
    assert first["host"]["cpu"]["capacity_cores"] == 1.5
    assert first["host"]["memory"]["available_bytes"] == 16 * GIB
    assert first["host"]["memory"]["effective_available_bytes"] == 6 * GIB
    clock.monotonic += 1
    (observer.proc_root / "stat").write_text("cpu  150 0 150 900 0 0 0 0\n")
    assert observer.snapshot("c")["host"]["cpu"]["utilization_percent"] == 50


def test_manifest_reads_selected_fields_without_decoding_geometry(tmp_path):
    observer, workspace, _ = make_observer(tmp_path)
    path = write_manifest(workspace)
    selected = selected_manifest(path)
    assert "fixtures" not in selected and "pause_outcome" not in selected
    assert selected["memory_predictions"][0]["predicted_peak_bytes"] == 20 * GIB
    path.write_text("{\"campaign_id\":\"c\",\"fixtures\":" + " " * (512 * 1024) + "[]}")
    assert selected_manifest(path) is None


def test_cost_cache_invalidates_on_progress_and_expires_unjournaled_writes(tmp_path):
    observer, workspace, clock = make_observer(tmp_path)
    write_trial(workspace)
    assert observer.snapshot("c")["budget"]["actual_seconds"] == 10
    write_trial(workspace, execution_seconds=20)
    assert observer.snapshot("c")["budget"]["actual_seconds"] == 20
    trial = workspace.store.get("t", "trial")
    trial["execution_seconds"] = 30
    workspace.store.put("trial", trial)
    assert observer.snapshot("c")["budget"]["actual_seconds"] == 20
    clock.monotonic += 31
    assert observer.snapshot("c")["budget"]["actual_seconds"] == 30


def test_shared_observer_and_actual_task_grid(tmp_path):
    observer, workspace, _ = make_observer(tmp_path)
    assert get_observer(workspace) is get_observer(workspace)
    write_trial(workspace)
    write_trial(workspace, "u")
    write_race(workspace)
    task = workspace.store.get("task", "task")
    task["problem"]["configuration"] = {"grid_x": 16, "grid_y": 8}
    workspace.store.put("task", task)
    snapshot = observer.snapshot("c")
    check = snapshot["plans"][0]["memory_checks"][0]
    assert check["configuration"]["grid_x"] == 16
    assert check["configuration"]["grid_y"] == 8


def test_resource_api_is_read_only_and_missing_campaign_is_404(tmp_path):
    from fastapi.testclient import TestClient
    from optimization_framework.api.app import create_app
    app = create_app(tmp_path / "api", start_workers=False)
    app.state.workspace.store.put("campaign", {"id": "c", "compute_budget_seconds": 100})
    with app.state.workspace.store.connection() as database:
        before = database.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    with TestClient(app) as client:
        response = client.get("/api/v1/resources?campaign_id=c")
        assert response.status_code == 200
        assert response.json()["workers"]["running_count"] == 0
        assert client.get("/api/v1/resources?campaign_id=absent").status_code == 404
    with app.state.workspace.store.connection() as database:
        after = database.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert before == after
