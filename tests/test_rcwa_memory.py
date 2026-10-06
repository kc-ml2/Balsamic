"""Resource plans distinguish source-based array sizes from historical RSS forecasts."""
import json
import subprocess
import sys

import pytest

from optimization_framework.execution.rcwa_memory import GIB, estimate_rcwa_memory


def fidelity(x, y):
    return {"rcwa_order_x": x, "rcwa_order_y": y}


def observed(**overrides):
    return {"fidelity": fidelity(18, 9), "phase": "validation", "completed_evaluations": 4,
            "peak_rss_bytes": 6_653_087_744, "trial_id": "ppo_median", "pid": 3819313,
            "process_identity": "31491698", "sampled_at": 1790839634.4116828, **overrides}


def test_actual_order_22_expanded_fft_and_em_matrix_sizes():
    plan = estimate_rcwa_memory(fidelity(22, 11))
    assert plan["harmonic_count"] == 1035
    assert plan["single_matrix_bytes"] == 17_139_600
    assert plan["em_block_matrix_bytes"] == 68_558_400
    assert plan["expanded_grid_shape"] == {"x": 23040, "y": 5888}
    assert plan["expanded_real_grid_bytes"] == 1_085_276_160
    assert plan["expanded_complex_grid_bytes"] == 2_170_552_320
    assert plan["fft_workspace_lower_bound_bytes"] == 5_477_799_600
    assert plan["analytical_lower_bound_bytes"] == 5_477_799_600
    assert plan["measured_peak_bytes"] is None
    assert plan["estimate_kind"] == "heuristic"


def test_exact_historical_prediction_is_not_presented_as_measured_usage():
    plan = estimate_rcwa_memory(fidelity(22, 11), [observed()], available_memory_bytes=18_944_090_112)
    assert plan["predicted_peak_bytes"] == 28_841_862_122
    assert plan["measured_peak_bytes"] == 6_653_087_744
    assert plan["measured_fidelity"] == fidelity(18, 9)
    assert plan["unmargined_peak_bytes"] == 14_420_931_061
    assert plan["uncertainty_factor"] == 2
    assert plan["fits_available_memory"] is False
    assert plan["estimated_safe_concurrency"] == 0
    assert plan["provenance"]["reference_trial_id"] == "ppo_median"
    assert "not measured" in plan["basis"]
    assert plan["fft_grid_growth_ratio"] < plan["dense_matrix_growth_ratio"]


def test_multiple_polls_are_not_multiple_independent_measurements():
    samples = [observed(peak_rss_bytes=6_000_000_000), observed(), observed(sampled_at=1790839635),
               observed(trial_id="adam", pid=4, process_identity="5", peak_rss_bytes=6_500_000_000)]
    plan = estimate_rcwa_memory(fidelity(22, 11), samples)
    assert plan["measurement_count"] == 2
    assert plan["raw_measurement_count"] == 4
    assert plan["measured_peak_bytes"] == 6_653_087_744
    assert plan["provenance"]["source_trial_ids"] == ["adam", "ppo_median"]


def test_only_completed_valid_validation_samples_at_highest_order_are_used():
    samples = [observed(fidelity=fidelity(14, 7), peak_rss_bytes=50 * GIB), observed(),
               observed(completed_evaluations=0, peak_rss_bytes=100 * GIB),
               observed(phase="measurement", peak_rss_bytes=100 * GIB),
               observed(fidelity=fidelity(-1, 9)), observed(peak_rss_bytes=float("nan")),
               {"peak_rss_bytes": 100 * GIB}, None]
    plan = estimate_rcwa_memory(fidelity(22, 11), samples)
    assert plan["raw_measurement_count"] == 1
    assert plan["predicted_peak_bytes"] == 28_841_862_122


