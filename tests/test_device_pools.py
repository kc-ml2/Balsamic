"""Runs are placed on GPU slots or on the CPU slots beside them, and runs compared on time never mix the two."""
from types import SimpleNamespace

import pytest

from optimization_framework.execution.devices import Pools, environment, physical_cores
from optimization_framework.execution.runtime_profile import combine, derive

TWO_D = {"definition_id": "meent_2d_dual_polarization_deflector"}
ONE_D = {"definition_id": "meent_grating"}


class Store:
    def __init__(self, trials=()):
        self.trials = list(trials)

    def list(self, kind, campaign_id=None):
        return [trial for trial in self.trials if campaign_id in (None, trial["campaign_id"])]


def pools(workers=15, gpu=1, trials=()):
    return Pools(SimpleNamespace(max_workers=workers, gpu_workers=gpu, store=Store(trials)))


def run(problem=TWO_D, **extra):
    return {"id": extra.pop("id", "trial_x"), "campaign_id": "c", "problem": problem, **extra}


def test_the_slots_split_into_gpu_and_cpu_and_cpu_always_keeps_one():
    assert pools(15, 1).sizes() == {"cuda": 1, "cpu": 14}
    assert pools(2, 0).sizes() == {"cuda": 0, "cpu": 2}
    assert pools(1, 1).sizes() == {"cuda": 0, "cpu": 1}   # a 1D run or a fixed-mask job can always go somewhere
    assert 1 <= physical_cores()
    assert environment("cpu") == {"GRATING_DEVICE": "cpu", "CUDA_VISIBLE_DEVICES": ""} and environment("cuda") == {"GRATING_DEVICE": "cuda"}


def test_a_run_goes_to_the_gpu_when_its_evaluator_can_use_it_and_a_slot_is_free_else_to_cpu():
    lab = pools(4, 1)
    used = lab.used([])
    assert lab.capable(run()) == ("cpu", "cuda") and lab.capable(run(ONE_D)) == ("cpu",)
    first, second = run(id="a"), run(id="b")
    assert lab.place(first, used) == ("cuda", None)
    lab.started(first, "cuda", used)
    assert first["device"] == "cuda" and lab.place(second, used) == ("cpu", None)
    # The 1D solver, a commissioned evaluator and a sandboxed run compute only on CPU, whatever is free.
    for cpu_only in (run(ONE_D), run(evaluator_version_id="v"), run(isolation_policy={"kind": "bubblewrap"})):
        assert pools(4, 1).place(cpu_only, {"cuda": 0, "cpu": 0}) == ("cpu", None)
    # A run takes a CPU slot per numerical thread, and at most all of them.
    assert lab.place(run(numerical_threads=3), {"cuda": 1, "cpu": 1}) == (None, "Waits for a free worker slot (1 of 1 GPU slots and 1 of 3 CPU slots taken).")
    assert lab.place(run(numerical_threads=2), {"cuda": 1, "cpu": 1}) == ("cpu", None)
    assert lab.cost("cpu", run(numerical_threads=4)) == 3 and lab.cost("cuda", run(numerical_threads=4)) == 1
    # Running work started before devices were recorded counts on CPU.
    assert lab.used([run(numerical_threads=2), run(device="cuda"), run(device="cpu")], mask_jobs=1) == {"cuda": 1, "cpu": 4}


def test_a_named_device_and_a_protocol_keep_their_device_even_when_the_other_is_free():
    race = [run(id="a", race_id="race_1", device="cuda", status="completed")]
    lab = pools(4, 1, race)
    assert lab.place(run(device="cpu"), {"cuda": 0, "cpu": 0}) == ("cpu", None)
    # The race's first run took the GPU: the next waits for it instead of comparing a CPU run on time.
    device, reason = lab.place(run(id="b", race_id="race_1"), {"cuda": 1, "cpu": 0})
    assert device is None and reason == "Waits for a free slot on the GPU (its race or protocol runs there; 1 of 1 taken)."
    assert lab.place(run(id="b", race_id="race_1"), {"cuda": 0, "cpu": 3}) == ("cuda", None)
    # A race none of whose runs has started yet takes whatever its first run gets, and keeps it.
    fresh = pools(4, 1)
    used = fresh.used([])
    first = run(id="c", race_id="race_2")
    assert fresh.place(first, used) == ("cuda", None)
    fresh.started(first, "cuda", used)
    assert fresh.place(run(id="d", race_id="race_2"), used)[0] is None
    # A lab restarted without a GPU worker says why such a run cannot start, instead of moving it.
    assert pools(4, 0, race).place(run(id="b", race_id="race_1"), {"cuda": 0, "cpu": 0})[1].endswith("this lab now runs no worker there.")


