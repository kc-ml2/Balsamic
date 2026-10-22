"""An installed evaluator is seen to start in this lab before any experiment or runtime probe is queued on it."""
import pytest

from optimization_framework.contracts.commands import RuntimeProfileInput
from optimization_framework.contracts.requests import CampaignInput, TaskInput, TrialInput
from optimization_framework.evaluation import preflight
from optimization_framework.execution.service import Workspace
from dqn_meent.problem_2d import Meent2DProblem

FLRL = Meent2DProblem().examples()[0]["instances"][0]


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    """Answers belong to this test: a staged failure must not reach another test's identical environment."""
    monkeypatch.setattr(preflight, "_results", {})
    monkeypatch.setattr(preflight, "_running", {})
    monkeypatch.setattr(preflight, "_environment", [0.0, None])


def deflector(tmp_path, gpu_workers=1):
    workspace = Workspace(tmp_path, max_workers=2, gpu_workers=gpu_workers)
    campaign = workspace.create_campaign(CampaignInput(name="Deflector", validation_reserve_seconds=0, tasks=[TaskInput(
        name=FLRL["name"], problem_id=FLRL["problem_id"], configuration=FLRL["configuration"], fidelity=FLRL["fidelity"])]))
    return workspace, campaign["id"], workspace.current_tasks(campaign["id"])[0]


@pytest.fixture
def hidden_gpu(monkeypatch):
    """A lab whose GPU worker sees no CUDA device: the evaluator starts on CPU and not on the GPU."""
    pytest.importorskip("torch")
    pytest.importorskip("meent")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")


def test_a_solver_that_cannot_start_refuses_the_run_and_the_profile_before_anything_is_queued(tmp_path, hidden_gpu):
    workspace, campaign_id, task = deflector(tmp_path / "workspace")
    state = workspace.evaluators.readiness(task)
    assert state["state"] == "cannot_start" and not state["runnable"] and state["device"] == "cuda"
    assert state["error"].startswith("on the GPU: ValueError: This run is placed on the GPU") and "sees no CUDA device" in state["error"]
    assert state["reason"].startswith("The evaluator of meent_2d_dual_polarization_deflector cannot start in this lab on the GPU")
    with pytest.raises(ValueError, match="cannot start in this lab"):
        workspace.create_trial(TrialInput(campaign_id=campaign_id, task_id=task["id"], algorithm="random", max_steps=3, wall_seconds=5))
    with pytest.raises(ValueError, match="cannot start in this lab"):
        workspace.runtime.start(campaign_id, RuntimeProfileInput(task_id=task["id"]))
    assert workspace.store.list("trial", campaign_id) == [] and workspace.store.list("runtime_profile", campaign_id) == []


def test_the_check_repeats_once_the_lab_changes_and_then_lets_the_run_through(tmp_path, hidden_gpu):
    workspace, campaign_id, task = deflector(tmp_path / "workspace")
    assert not workspace.evaluators.readiness(task)["runnable"]
    # The researcher restarts the lab without a GPU worker: only CPU is checked now, and it starts there.
    workspace.gpu_workers = 0
    state = workspace.evaluators.readiness(task)
    assert state["state"] == "ready" and state["devices"] == ["cpu"]
    with pytest.raises(ValueError, match="runs no GPU workers"):
        workspace.create_trial(TrialInput(campaign_id=campaign_id, task_id=task["id"], algorithm="random", max_steps=1, wall_seconds=5, device="cuda"))
    trial = workspace.create_trial(TrialInput(campaign_id=campaign_id, task_id=task["id"], algorithm="random", max_steps=1, wall_seconds=5))
    assert trial["status"] == "queued" and trial["device"] is None


def test_without_waiting_a_first_check_runs_in_the_background(tmp_path, monkeypatch):
    workspace = Workspace(tmp_path)
    campaign = workspace.create_campaign(CampaignInput(name="Quadratic", validation_reserve_seconds=0, tasks=[TaskInput(name="Quadratic", problem_id="bounded_continuous")]))
    task = workspace.current_tasks(campaign["id"])[0]
    import threading
    release = threading.Event()
    monkeypatch.setattr(preflight, "_probe", lambda problem, device: release.wait(10) and {"ok": False, "error": "ModuleNotFoundError: No module named 'scipy'"})
    assert workspace.evaluators.readiness(task, wait=False)["state"] == "checking"
    release.set()
    state = workspace.evaluators.readiness(task)
    assert state["state"] == "cannot_start" and state["error"] == "ModuleNotFoundError: No module named 'scipy'"
    # A commissioned evaluator is not this check's to answer.
    assert preflight.installed({**task, "evaluator_requirement_id": "evaluator_requirement_x"}) is None
