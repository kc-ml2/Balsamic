"""Reviewed 2D pilot fidelity checks must not invent passes from partial evidence."""
from copy import deepcopy
import math

import pytest

from dqn_meent.problem_2d import Meent2DProblem, ORDER_CONVERGENCE_RECIPE
from optimization_framework.contracts.problems import ProblemInstance
from optimization_framework.contracts.problems import Evaluation
from optimization_framework.evaluation.recipes import compile_recipe, recipe_parameters
from optimization_framework.evaluation.registry import problems
from optimization_framework.execution.worker import ExperimentWorker, read_journal
from optimization_framework.storage.artifacts import atomic_json


def instance():
    return Meent2DProblem().resolve({
        "wavelength_nm": 1050, "target_angle_deg": 75,
        "period_x_nm": 1050 / math.sin(math.radians(75)), "period_y_nm": 525,
        "thickness_nm": 325, "grid_x": 4, "grid_y": 2,
        "incident_n": 1.45, "exit_n": 1.0, "incident_angle_deg": 0,
        "target_order_x": 1, "target_order_y": 0,
        "silicon_index_source": "FLRL Si_refractive_data.csv",
        "silicon_n_1050": 3.567390909090909, "reference_meent_version": "0.9.5",
    }, {"rcwa_order_x": 10, "rcwa_order_y": 5})


def plan(parameters=None, subjects=None):
    return compile_recipe(instance(), ORDER_CONVERGENCE_RECIPE, parameters or {},
                          subjects or [[0, 1, 0, 1, 1, 0, 1, 0]])


def observations(recipe):
    result = []
    for index, case in enumerate(recipe["cases"]):
        problem = ProblemInstance(**case["problem"])
        te, tm = [(0.2, 0.4), (0.25, 0.35), (0.253, 0.347)][min(case["fidelity_index"], 2)]
        result.append({"id": f"observation_{index}", "proposal_id": f"case_{index}",
                       "status": "ok", "candidate": case["candidate"],
                       "fidelity": problem.fidelity, "evaluator_identity": problem.evaluation_identity,
                       "objectives": {"mean_plus1_transmission": (te + tm) / 2,
                                      "te_plus1_transmission": te, "tm_plus1_transmission": tm},
                       "metadata": {"meent": {"energy_totals": [1.0, 1.0]}},
                       "costs": {"solver_executions": 2}})
    return result


def summarize(recipe, measured):
    return Meent2DProblem().summarize_recipe(recipe, measured)


def test_2d_recipe_registration_freezes_all_default_settings_and_actual_mask():
    definition = problems.get(instance().definition_id).describe()
    assert definition.validation_recipes == [ORDER_CONVERGENCE_RECIPE]
    schema = definition.recipe_schemas[ORDER_CONVERGENCE_RECIPE]
    assert schema["assertion_kind"] == "solution_fidelity"
    values = recipe_parameters(instance(), ORDER_CONVERGENCE_RECIPE, {})
    assert values["absolute_tolerance"] == .005
    assert values["repeat_tolerance"] == 1e-6
    assert values["energy_tolerance"] == .001
    recipe = plan()
    assert [case["problem"]["fidelity"] for case in recipe["cases"]] == [
        {"rcwa_order_x": 10, "rcwa_order_y": 5}, {"rcwa_order_x": 12, "rcwa_order_y": 6},
        {"rcwa_order_x": 14, "rcwa_order_y": 7}, {"rcwa_order_x": 14, "rcwa_order_y": 7}]
    assert all(case["candidate"] == recipe["subjects"][0] for case in recipe["cases"])
    assert len({case["problem"]["scientific_identity"] for case in recipe["cases"]}) == 1


@pytest.mark.parametrize("parameters", [
    {"orders": [10, 12, 14]}, {"fourier_order": 14},
    {"fidelities": [10, 12, 14]},
    {"fidelities": [{"rcwa_order_x": 10}, {"rcwa_order_x": 14}]},
    {"fidelities": [{"rcwa_order_x": 10, "rcwa_order_y": 5}]},
    {"fidelities": [{"rcwa_order_x": 10, "rcwa_order_y": 5}] * 2},
    {"fidelities": [{"rcwa_order_x": 10, "rcwa_order_y": 5}, {"rcwa_order_x": 14, "rcwa_order_y": 4}]},
    {"fidelities": [{"rcwa_order_x": True, "rcwa_order_y": 5}, {"rcwa_order_x": 14, "rcwa_order_y": 7}]},
    {"absolute_tolerance": .006}, {"repeat_tolerance": 1e-5}, {"energy_tolerance": .002},
    {"absolute_tolerance": float("nan")}, {"repeats": 1}, {"repeats": True},
])
def test_1d_or_invalid_settings_cannot_compile_as_a_2d_assertion(parameters):
    with pytest.raises(ValueError):
        plan(parameters)


def test_passing_requires_mean_both_polarizations_energy_and_repeat_consistency():
    recipe = plan()
    result = summarize(recipe, observations(recipe))
    assert result["complete"] and result["verdict"] == "passed"
    subject = result["subjects"][0]
    assert subject["complete"] and subject["converged"]
    assert subject["last_two_absolute_differences"] == pytest.approx({
        "mean_plus1_transmission": 0, "te_plus1_transmission": .003, "tm_plus1_transmission": .003})
    assert max(subject["repeat_max_absolute_differences"].values()) == 0
    assert subject["max_energy_error"] == 0


