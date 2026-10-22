"""The lab's MEENT: the meent-private fork (branch feature/cuda-acceleration), Torch-only, on CPU or CUDA.

The fork reports the release's version, 0.13.2, and has no NumPy or JAX backend; the PyPI release is refused rather
than run with the same version number. Both the 1D and the 2D evaluator load it here, so a missing PyTorch, MEENT or
fork stops the evaluator with what to install, and the lab's evaluator check reports that before any run.
"""
from __future__ import annotations

import importlib.util
import os
import sys

INSTALL = "install it with scripts/install-meent"
DEVICES = ("cpu", "cuda")


def load():
    """(meent, torch), or an error naming what is missing from the lab's Python environment."""
    try:
        import torch
    except ModuleNotFoundError as error:
        raise ModuleNotFoundError("MEENT runs on PyTorch, which is not installed in the lab's Python environment "
                                  f"({sys.executable}); {INSTALL}") from error
    try:
        import meent
    except ModuleNotFoundError as error:
        raise ModuleNotFoundError(f"MEENT is not installed in the lab's Python environment ({sys.executable}); {INSTALL}") from error
    if importlib.util.find_spec("meent.on_numpy") is not None:
        raise ModuleNotFoundError(f"The installed MEENT {meent.__version__} is the PyPI release, not the meent-private fork "
                                  f"(feature/cuda-acceleration) this lab's evaluators run on; {INSTALL}")
    return meent, torch


def device():
    """Where the 2D solver runs: GRATING_DEVICE, which the scheduler sets for each run, cpu when unset. The 1D solver always runs on CPU."""
    value = os.environ.get("GRATING_DEVICE") or "cpu"
    if value not in DEVICES:
        raise ValueError(f"GRATING_DEVICE is {value!r}; it must be one of {', '.join(DEVICES)}")
    return value


def source():
    """Which build of MEENT is installed: repository@commit for the fork, since its version is the release's."""
    from optimization_framework.execution.source import installed_source
    return installed_source("meent")
