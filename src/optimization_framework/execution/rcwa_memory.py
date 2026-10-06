"""Plan MEENT 0.13.2 CPU memory without importing or executing a simulator.

Array sizes are analytical lower bounds. The RSS extrapolation is a separate,
conservative planning heuristic, not a measurement of the requested order.
"""
from __future__ import annotations

import argparse
import json
import math
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any


GIB = 1024**3
DEFAULT_REFERENCE_FIDELITY = {"rcwa_order_x": 14, "rcwa_order_y": 7}
SOURCE_PROVENANCE = [
    "MEENT 0.13.2 on_torch/emsolver/_base.py:solve_2d",
    "MEENT 0.13.2 on_torch/emsolver/transfer_method.py:transfer_2d_1, transfer_2d_2, transfer_2d_3",
    "MEENT 0.13.2 on_torch/emsolver/convolution_matrix.py:to_conv_mat_raster_discrete",
    "MEENT 0.13.2 on_torch/emsolver/fourier_analysis.py:dfs2d",
]


def _integer(value: Any, name: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _fidelity(value: Mapping[str, Any]) -> dict[str, int]:
    return {key: _integer(value[key], key) for key in ("rcwa_order_x", "rcwa_order_y")}


def harmonic_count(fidelity: Mapping[str, Any]) -> int:
    orders = _fidelity(fidelity)
    return (2 * orders["rcwa_order_x"] + 1) * (2 * orders["rcwa_order_y"] + 1)


def _expanded_dimension(size: int, order: int, enhanced: bool) -> int:
    # Match MEENT's strict '<' check and integer quotient '+ 1', including n=0.
    minimum = (4 * order + 1) * size if enhanced else 4 * order + 1
    return size * (minimum // size + 1) if size < minimum else size


def _positive_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def estimate_rcwa_memory(
    fidelity: Mapping[str, Any],
    samples: Iterable[Mapping[str, Any]] = (),
    *,
    available_memory_bytes: int | None = None,
    memory_headroom_bytes: int = 4 * GIB,
    floor_bytes: int = 5 * GIB,
    uncertainty_factor: float = 2.0,
    grid_x: int = 256,
    grid_y: int = 128,
    layers: int = 1,
    enhanced_dfs: bool = True,
    fourier_type: int = 0,
) -> dict[str, Any]:
    """Return transparent sizes and the existing conservative RSS growth model.

    Samples must describe completed validation evaluations. They are historical
    records supplied by the identity-checking sampler; this function cannot
    authenticate a PID that has already exited. RSS is the whole worker's peak,
    not the isolated allocation of one solve. Repeated polls are deduplicated
    for the displayed measurement count, while the largest peak is retained.
    """
    orders = _fidelity(fidelity)
    grid_x, grid_y = _integer(grid_x, "grid_x", 1), _integer(grid_y, "grid_y", 1)
    layers = _integer(layers, "layers", 1)
    floor_bytes = _integer(floor_bytes, "floor_bytes")
    headroom = _integer(memory_headroom_bytes, "memory_headroom_bytes")
    if available_memory_bytes is not None:
        available_memory_bytes = _integer(available_memory_bytes, "available_memory_bytes")
    if not _positive_number(uncertainty_factor) or uncertainty_factor < 1:
        raise ValueError("uncertainty_factor must be finite and >= 1")
    if type(enhanced_dfs) is not bool or type(fourier_type) is not int or fourier_type not in (0, 1):
        raise ValueError("enhanced_dfs must be boolean and fourier_type must be 0 or 1")

    harmonics = harmonic_count(orders)
    matrix = 16 * harmonics**2  # complex128, N x N
    block = 4 * matrix  # electromagnetic blocks are 2N x 2N
    # Three convolution matrices per layer; W/V/A_i/B retained for each
    # layer, plus current F/G/T/X. Views into convolution arrays aren't counted twice.
    retained_matrix = (19 * layers + 16) * matrix
    if fourier_type == 0:
        expanded_x = _expanded_dimension(grid_x, orders["rcwa_order_x"], enhanced_dfs)
        expanded_y = _expanded_dimension(grid_y, orders["rcwa_order_y"], enhanced_dfs)
        grid_points = expanded_x * expanded_y
        real_grid, complex_grid = 8 * grid_points, 16 * grid_points
        # The caller's real epsilon grid remains alive while dfs2d retains
        # both its converted complex cell and its full first FFT result.
        # Ignore transient normalization buffers and FFT library scratch.
        fft_lower_bound = real_grid + 2 * complex_grid + 3 * layers * matrix
        expanded_shape: dict[str, int] | None = {"x": expanded_x, "y": expanded_y}
    else:
        expanded_shape = None
        grid_points = real_grid = complex_grid = fft_lower_bound = 0
    analytical_lower_bound = max(retained_matrix, fft_lower_bound)

    measured = []
    for sample in samples:
        if not isinstance(sample, Mapping):
            continue
        if (sample.get("phase") != "validation"
                or not _positive_number(sample.get("completed_evaluations"))
                or not _positive_number(sample.get("peak_rss_bytes"))):
            continue
        try:
            reference_orders = _fidelity(sample["fidelity"])
        except (KeyError, TypeError, ValueError):
            continue
        measured.append({**sample, "fidelity": reference_orders})

    if measured:
        highest = max(harmonic_count(sample["fidelity"]) for sample in measured)
        measured = [sample for sample in measured if harmonic_count(sample["fidelity"]) == highest]
        worst = max(measured, key=lambda sample: sample["peak_rss_bytes"])
        reference_orders = worst["fidelity"]
        reference_peak = int(worst["peak_rss_bytes"])
        factor = float(uncertainty_factor)
        basis = "Observed validation worker peak RSS, extrapolated; requested-order peak is not measured"
        method = "measured_rss_extrapolation"
    else:
        reference_orders = dict(DEFAULT_REFERENCE_FIDELITY)
        reference_peak = 5 * GIB
        factor = 1.0  # The unmeasured allowance already includes conservatism.
        basis = "Unmeasured conservative 5 GiB planning allowance at (14,7)"
        method = "unmeasured_planning_allowance"
        worst = {}

    growth = (harmonics / harmonic_count(reference_orders)) ** 2
    raw_extrapolation = math.ceil(reference_peak * growth)
    prediction = max(floor_bytes, analytical_lower_bound, math.ceil(factor * reference_peak * growth))
    unique_workers = {
        (sample.get("trial_id"), sample.get("pid"), sample.get("process_identity"),
         sample["fidelity"]["rcwa_order_x"], sample["fidelity"]["rcwa_order_y"])
        for sample in measured
    }
    current_fits = None if available_memory_bytes is None else prediction + headroom <= available_memory_bytes
    safe_workers = (None if available_memory_bytes is None else
                    max(0, (available_memory_bytes - headroom) // max(1, prediction)))
    reference_grid = None
    if fourier_type == 0:
        reference_grid = (_expanded_dimension(grid_x, reference_orders["rcwa_order_x"], enhanced_dfs)
                          * _expanded_dimension(grid_y, reference_orders["rcwa_order_y"], enhanced_dfs))

    return {
        "fidelity": orders, "harmonic_count": harmonics, "complex_element_bytes": 16,
        "single_matrix_bytes": matrix, "em_block_matrix_bytes": block,
        "retained_matrix_lower_bound_bytes": retained_matrix,
        "expanded_grid_shape": expanded_shape, "expanded_grid_points": grid_points,
        "expanded_real_grid_bytes": real_grid, "expanded_complex_grid_bytes": complex_grid,
        "fft_workspace_lower_bound_bytes": fft_lower_bound,
        "analytical_lower_bound_bytes": analytical_lower_bound,
        "predicted_peak_bytes": prediction, "unmargined_peak_bytes": raw_extrapolation,
        "measured_peak_bytes": reference_peak if measured else None,
        "measured_fidelity": reference_orders if measured else None,
        "measurement_count": len(unique_workers), "raw_measurement_count": len(measured),
        "memory_headroom_bytes": headroom, "available_memory_bytes": available_memory_bytes,
        "fits_available_memory": current_fits, "estimated_safe_concurrency": safe_workers,
        "basis": basis, "estimate_kind": "heuristic", "uncertainty_factor": factor,
        "growth_model": "uncertainty factor * reference worker peak * (target harmonics / reference harmonics)^2",
        "dense_matrix_growth_ratio": growth,
        "fft_grid_growth_ratio": grid_points / reference_grid if reference_grid else None,
        "configuration": {"grid_x": grid_x, "grid_y": grid_y, "layers": layers,
                          "enhanced_dfs": enhanced_dfs, "fourier_type": fourier_type,
                          "dtype": "complex128", "execution": "CPU forward solve"},
        "provenance": {
            "analytical_sources": SOURCE_PROVENANCE,
            "forecast_method": method, "reference_fidelity": reference_orders,
            "reference_peak_bytes": reference_peak,
            "reference_trial_id": worst.get("trial_id"), "reference_pid": worst.get("pid"),
            "reference_process_identity": worst.get("process_identity"),
            "reference_sampled_at": worst.get("sampled_at"),
            "measurement_scope": "Whole worker VmHWM after completed evaluations, potentially across multiple solves",
            "source_trial_ids": sorted({sample["trial_id"] for sample in measured
                                        if isinstance(sample.get("trial_id"), str)}),
        },
        "limitations": [
            "Analytical array sizes are lower bounds, not safe peak-memory requirements.",
            "FFT/eigensolver scratch, allocator retention, runtime overhead and autograd are not analytically bounded.",
            "Quadratic harmonic RSS extrapolation can overestimate FFT-dominated growth; its uncertainty factor is a policy allowance.",
            "A numerical convergence requirement cannot be determined from memory sizes.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--order-x", type=int, required=True)
    parser.add_argument("--order-y", type=int, required=True)
    parser.add_argument("--grid-x", type=int, default=256)
    parser.add_argument("--grid-y", type=int, default=128)
    parser.add_argument("--layers", type=int, default=1)
    parser.add_argument("--samples", type=Path, help="Preflight manifest or JSON list of observed worker samples")
    parser.add_argument("--available-gib", type=float)
    args = parser.parse_args()
    samples = []
    if args.samples:
        saved = json.loads(args.samples.read_text())
        samples = (saved if isinstance(saved, list) else
                   saved.get("worker_memory_samples", saved.get("profile", {}).get("validation_memory_samples", [])))
    available = None if args.available_gib is None else int(args.available_gib * GIB)
    print(json.dumps(estimate_rcwa_memory(
        {"rcwa_order_x": args.order_x, "rcwa_order_y": args.order_y}, samples,
        grid_x=args.grid_x, grid_y=args.grid_y, layers=args.layers, available_memory_bytes=available,
    ), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
