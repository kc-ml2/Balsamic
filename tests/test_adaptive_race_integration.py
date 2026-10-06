"""Execution policy and independent deadline ownership, using real child PIDs."""
import json
import signal
import subprocess
import sys
import time

import pytest

from optimization_framework.contracts.requests import TrialInput
from optimization_framework.execution.race_guard import enforce, owned_processes, process_identity, signal_owned
from optimization_framework.execution.worker import fingerprint
from optimization_framework.storage.sqlite import Store


def test_default_thread_policy_preserves_legacy_checkpoint_identity():
    legacy = {"id": "checkpoint", "algorithm": "random", "seed": 17}
    assert fingerprint(legacy) == fingerprint({**legacy, "numerical_threads": 1})
    assert fingerprint(legacy) != fingerprint({**legacy, "numerical_threads": 2})
    assert fingerprint(legacy) != fingerprint({**legacy, "numerical_threads": 4})


@pytest.mark.parametrize("threads", [0, 5, 1.5, True])
def test_threads_are_bounded_integers(threads):
    with pytest.raises(ValueError):
        TrialInput(campaign_id="campaign", task_id="task", numerical_threads=threads)


def test_deadline_guard_kills_only_verified_race_owned_processes(tmp_path):
    store = Store(tmp_path)
    owned = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        for identity, pid, race in (("owned", owned.pid, "race_test"), ("other", other.pid, "race_other")):
            store.put("trial", {"id": identity, "campaign_id": "campaign", "race_id": race,
                "status": "running", "pid": pid, "process_identity": process_identity(pid)})
        rows = owned_processes(store.path, "race_test")
        assert len(rows) == 1
        assert signal_owned([("unverified", other.pid, "wrong-start-identity")], signal.SIGTERM) == []
        enforce(store.path, "race_test", time.time() + .2, tmp_path, grace=.1)
        owned.wait(timeout=3)
        assert other.poll() is None
        receipt = json.loads((tmp_path / "deadline-receipt.json").read_text())
        assert receipt["cooperative_stop"] == ["owned"]
        assert receipt["enforced_at"] >= receipt["deadline_at"]
    finally:
        for process in (owned, other):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)


def test_missing_process_identity_cannot_signal_an_unverified_pid():
    assert signal_owned([("missing", 1, None)], signal.SIGKILL) == []


def test_adam_calibration_completes_one_actual_gradient(tmp_path):
    from dqn_meent.problem_2d import Meent2DProblem
    from optimization_framework.execution.worker import run
    from optimization_framework.optimizers.registry import capabilities
    from optimization_framework.storage.artifacts import atomic_json
    from test_meent_2d_problem import configuration

    assert "optimizer_decisions" in capabilities("flrl_autograd_adam").completion_units
    problem = Meent2DProblem().resolve(configuration(), {"rcwa_order_x": 1, "rcwa_order_y": 1})
    atomic_json(tmp_path / "spec.json", {"id": "calibration", "campaign_id": "campaign", "study_id": "study",
        "problem": problem.model_dump(mode="json"), "algorithm": "flrl_autograd_adam",
        "algorithm_config": {"level_set_modes_x": 2, "level_set_modes_y": 1}, "seed": 173,
        "max_steps": 100000, "schedule_steps": 100000, "wall_seconds": 60,
        "completion": {"unit": "optimizer_decisions", "count": 1}, "numerical_threads": 1})
    result = run(tmp_path)
    assert result["status"] == "completed", result.get("reason")
    assert result["scientific_complete"]
    assert result["diagnostics"]["gradient_evaluations"] == 1
    assert result["diagnostics"]["decisions"] == 1
    assert result["confirmed_observations"] == 1