def test_a_named_device_is_refused_when_queued_if_this_lab_cannot_give_it():
    with pytest.raises(ValueError, match="computes only on CPU"):
        pools().check(run(ONE_D, device="cuda"))
    with pytest.raises(ValueError, match="runs no GPU workers"):
        pools(4, 0).check(run(device="cuda"))
    pools().check(run(device="cuda"))
    assert pools(4, 0).reachable(run()) == ("cpu",) and pools(4, 1).reachable(run()) == ("cpu", "cuda")


def level(device, workers, rate, **load):
    return {"device": device, "workers": workers, "status": "measured", "evaluations_per_second": rate, "overhead_share": .05,
            "bytes_per_evaluation": 40000., "solver_executions_per_evaluation": 1., **load}


def test_a_profile_judges_each_device_on_its_own_load_and_adds_their_speeds():
    # The 2D deflector on this host: one GPU worker fills the card; CPU workers scale with the cores they find free.
    gpu = derive([level("cuda", 1, 3.47, gpu_percent=88., cpu_percent=4.5)], 1, 65537, device="cuda")
    cpu = derive([level("cpu", 1, .46, cpu_core_percent=7., gpu_percent=0.), level("cpu", 2, .92, cpu_core_percent=13.),
                  level("cpu", 4, 1.8, cpu_core_percent=26.), level("cpu", 8, 3.5, cpu_core_percent=51.),
                  level("cpu", 14, 5.9, cpu_core_percent=88.)], 14, 65537, device="cpu")
    assert gpu["verdict"] == "passed" and gpu["recommended_workers"] == 1
    assert [flag["id"] for flag in cpu["flags"]] == ["unsaturated"] and cpu["recommended_workers"] == 14
    summary = combine({"cuda": gpu, "cpu": cpu}, 65537)
    assert summary["verdict"] == "passed" and summary["recommended_workers"] == 15
    assert summary["evaluations_per_hour"] == pytest.approx(3600 * (3.47 + 5.9))
    assert summary["flags"][0]["device"] == "cpu" and summary["flags"][0]["message"].startswith("CPU: Throughput was still growing")
    # A CPU level is busy by its physical cores, not by the idle GPU beside it.
    stalled = derive([level("cpu", 1, .46, cpu_core_percent=7.), level("cpu", 2, .5, cpu_core_percent=13.)], 2, 65537, device="cpu")
    assert [flag["id"] for flag in stalled["flags"]] == ["no_scaling"]
    # One device alone reads as before, with its part kept.
    assert combine({"cpu": cpu}, 65537)["flags"][0]["message"].startswith("Throughput")
    unmeasured = combine({"cuda": gpu, "cpu": {"verdict": "inconclusive", "flags": [], "reason": "too short"}}, 65537)
    assert unmeasured["verdict"] == "inconclusive" and unmeasured["reason"] == "CPU: too short"


def test_the_scheduler_records_where_a_run_computes_and_its_worker_sees_only_that(tmp_path, monkeypatch):
    import time
    from optimization_framework.contracts.requests import CampaignInput, ControlInput, TaskInput, TrialInput
    from optimization_framework.execution.service import Workspace, alive
    monkeypatch.setenv("GRATING_LLM_DISABLED", "true")
    workspace = Workspace(tmp_path / "workspace", max_workers=3, gpu_workers=1)
    try:
        campaign = workspace.create_campaign(CampaignInput(name="Placed", compute_budget_seconds=200, validation_reserve_seconds=0,
            tasks=[TaskInput(name="Continuous", problem_id="bounded_continuous", configuration={"dimensions": 2})]))
        task = workspace.current_tasks(campaign["id"])[0]
        with pytest.raises(ValueError, match="computes only on CPU"):
            workspace.create_trial(TrialInput(campaign_id=campaign["id"], task_id=task["id"], algorithm="random", device="cuda"))
        trial = workspace.create_trial(TrialInput(campaign_id=campaign["id"], task_id=task["id"], algorithm="random", max_steps=10 ** 6, wall_seconds=20))
        workspace.start()
        deadline = time.monotonic() + 20
        while not alive(record := workspace.store.get(trial["id"], "trial")) and time.monotonic() < deadline:
            time.sleep(.05)
        assert record["device"] == "cpu"
        environ = dict(item.split("=", 1) for item in open(f"/proc/{record['pid']}/environ").read().split("\0") if "=" in item)
        assert environ["GRATING_DEVICE"] == "cpu" and environ["CUDA_VISIBLE_DEVICES"] == ""
        assert workspace.admission.view()["pools"] == {"cuda": {"slots": 1, "taken": 0}, "cpu": {"slots": 2, "taken": 1}}
        workspace.control(trial["id"], ControlInput(action="stop"))
    finally:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and any(alive(row) for row in workspace.store.list("trial")):
            time.sleep(.05)
        workspace.close()
