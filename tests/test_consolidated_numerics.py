"""Compare the common optimizer lifecycle against independently captured runs."""
import json
import random
from copy import deepcopy
from pathlib import Path

import numpy as np
import pytest
import torch
from threadpoolctl import threadpool_limits

from dqn_meent.config import PhysicsConfig
from dqn_meent.physics import ForwardSolver
from optimization_framework.optimizers.binary import make_optimizer


REFERENCE = json.loads((Path(__file__).parent / "fixtures/consolidation-numerical-reference.json").read_text())
# The references were captured with the PyPI MEENT release's NumPy solver. The lab's MEENT, the meent-private fork on Torch,
# reproduces every efficiency to about 3e-11 and every decision, count, loss and weight exactly: values compare to this.
EFFICIENCY = 1e-10


@pytest.mark.parametrize("profile", ["finite", "continuing_public", "continuing_default_init"])
def test_shared_learner_matches_frozen_sibling_trace_replay_and_networks(profile):
    reference = REFERENCE["learners"][profile]
    settings = reference["config"]["training"]
    physics = PhysicsConfig(**reference["config"]["physics"])
    with threadpool_limits(limits=1):
        optimizer = make_optimizer("dqn", physics.n_cells, settings["seed"], settings["total_steps"], training=settings)
        solver = ForwardSolver(physics)
        trace = []
        while optimizer.decisions < settings["total_steps"]:
            design = optimizer.ask()
            previous = optimizer.decisions
            value = solver.evaluate(design).efficiency
            optimizer.tell(design, value)
            if optimizer.decisions > previous:
                trace.append(value)
        assert trace == pytest.approx([float(row["efficiency"]) for row in reference["trace"]], abs=EFFICIENCY, rel=0)
        assert optimizer.updates == reference["summary"]["updates"]
        assert optimizer.loss == reference["summary"]["last_loss"]
        assert solver.evaluations == reference["summary"]["evaluations"]
        assert solver.solver_calls == reference["summary"]["solver_calls"]
        for actual, saved in ((optimizer.online.state_dict(), reference["weights"]), (optimizer.target.state_dict(), reference["target"])):
            for key, tensor in actual.items():
                torch.testing.assert_close(tensor, torch.tensor(saved[key], dtype=tensor.dtype), rtol=0, atol=0)
        state = optimizer.replay.state_dict()
        for key, values in reference["replay"].items():
            np.testing.assert_allclose(state[key][:state["size"]], np.asarray(values, dtype=state[key].dtype), rtol=0, atol=EFFICIENCY)


@pytest.mark.parametrize("method", ["hillclimb", "refinement"])
def test_consolidated_controls_match_frozen_sibling_sequences_and_restore(method):
    reference = REFERENCE["controls"][method]
    physics = PhysicsConfig(**reference["config"]["physics"])
    seed = reference["config"]["training"]["seed"]
    parameters = ({"neighborhood": "random", "accept_equal": True, "initialization": "ones", "restart_patience": 2 * physics.n_cells}
                  if method == "hillclimb" else {"initial_design": reference["initial_design"]})
    with threadpool_limits(limits=1):
        optimizer = make_optimizer(method, physics.n_cells, seed, len(reference["trace"]), parameters)
        solver = ForwardSolver(physics)
        for i, row in enumerate(reference["trace"]):
            if i == 17:
                state = optimizer.state_dict()
                restored = make_optimizer(method, physics.n_cells, seed, len(reference["trace"]), parameters)
                restored.load_state_dict(state)
                optimizer = restored
            candidate = optimizer.ask()
            value = solver.evaluate(candidate).efficiency
            optimizer.tell(candidate, value)
            assert value == pytest.approx(float(row["efficiency"]), abs=EFFICIENCY, rel=0), (method, i)
            assert optimizer.best_efficiency == pytest.approx(float(row["best_efficiency"]), abs=EFFICIENCY, rel=0)
            assert solver.solver_calls == int(row["solver_calls"])
            assert solver.cache_hits == int(row["cache_hits"])


@pytest.mark.parametrize("profile", ["finite", "continuing_public"])
def test_diagnostics_and_exports_do_not_change_any_training_random_stream(profile):
    reference = REFERENCE["learners"][profile]
    settings = reference["config"]["training"]
    physics = PhysicsConfig(**reference["config"]["physics"])
    optimizer = make_optimizer("dqn", physics.n_cells, settings["seed"], settings["total_steps"], training=settings)
    for _ in range(12):
        candidate = optimizer.ask()
        optimizer.tell(candidate, float(candidate.mean()))
    before_python, before_numpy, before_torch = random.getstate(), np.random.get_state(), torch.get_rng_state().clone()
    before_generator = deepcopy(optimizer.rng.bit_generator.state)
    report = optimizer.q_diagnostics(reward_upper_bound=1.)
    assert report["sampled_states"] > 0 and report["initial_return_bound"] > 0
    artifacts = optimizer.export_artifacts()
    assert {item["kind"] for item in artifacts} == {"policy", "learner_diagnostics"}
    assert random.getstate() == before_python
    np.testing.assert_array_equal(np.random.get_state()[1], before_numpy[1])
    assert np.random.get_state()[2:] == before_numpy[2:]
    torch.testing.assert_close(torch.get_rng_state(), before_torch, rtol=0, atol=0)
    assert optimizer.rng.bit_generator.state == before_generator
