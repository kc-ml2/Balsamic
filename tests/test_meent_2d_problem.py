"""Numerical checks for the installed two-dimensional MEENT problem."""
import math

import pytest

pytest.importorskip("meent")
torch = pytest.importorskip("torch")

from dqn_meent.problem_2d import Meent2DProblem
from optimization_framework.contracts.requests import TaskInput
from optimization_framework.contracts.commands import CampaignUpdateInput


def configuration():
    return {
        "wavelength_nm": 1050, "target_angle_deg": 75,
        "period_x_nm": 1050 / math.sin(math.radians(75)), "period_y_nm": 525,
        "thickness_nm": 325, "grid_x": 16, "grid_y": 8,
        "incident_n": 1.45, "exit_n": 1.0, "incident_angle_deg": 0,
        "target_order_x": 1, "target_order_y": 0,
        "silicon_index_source": "FLRL Si_refractive_data.csv",
        "silicon_n_1050": 3.567390909090909,
        "reference_meent_version": "0.9.5",
    }


def test_uniform_layers_have_no_first_order_and_conserve_power():
    adapter = Meent2DProblem()
    instance = adapter.resolve(configuration(), {"rcwa_order_x": 1, "rcwa_order_y": 1})
    evaluator = adapter.evaluator(instance)
    for material in (0, 1):
        result = evaluator.evaluate([material] * instance.candidate_schema.dimensions)
        assert result.objectives == {
            "mean_plus1_transmission": 0,
            "te_plus1_transmission": 0,
            "tm_plus1_transmission": 0,
            "min_plus1_transmission": 0,
        }
        assert all(abs(total - 1) < 1e-10 for total in result.metadata["meent"]["energy_totals"])
        assert result.solver_executions == 1


def test_x_invariant_stripe_agrees_with_one_dimensional_meent():
    import meent

    config = configuration()
    instance = Meent2DProblem().resolve(config, {"rcwa_order_x": 2, "rcwa_order_y": 1})
    candidate = ([1] * 8 + [0] * 8) * config["grid_y"]
    measured = Meent2DProblem().evaluator(instance).evaluate(candidate).objectives
    unit_cell = torch.tensor([[config["silicon_n_1050"]] * 8 + [1.0] * 8], dtype=torch.float64).reshape(1, 1, 16)
    reference = meent.call_mee(
        pol=0, n_top=1.45, n_bot=1.0,
        theta=torch.tensor(0.0, dtype=torch.float64), phi=torch.tensor(0.0, dtype=torch.float64),
        fto=[2], wavelength=1050,
        period=torch.tensor([config["period_x_nm"], config["period_y_nm"]], dtype=torch.float64),
        thickness=torch.tensor([325.0], dtype=torch.float64), type_complex=torch.complex128, device="cpu",
    )
    reference.ucell = unit_cell
    for pol, name in ((0, "te_plus1_transmission"), (1, "tm_plus1_transmission")):
        reference.pol = pol
        orders = reference.conv_solve().res.de_ti
        assert measured[name] == pytest.approx(float(orders[..., orders.shape[-1] // 2 + 1].item()), abs=1e-10)
    assert measured["mean_plus1_transmission"] == pytest.approx(
        (measured["te_plus1_transmission"] + measured["tm_plus1_transmission"]) / 2)


def test_invalid_binary_candidate_is_rejected():
    instance = Meent2DProblem().resolve(configuration(), {"rcwa_order_x": 1, "rcwa_order_y": 1})
    with pytest.raises(ValueError, match="binary"):
        Meent2DProblem().evaluator(instance).evaluate([0.5] * instance.candidate_schema.dimensions)


def test_campaign_task_survives_legacy_physics_projection():
    task = TaskInput(name="2D deflector", problem_id="meent_2d_dual_polarization_deflector",
                     configuration=configuration(), fidelity={"rcwa_order_x": 2, "rcwa_order_y": 1})
    request = CampaignUpdateInput(tasks=[task])
    from optimization_framework.contracts.requests import CampaignUpdate
    replayed = CampaignUpdate.model_validate(request.model_dump(mode="json", exclude_none=True,
                                                                exclude={"rationale"}))
    assert replayed.tasks[0].problem == task.problem


def test_implementation_fixture_honors_embedded_physical_fidelity():
    adapter = Meent2DProblem()
    config = {**configuration(), "rcwa_order_x": 10, "rcwa_order_y": 5}
    fixture = adapter.implementation_fixture(config, 16 * 8)
    assert fixture.fidelity == {"rcwa_order_x": 10, "rcwa_order_y": 5}
    fallback = adapter.implementation_fixture(configuration(), 16 * 8)
    assert fallback.fidelity == {"rcwa_order_x": 1, "rcwa_order_y": 1}


def test_one_solve_returns_what_a_solve_per_polarization_returns():
    config = configuration()
    adapter = Meent2DProblem()
    evaluator = adapter.evaluator(adapter.resolve(config, {"rcwa_order_x": 3, "rcwa_order_y": 2}))
    generator = torch.Generator().manual_seed(3)
    candidate = torch.randint(0, 2, (config["grid_x"] * config["grid_y"],), generator=generator).tolist()
    measured = evaluator.evaluate(candidate)
    assert measured.solver_executions == 1
    assert measured.objectives["te_plus1_transmission"] != pytest.approx(measured.objectives["tm_plus1_transmission"], abs=1e-4)
    for pol, name in ((0, "te_plus1_transmission"), (1, "tm_plus1_transmission")):
        evaluator.mee.pol = pol
        orders = evaluator.mee.conv_solve().res.de_ti
        assert measured.objectives[name] == pytest.approx(float(orders[orders.shape[0] // 2, orders.shape[1] // 2 + 1]), abs=1e-12)