def test_mean_agreement_cannot_hide_polarization_disagreement():
    recipe = plan()
    measured = observations(recipe)
    for row in measured[-2:]:
        row["objectives"]["te_plus1_transmission"] += .02
        row["objectives"]["tm_plus1_transmission"] -= .02
    result = summarize(recipe, measured)
    assert result["complete"] and result["verdict"] == "failed"
    assert result["subjects"][0]["last_two_absolute_differences"]["mean_plus1_transmission"] == 0


@pytest.mark.parametrize("missing", ["te_plus1_transmission", "tm_plus1_transmission", "mean_plus1_transmission"])
def test_missing_polarization_never_receives_a_pass(missing):
    recipe = plan()
    measured = observations(recipe)
    del measured[-1]["objectives"][missing]
    result = summarize(recipe, measured)
    assert not result["complete"] and result["verdict"] == "inconclusive"
    assert not result["subjects"][0]["converged"]


@pytest.mark.parametrize("count", [0, 1, 2, 3])
def test_incomplete_fidelity_or_repeat_sequences_remain_inconclusive(count):
    recipe = plan()
    result = summarize(recipe, observations(recipe)[:count])
    assert not result["complete"] and result["verdict"] == "inconclusive"


def test_repeat_disagreement_and_energy_failure_are_not_fidelity_passes():
    recipe = plan()
    measured = observations(recipe)
    measured[-1]["objectives"]["te_plus1_transmission"] += 1e-4
    measured[-1]["objectives"]["mean_plus1_transmission"] += 5e-5
    assert summarize(recipe, measured)["verdict"] == "failed"
    measured = observations(recipe)
    measured[0]["metadata"]["meent"]["energy_totals"] = [1.0, 1.002]
    assert summarize(recipe, measured)["verdict"] == "failed"
    measured = observations(recipe)
    del measured[0]["metadata"]["meent"]["energy_totals"]
    assert summarize(recipe, measured)["verdict"] == "inconclusive"


@pytest.mark.parametrize("mutation", ["proposal_id", "fidelity", "evaluator_identity", "candidate", "status"])
def test_observations_must_match_frozen_case_identity(mutation):
    recipe = plan()
    measured = observations(recipe)
    changed = {"proposal_id": "case_2", "fidelity": {"fourier_order": 14},
               "evaluator_identity": "other_evaluator", "candidate": [0] * 8, "status": "uncertain"}
    measured[-1][mutation] = changed[mutation]
    assert summarize(recipe, measured)["verdict"] == "inconclusive"


def test_higher_fidelity_extensions_are_explicit_and_all_cases_must_complete():
    parameters = {"fidelities": [{"rcwa_order_x": x, "rcwa_order_y": y}
                                 for x, y in ((10, 5), (12, 6), (14, 7), (18, 9))]}
    recipe = plan(parameters, [[0, 1, 0, 1, 1, 0, 1, 0], [1, 0, 1, 0, 0, 1, 0, 1]])
    assert len(recipe["cases"]) == 10
    result = summarize(recipe, observations(recipe))
    assert result["verdict"] == "passed" and len(result["subjects"]) == 2
    result = summarize(recipe, observations(recipe)[:-1])
    assert not result["complete"] and result["verdict"] == "inconclusive"
    assert result["subjects"][0]["verdict"] == "passed"
    assert result["subjects"][1]["verdict"] == "inconclusive"


def test_mutated_parameters_do_not_leak_into_adapter_defaults():
    settings = recipe_parameters(instance(), ORDER_CONVERGENCE_RECIPE, {})
    settings["fidelities"][0]["rcwa_order_x"] = 1
    assert recipe_parameters(instance(), ORDER_CONVERGENCE_RECIPE, {})["fidelities"][0]["rcwa_order_x"] == 10
    assert deepcopy(Meent2DProblem().describe().recipe_schemas)[ORDER_CONVERGENCE_RECIPE]["properties"]["fidelities"]["default"][0]["rcwa_order_x"] == 10


def test_common_worker_recovers_sequence_and_charges_real_repetitions(tmp_path, monkeypatch):
    class ControlledEvaluator:
        def __init__(self, problem):
            self.identity = problem.evaluation_identity
            self.calls = 0

        def evaluate(self, candidate):
            self.calls += 1
            return Evaluation(objectives={"mean_plus1_transmission": .3,
                "te_plus1_transmission": .2, "tm_plus1_transmission": .4,
                "min_plus1_transmission": .2}, metadata={"meent": {"energy_totals": [1., 1.]}},
                solver_executions=2)

        def checkpoint(self):
            return {"identity": self.identity, "calls": self.calls}

        def restore(self, state):
            assert state["identity"] == self.identity
            self.calls = state["calls"]

    monkeypatch.setattr(Meent2DProblem, "evaluator", lambda self, problem: ControlledEvaluator(problem))
    recipe = plan()
    atomic_json(tmp_path / "spec.json", {"id": "2d_recipe_recovery", "campaign_id": "campaign_test",
        "study_id": "study_test", "seed": 17, "algorithm": "recipe", "algorithm_config": {},
        "problem": instance().model_dump(mode="json"), "recipe": recipe,
        "max_steps": len(recipe["cases"]), "schedule_steps": len(recipe["cases"]), "wall_seconds": 60})
    first = ExperimentWorker(tmp_path)
    first.one_step()
    first.save_checkpoint()
    recovered = ExperimentWorker(tmp_path)
    result = recovered.run()
    assert result["scientific_complete"] and result["recipe_result"]["verdict"] == "passed"
    assert result["evaluations"] == 4 and result["solver_calls"] == 8
    measured = read_journal(tmp_path / "observations.jsonl")
    assert len({row["evaluator_identity"] for row in measured}) == 3
    assert [row["proposal_id"] for row in measured] == [f"case_{index}" for index in range(4)]
