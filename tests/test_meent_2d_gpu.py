"""The 2D evaluator on CUDA must reproduce itself on CPU: the same installed fork, two devices.

Skipped without a CUDA device.
"""
import numpy as np
import pytest

pytest.importorskip("meent")
torch = pytest.importorskip("torch")

from dqn_meent.fourier import FourierGeometry
from dqn_meent.problem_2d import Meent2DProblem
from optimization_framework.execution.source import runtime_identity
from test_meent_2d_problem import configuration

pytestmark = pytest.mark.skipif(not torch.cuda.is_available(), reason="needs a CUDA device")


def test_cuda_matches_cpu_scores_and_gradients(monkeypatch):
    adapter = Meent2DProblem()
    # 91 harmonics: large enough for the fork's mixed-precision eigensolver route on CUDA.
    instance = adapter.resolve(configuration(), {"rcwa_order_x": 6, "rcwa_order_y": 3})
    geometry = FourierGeometry(2, 1, 16, 8)
    rng = np.random.default_rng(3)
    coefficients = [rng.normal(size=geometry.dimensions) for _ in range(3)]
    monkeypatch.setenv("GRATING_DEVICE", "cpu")
    reference = adapter.evaluator(instance)
    monkeypatch.setenv("GRATING_DEVICE", "cuda")
    accelerated = adapter.evaluator(instance)
    assert runtime_identity()["meent_source"].split("@")[0].endswith("meent-private")
    for vector in coefficients:
        expected, measured = reference.evaluate(geometry.mask(vector)), accelerated.evaluate(geometry.mask(vector))
        for name, value in expected.objectives.items():
            assert measured.objectives[name] == pytest.approx(value, abs=1e-9)
        assert (expected.metadata["meent"]["device"], measured.metadata["meent"]["device"]) == ("cpu", "cuda")
        assert measured.metadata["meent"]["source"] == expected.metadata["meent"]["source"] == runtime_identity()["meent_source"]
        request = geometry.gradient_request(vector, 8.0)
        a, b = reference.relaxed_gradient(request), accelerated.relaxed_gradient(request)
        np.testing.assert_allclose(b["gradient"], a["gradient"], rtol=0, atol=1e-7)
        assert b["mean"] == pytest.approx(a["mean"], abs=1e-9)
