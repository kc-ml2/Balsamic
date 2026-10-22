"""The lab's MEENT is the meent-private fork: the PyPI release with the same version is refused, and the build is recorded."""
import importlib.util

import pytest

pytest.importorskip("torch")
pytest.importorskip("meent")

from dqn_meent import rcwa
from dqn_meent.config import PhysicsConfig
from dqn_meent.physics import ForwardSolver
from optimization_framework.execution.source import runtime_identity


def test_the_pypi_release_is_refused_with_what_to_install(monkeypatch):
    find_spec = importlib.util.find_spec
    monkeypatch.setattr(rcwa.importlib.util, "find_spec", lambda name: object() if name == "meent.on_numpy" else find_spec(name))
    with pytest.raises(ModuleNotFoundError, match="is the PyPI release, not the meent-private fork.*scripts/install-meent"):
        ForwardSolver(PhysicsConfig())


def test_the_device_is_cpu_unless_set_and_only_cpu_or_cuda(monkeypatch):
    monkeypatch.delenv("GRATING_DEVICE", raising=False)
    assert rcwa.device() == "cpu"
    monkeypatch.setenv("GRATING_DEVICE", "cuda")
    assert rcwa.device() == "cuda"
    monkeypatch.setenv("GRATING_DEVICE", "gpu")
    with pytest.raises(ValueError, match="one of cpu, cuda"):
        rcwa.device()


def test_the_installed_build_is_part_of_the_runtime_identity():
    identity = runtime_identity()
    assert identity["meent"] == "0.13.2" and identity["meent_source"] == rcwa.source()
    repository, commit = identity["meent_source"].rsplit("@", 1)
    assert repository.endswith("meent-private") and len(commit.removesuffix("+modified")) >= 7