def test_unmeasured_allowance_does_not_apply_the_uncertainty_margin_twice():
    plan = estimate_rcwa_memory(fidelity(14, 7))
    assert plan["predicted_peak_bytes"] == 5 * GIB
    assert plan["uncertainty_factor"] == 1
    assert plan["measurement_count"] == 0
    assert plan["fits_available_memory"] is None
    assert plan["estimated_safe_concurrency"] is None


def test_zero_order_preserves_raster_without_spurious_doubling():
    plan = estimate_rcwa_memory(fidelity(0, 0))
    assert plan["expanded_grid_shape"] == {"x": 256, "y": 128}


def test_forecast_array_sizes_follow_the_actual_task_grid():
    small = estimate_rcwa_memory(fidelity(22, 11), grid_x=4, grid_y=2)
    paper = estimate_rcwa_memory(fidelity(22, 11))
    assert small["expanded_grid_shape"] == {"x": 360, "y": 92}
    assert small["expanded_complex_grid_bytes"] == 529_920
    assert small["expanded_complex_grid_bytes"] < paper["expanded_complex_grid_bytes"]
    # Fourier order, rather than raster resolution, controls the EM matrix size.
    assert small["single_matrix_bytes"] == paper["single_matrix_bytes"]
    assert small["configuration"]["grid_x"] == 4
    assert small["configuration"]["grid_y"] == 2


def test_disabled_enhancement_uses_actual_meent_repeat_boundary():
    # A dimension equal to the minimum is retained; a smaller dimension repeats
    # to strictly exceed the minimum, as in MEENT's repeat_interleave implementation.
    plan = estimate_rcwa_memory(fidelity(2, 1), grid_x=3, grid_y=5, enhanced_dfs=False)
    assert plan["expanded_grid_shape"] == {"x": 12, "y": 5}


def test_continuous_fourier_mode_does_not_claim_enhanced_fft_allocations():
    plan = estimate_rcwa_memory(fidelity(22, 11), fourier_type=1)
    assert plan["expanded_grid_shape"] is None
    assert plan["fft_workspace_lower_bound_bytes"] == 0
    assert plan["analytical_lower_bound_bytes"] == plan["retained_matrix_lower_bound_bytes"]


def test_headroom_is_reserved_once_when_reporting_safe_worker_count():
    plan = estimate_rcwa_memory(fidelity(14, 7), available_memory_bytes=14 * GIB)
    assert plan["estimated_safe_concurrency"] == 2
    assert plan["fits_available_memory"] is True
    assert estimate_rcwa_memory(fidelity(14, 7), available_memory_bytes=3 * GIB)["estimated_safe_concurrency"] == 0


@pytest.mark.parametrize("kwargs", [{"grid_x": 0}, {"layers": 0}, {"uncertainty_factor": float("inf")},
                                    {"uncertainty_factor": .5}, {"available_memory_bytes": -1},
                                    {"enhanced_dfs": 1}, {"fourier_type": 2}])
def test_invalid_resource_inputs_are_rejected(kwargs):
    with pytest.raises(ValueError):
        estimate_rcwa_memory(fidelity(22, 11), **kwargs)


def test_cli_reads_existing_samples_without_loading_numerical_libraries(tmp_path):
    saved = tmp_path / "preflight.json"
    saved.write_text(json.dumps({"worker_memory_samples": [observed()]}))
    result = subprocess.run([sys.executable, "-m", "optimization_framework.execution.rcwa_memory",
                             "--order-x", "22", "--order-y", "11", "--samples", str(saved),
                             "--available-gib", "17.64"], check=True, text=True, capture_output=True)
    assert json.loads(result.stdout)["predicted_peak_bytes"] == 28_841_862_122
    subprocess.run([sys.executable, "-c", "import sys; "
                    "from optimization_framework.execution.rcwa_memory import estimate_rcwa_memory; "
                    "estimate_rcwa_memory({'rcwa_order_x':22,'rcwa_order_y':11}); "
                    "assert 'meent' not in sys.modules and 'torch' not in sys.modules"], check=True)
