"""Installed MEENT evaluator for the FLRL two-dimensional beam deflector."""
from __future__ import annotations

import math
from copy import deepcopy
from typing import Any

from optimization_framework.contracts.base import content_hash
from optimization_framework.contracts.problems import (
    CandidateSchema, Evaluation, Objective, ProblemDefinition, ProblemInstance,
)


PROBLEM_ID = "meent_2d_dual_polarization_deflector"
DEFINITION_VERSION = "flrl-2d-v1"
EVALUATOR_VERSION = "meent-0.13.2-flrl-2d-v1"
ORDER_CONVERGENCE_RECIPE = "meent_2d_order_convergence:v1"
FLRL_OBJECTIVE = (
    "Design one periodic 2D silicon/air freeform metasurface that sends 1050 nm light (normal incidence inferred from the "
    "stated period relation) from silica (n=1.45) into transmitted diffraction order (+1, 0) at 75 degrees in x, for both "
    "TE and TM incidence. The patterned layer is 325 nm thick, sampled on a 256 x 128 binary grid; "
    "Px = 1050/sin(75 degrees) = 1087.0399894305872 nm and Py = 525 nm, with air above. The FLRL reference code's scalar "
    "score is the equal-weight mean of the absolute +1 transmitted efficiencies for TE and TM, each divided by its own "
    "incident power. Record both efficiencies and their minimum separately; the released FLRL code explicitly computes "
    "(TE + TM)/2. The task describes physical binary designs. Fourier level-set coefficients with a real-valued level-set "
    "function and geometry symmetric under y-to-minus-y reflection are a proposed search representation, not RCWA "
    "truncation. Compare the paper's FLRL/PPO approach at level-set mode limits (Nx, Ny) = (2,1), (4,2), (6,3), and (8,4) "
    "against matched-budget baselines using the installed 2D MEENT evaluator; verify RCWA convergence and cross-version "
    "parity before reproduction claims. The manuscript's 93.5% best result at (8,4) is prior literature, not a measurement "
    "in this campaign. The paper-code reference uses n(Si)=3.567390909 at 1050 nm by linear interpolation of its CSV n "
    "column (ignoring k), MEENT 0.9.5, RCWA fto=(10,5), complex128 Torch CPU, normal incidence from silica to air, and "
    "TE/TM pol=0/1. Validate its order indexing and convergence, independently check the evaluator, and measure "
    "per-evaluation cost before trials. The released 2D training config has 100,000 timesteps, one environment, and four "
    "stacked observations, whereas the manuscript describes 500,000 samples, four environments, and a three-step history; "
    "treat these as distinct reference settings until reconciled. Sources: the Octavian FLRL manuscript (main text and "
    "supplementary methods) and the released FLRL code at commit 7838e71313d71cee8e2db3b432f41f80b9106a95. This reported "
    "condition is development evidence and must not be labeled an untouched test condition. The installed campaign "
    "evaluator uses MEENT 0.13.2 and runs both polarizations; its current numerical checks cover uniform layers and an "
    "x-only stripe, not arbitrary 2D-pattern convergence.")

CONFIGURATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "wavelength_nm", "target_angle_deg", "period_x_nm", "period_y_nm",
        "thickness_nm", "grid_x", "grid_y", "incident_n", "exit_n",
        "incident_angle_deg", "target_order_x", "target_order_y",
        "silicon_index_source", "silicon_n_1050", "reference_meent_version",
    ],
    "properties": {
        "wavelength_nm": {"type": "number", "exclusiveMinimum": 0},
        "target_angle_deg": {"type": "number"},
        "period_x_nm": {"type": "number", "exclusiveMinimum": 0},
        "period_y_nm": {"type": "number", "exclusiveMinimum": 0},
        "thickness_nm": {"type": "number", "exclusiveMinimum": 0},
        "grid_x": {"type": "integer", "minimum": 2},
        "grid_y": {"type": "integer", "minimum": 2},
        "incident_n": {"type": "number", "exclusiveMinimum": 0},
        "exit_n": {"type": "number", "exclusiveMinimum": 0},
        "incident_angle_deg": {"type": "number"},
        "target_order_x": {"type": "integer"},
        "target_order_y": {"type": "integer"},
        "silicon_index_source": {"type": "string"},
        "silicon_n_1050": {"type": "number", "exclusiveMinimum": 0},
        "reference_meent_version": {"type": "string"},
    },
}
FIDELITY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["rcwa_order_x", "rcwa_order_y"],
    "properties": {
        "rcwa_order_x": {"type": "integer", "minimum": 1},
        "rcwa_order_y": {"type": "integer", "minimum": 1},
    },
}
OBJECTIVES = [
    Objective(name=name, direction="maximize", units="fraction of incident power")
    for name in (
        "mean_plus1_transmission", "te_plus1_transmission",
        "tm_plus1_transmission", "min_plus1_transmission",
    )
]
CONVERGENCE_OBJECTIVES = tuple(objective.name for objective in OBJECTIVES[:3])
ORDER_CONVERGENCE_SCHEMA = {
    "title": "2D RCWA order and repeat consistency",
    "assertion_kind": "solution_fidelity",
    "type": "object", "additionalProperties": False,
    "properties": {
        "fidelities": {
            "title": "Increasing 2D RCWA fidelities", "type": "array",
            "minItems": 2, "maxItems": 8, "items": FIDELITY_SCHEMA,
            "default": [{"rcwa_order_x": x, "rcwa_order_y": y}
                        for x, y in ((10, 5), (12, 6), (14, 7))],
        },
        "absolute_tolerance": {"type": "number", "exclusiveMinimum": 0,
                               "maximum": .005, "default": .005},
        "energy_tolerance": {"type": "number", "exclusiveMinimum": 0,
                             "maximum": .001, "default": .001},
        "repeat_tolerance": {"type": "number", "exclusiveMinimum": 0,
                             "maximum": 1e-6, "default": 1e-6},
        "repeats": {"title": "Evaluations at the highest fidelity", "type": "integer",
                    "minimum": 2, "maximum": 8, "default": 2},
    },
}


def _number(value: Any, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if positive and value <= 0:
        raise ValueError(f"{name} must be positive")
    return float(value)


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


class Meent2DProblem:
    def examples(self):
        """The Octavian FLRL paper's reported condition, as a New campaign starting point."""
        return [{
            "id": "flrl_2d_deflector_1050nm_75deg", "order": 20,
            "name": "2D dual-polarization beam deflector · Octavian FLRL",
            "summary": "Periodic 2D silicon/air freeform metasurface sending 1050 nm light from silica into the (+1, 0) "
                       "transmitted order at 75 degrees for both TE and TM; 256 x 128 binary grid, 325 nm thick.",
            "instances": [{
                "name": "1050 nm, 75\u00b0, TE+TM, 256\u00d7128 Si/air beam deflector",
                "problem_id": PROBLEM_ID,
                "configuration": {
                    "wavelength_nm": 1050.0, "target_angle_deg": 75.0, "period_x_nm": 1087.0399894305872,
                    "period_y_nm": 525.0, "thickness_nm": 325.0, "grid_x": 256, "grid_y": 128,
                    "incident_n": 1.45, "exit_n": 1.0, "incident_angle_deg": 0.0,
                    "target_order_x": 1, "target_order_y": 0,
                    "silicon_index_source": "FLRL Si_refractive_data.csv at commit 7838e71313d71cee8e2db3b432f41f80b9106a95; "
                                            "linear interpolation of n column, k ignored",
                    "silicon_n_1050": 3.567390909090909, "reference_meent_version": "0.9.5"},
                "fidelity": {"rcwa_order_x": 10, "rcwa_order_y": 5},
            }],
            "campaign": {
                "name": "2D dual-polarization beam deflector \u2014 Octavian FLRL paper",
                "objective": FLRL_OBJECTIVE, "compute_budget_seconds": 248400.0, "validation_reserve_seconds": 600.0,
                "delegated_trial_seconds": 3600.0, "implementation_compute_budget_seconds": 900.0,
                "llm_budget_usd": 5.0, "autonomy": "delegated"},
        }]

    def describe(self) -> ProblemDefinition:
        return ProblemDefinition(
            id=PROBLEM_ID, version=DEFINITION_VERSION,
            name="FLRL 2D silicon/air dual-polarization beam deflector",
            evaluator_id="meent_rcwa_2d", evaluator_version=EVALUATOR_VERSION,
            configuration_schema=CONFIGURATION_SCHEMA, fidelity_schema=FIDELITY_SCHEMA,
            capabilities=["binary", "binary_forward", "scalar_objective"],
            resources={"cpu_threads": 1},
            validation_recipes=[ORDER_CONVERGENCE_RECIPE],
            recipe_schemas={ORDER_CONVERGENCE_RECIPE: deepcopy(ORDER_CONVERGENCE_SCHEMA)},
        )

    def resolve(self, configuration: dict, fidelity: dict | None = None) -> ProblemInstance:
        config = dict(configuration)
        settings = dict(fidelity or {})
        # TaskInput also supplies its legacy binary ``physics`` projection,
        # which includes the fidelity values alongside the configuration.
        embedded = {key: config.pop(key) for key in FIDELITY_SCHEMA["required"] if key in config}
        if any(key in settings and settings[key] != value for key, value in embedded.items()):
            raise ValueError("Embedded RCWA fidelity differs from the explicit fidelity")
        settings = {**embedded, **settings}
        if set(config) != set(CONFIGURATION_SCHEMA["required"]):
            raise ValueError("2D MEENT configuration must contain exactly the declared fields")
        if set(settings) != set(FIDELITY_SCHEMA["required"]):
            raise ValueError("2D MEENT fidelity requires both RCWA orders")
        for name in ("wavelength_nm", "period_x_nm", "period_y_nm", "thickness_nm",
                     "incident_n", "exit_n", "silicon_n_1050"):
            config[name] = _number(config[name], name, positive=True)
        for name in ("target_angle_deg", "incident_angle_deg"):
            config[name] = _number(config[name], name)
        for name in ("grid_x", "grid_y"):
            config[name] = _integer(config[name], name, minimum=2)
        for name in ("target_order_x", "target_order_y"):
            config[name] = _integer(config[name], name)
        for name in ("silicon_index_source", "reference_meent_version"):
            if not isinstance(config[name], str) or not config[name].strip():
                raise ValueError(f"{name} must be a nonempty string")
        for name in ("rcwa_order_x", "rcwa_order_y"):
            settings[name] = _integer(settings[name], name, minimum=1)
        if settings["rcwa_order_x"] < config["target_order_x"] or settings["rcwa_order_y"] < config["target_order_y"]:
            raise ValueError("The target diffraction order lies outside the RCWA truncation")
        if config["grid_x"] * config["grid_y"] > 1_000_000:
            raise ValueError("Candidate exceeds the supported binary design dimension")
        definition = self.describe()
        return ProblemInstance(
            definition_id=definition.id, definition_version=definition.version,
            evaluator_id=definition.evaluator_id, evaluator_version=definition.evaluator_version,
            configuration=config,
            candidate_schema=CandidateSchema(representation="binary", dimensions=config["grid_x"] * config["grid_y"]),
            primary_objective=OBJECTIVES[0], extra_metrics=OBJECTIVES[1:],
            public_descriptor={
                "wavelength_nm": config["wavelength_nm"],
                "target_angle_deg": config["target_angle_deg"],
                "grid_x": config["grid_x"], "grid_y": config["grid_y"],
            },
            capabilities=definition.capabilities, fidelity=settings,
            scientific_identity=content_hash({"definition": [definition.id, definition.version], "configuration": config}),
        )

    def evaluator(self, instance: ProblemInstance) -> "Meent2DEvaluator":
        return Meent2DEvaluator(instance)

    def implementation_fixture(self, configuration: dict, dimensions: int) -> ProblemInstance:
        if configuration["grid_x"] * configuration["grid_y"] != dimensions:
            raise ValueError("Optimizer dimension differs from the 2D design grid")
        # A commission can pin a physical fidelity in its legacy configuration.
        # Preserve that choice; use the inexpensive smoke fidelity only when no
        # explicit RCWA orders were supplied.
        embedded = {key: configuration[key] for key in FIDELITY_SCHEMA["required"] if key in configuration}
        return self.resolve(configuration, embedded or {"rcwa_order_x": 1, "rcwa_order_y": 1})

    def recipe_parameters(self, instance, recipe_id, parameters):
        if recipe_id != ORDER_CONVERGENCE_RECIPE:
            raise ValueError(f"Unknown 2D MEENT recipe: {recipe_id}")
        if not isinstance(parameters, dict) or set(parameters) - set(ORDER_CONVERGENCE_SCHEMA["properties"]):
            raise ValueError("2D convergence requires explicit fidelity pairs; 1D orders are unsupported")
        values = {name: deepcopy(schema["default"])
                  for name, schema in ORDER_CONVERGENCE_SCHEMA["properties"].items()}
        values.update(deepcopy(parameters))
        fidelities = values["fidelities"]
        if not isinstance(fidelities, list) or not 2 <= len(fidelities) <= 8:
            raise ValueError("2D convergence requires two through eight distinct fidelity pairs")
        resolved = []
        for fidelity in fidelities:
            if not isinstance(fidelity, dict):
                raise ValueError("Each 2D fidelity requires both rcwa_order_x and rcwa_order_y")
            current = self.resolve(instance.configuration, fidelity).fidelity
            if resolved and (current == resolved[-1] or any(
                    current[key] < resolved[-1][key] for key in FIDELITY_SCHEMA["required"])):
                raise ValueError("2D fidelity pairs must increase without lowering either RCWA order")
            resolved.append(current)
        values["fidelities"] = resolved
        for name in ("absolute_tolerance", "energy_tolerance", "repeat_tolerance"):
            values[name] = _number(values[name], name, positive=True)
            if values[name] > ORDER_CONVERGENCE_SCHEMA["properties"][name]["maximum"]:
                raise ValueError(f"{name} cannot weaken the reviewed 2D validation threshold")
        values["repeats"] = _integer(values["repeats"], "repeats", minimum=2)
        if values["repeats"] > 8:
            raise ValueError("At most eight highest-fidelity repetitions are supported")
        return values

    def plan_recipe(self, instance, recipe_id, parameters, subjects):
        values = self.recipe_parameters(instance, recipe_id, parameters)
        if not subjects:
            raise ValueError("Select actual binary 2D masks for order convergence")
        candidates = [instance.candidate_schema.canonicalize(candidate) for candidate in subjects]
        instances = [self.resolve(instance.configuration, fidelity) for fidelity in values["fidelities"]]
        cases = []
        for subject_index, candidate in enumerate(candidates):
            for fidelity_index, problem in enumerate(instances):
                repeats = values["repeats"] if fidelity_index == len(instances) - 1 else 1
                for repeat in range(repeats):
                    cases.append({"problem": problem.model_dump(mode="json"), "candidate": candidate,
                                  "subject_index": subject_index, "fidelity_index": fidelity_index,
                                  "repeat": repeat})
        return {"subjects": candidates, "cases": cases,
                "validation_rule": {"kind": "solution_fidelity", "subject": "solution",
                    "evidence_requirements": ["Every declared 2D fidelity and repeat completed",
                        "Mean, TE and TM agree at the final two distinct fidelities",
                        "Both polarizations conserve energy", "Highest-fidelity repeats agree"]}}

    def summarize_recipe(self, recipe, observations):
        if recipe.get("recipe_id") != ORDER_CONVERGENCE_RECIPE:
            raise ValueError("Unknown 2D MEENT recipe summary")
        parameters = self.recipe_parameters(ProblemInstance(**recipe["cases"][0]["problem"]),
                                            recipe["recipe_id"], recipe["parameters"])
        findings = []
        for subject_index, candidate in enumerate(recipe["subjects"]):
            expected = [(index, case) for index, case in enumerate(recipe["cases"])
                        if case["subject_index"] == subject_index]
            rows, missing, invalid = [], [], []
            for index, case in expected:
                observation = observations[index] if index < len(observations) else None
                if not isinstance(observation, dict):
                    missing.append(f"case_{index}")
                    continue
                problem = ProblemInstance(**case["problem"])
                if (observation.get("status") != "ok" or observation.get("proposal_id") != f"case_{index}"
                        or observation.get("fidelity") != problem.fidelity
                        or observation.get("evaluator_identity") != problem.evaluation_identity
                        or observation.get("candidate") != candidate):
                    missing.append(f"case_{index}: matching successful observation required")
                    continue
                objectives = observation.get("objectives", {})
                energy = observation.get("metadata", {}).get("meent", {}).get("energy_totals")
                if (not isinstance(objectives, dict) or any(name not in objectives for name in CONVERGENCE_OBJECTIVES)
                        or not isinstance(energy, list) or len(energy) != 2):
                    missing.append(f"case_{index}: mean, TE, TM and both energy totals required")
                    continue
                try:
                    scores = {name: _number(objectives[name], name) for name in CONVERGENCE_OBJECTIVES}
                    totals = [_number(total, "polarization energy total") for total in energy]
                except ValueError:
                    invalid.append(f"case_{index}: nonfinite physical measurement")
                    continue
                if (any(score < -1e-6 or score > 1 + 1e-6 for score in scores.values())
                        or abs(scores[CONVERGENCE_OBJECTIVES[0]] -
                               (scores[CONVERGENCE_OBJECTIVES[1]] + scores[CONVERGENCE_OBJECTIVES[2]]) / 2) > 1e-9):
                    invalid.append(f"case_{index}: invalid polarization objective")
                rows.append({"fidelity": problem.fidelity, "fidelity_index": case["fidelity_index"],
                             "repeat": case["repeat"], "observation_id": observation.get("id"),
                             "objectives": scores, "energy_totals": totals,
                             "costs": deepcopy(observation.get("costs", {}))})
            complete = len(rows) == len(expected) and not missing
            groups = [[row for row in rows if row["fidelity_index"] == index]
                      for index in range(len(parameters["fidelities"]))]
            differences = {name: abs(groups[-1][0]["objectives"][name] - groups[-2][0]["objectives"][name])
                           for name in CONVERGENCE_OBJECTIVES} if groups[-1] and groups[-2] else None
            repeat_differences = {name: max(row["objectives"][name] for row in groups[-1]) -
                                 min(row["objectives"][name] for row in groups[-1])
                                 for name in CONVERGENCE_OBJECTIVES} if len(groups[-1]) >= 2 else None
            energy_error = max((abs(total - 1) for row in rows for total in row["energy_totals"]), default=None)
            converged = bool(complete and not invalid and differences is not None and repeat_differences is not None
                             and max(differences.values()) <= parameters["absolute_tolerance"]
                             and max(repeat_differences.values()) <= parameters["repeat_tolerance"]
                             and energy_error <= parameters["energy_tolerance"])
            verdict = "passed" if converged else "failed" if complete or invalid else "inconclusive"
            findings.append({"design_index": subject_index, "design": candidate, "complete": complete,
                "converged": converged, "verdict": verdict,
                "numerical_status": "converged" if converged else "unconverged" if complete else "incomplete",
                "last_two_absolute_differences": differences,
                "repeat_max_absolute_differences": repeat_differences, "max_energy_error": energy_error,
                "highest_fidelity": parameters["fidelities"][-1], "parameters": parameters,
                "observations": rows, "missing_evidence": missing, "invalid_evidence": invalid,
                "rationale": "Final two 2D fidelities, both polarization energies and repeated solves satisfy the frozen thresholds."
                    if converged else "2D fidelity, energy and repeat consistency have not all been established."})
        complete = len(observations) == len(recipe["cases"]) and all(item["complete"] for item in findings)
        verdict = ("passed" if complete and all(item["verdict"] == "passed" for item in findings)
                   else "failed" if any(item["verdict"] == "failed" for item in findings) else "inconclusive")
        return {"kind": "solution_fidelity", "recipe_id": recipe["recipe_id"], "complete": complete,
                "verdict": verdict, "subjects": findings,
                "note": "Consistency over the tested 2D truncations is separate from independent evaluator correctness and scientific confirmation."}


class Meent2DEvaluator:
    def __init__(self, instance: ProblemInstance):
        import meent
        import torch

        if meent.__version__ != "0.13.2":
            raise ValueError("This evaluator requires MEENT 0.13.2")
        self.instance = instance
        self.torch = torch
        self.cache = {}
        self.mee = meent.call_mee(
            backend=2, pol=0,
            n_top=instance.configuration["incident_n"],
            n_bot=instance.configuration["exit_n"],
            theta=torch.tensor(math.radians(instance.configuration["incident_angle_deg"]), dtype=torch.float64),
            phi=torch.tensor(0.0, dtype=torch.float64),
            fto=[instance.fidelity["rcwa_order_x"], instance.fidelity["rcwa_order_y"]],
            wavelength=instance.configuration["wavelength_nm"],
            period=torch.tensor([instance.configuration["period_x_nm"], instance.configuration["period_y_nm"]], dtype=torch.float64),
            thickness=torch.tensor([instance.configuration["thickness_nm"]], dtype=torch.float64),
            type_complex=torch.complex128, device=0,
        )

    def evaluate(self, candidate: list[int | float]) -> Evaluation:
        values = self.instance.candidate_schema.canonicalize(candidate)
        config = self.instance.configuration
        cell = self.torch.tensor(values, dtype=self.torch.float64).reshape(1, config["grid_y"], config["grid_x"])
        self.mee.ucell = 1.0 + cell * (config["silicon_n_1050"] - 1.0)
        y_order = config["target_order_y"]
        x_order = config["target_order_x"]
        efficiencies = []
        energy_totals = []
        with self.torch.no_grad():
            for pol in (0, 1):
                self.mee.pol = pol
                result = self.mee.conv_solve().res
                transmitted = result.de_ti
                reflected = result.de_ri
                if transmitted.ndim != 2 or transmitted.shape != (
                    2 * self.instance.fidelity["rcwa_order_y"] + 1,
                    2 * self.instance.fidelity["rcwa_order_x"] + 1,
                ):
                    raise ValueError("MEENT returned an unexpected 2D diffraction-order grid")
                efficiency = float(transmitted[transmitted.shape[0] // 2 + y_order,
                                                transmitted.shape[1] // 2 + x_order])
                energy = float(transmitted.sum() + reflected.sum())
                if not math.isfinite(efficiency) or not math.isfinite(energy):
                    raise ValueError("MEENT returned a nonfinite diffraction efficiency")
                if efficiency < -1e-6 or efficiency > 1 + 1e-6 or abs(energy - 1) > 1e-3:
                    raise ValueError("MEENT returned an invalid diffraction or energy budget")
                efficiencies.append(efficiency)
                energy_totals.append(energy)
        te, tm = efficiencies
        return Evaluation(
            objectives={
                "mean_plus1_transmission": (te + tm) / 2,
                "te_plus1_transmission": te,
                "tm_plus1_transmission": tm,
                "min_plus1_transmission": min(te, tm),
            },
            metadata={"meent": {"version": "0.13.2", "energy_totals": energy_totals}},
            solver_executions=2, cache_hit=False,
        )

    def checkpoint(self) -> dict:
        from copy import deepcopy
        return {"evaluation_identity": self.instance.evaluation_identity, "binary_cache": deepcopy(self.cache)}

    def restore(self, state: dict) -> None:
        if state.get("evaluation_identity") != self.instance.evaluation_identity:
            raise ValueError("MEENT 2D checkpoint belongs to another problem or fidelity")
        from copy import deepcopy
        self.cache = deepcopy(state.get("binary_cache", {}))

    def relaxed_gradient(self, request: dict) -> dict:
        """Auxiliary physical solve: never supplies a binary campaign score."""
        from dqn_meent.fourier import FourierGeometry
        import numpy as np

        if set(request) != {"coefficients", "modes_x", "modes_y", "beta", "material_map"}:
            raise ValueError("Invalid Fourier gradient request")
        config = self.instance.configuration
        nx = _integer(request['modes_x'], 'modes_x', minimum=1)
        ny = _integer(request['modes_y'], 'modes_y')
        geometry = FourierGeometry(nx, ny, config['grid_x'], config['grid_y'])
        geometry.field(request['coefficients'])
        beta = _number(request['beta'], 'beta', positive=True)
        if beta > 100 or request['material_map'] not in {'index', 'permittivity'}:
            raise ValueError('Unsupported relaxation strength or material map')
        torch = self.torch
        with torch.enable_grad():
            coefficients = torch.tensor(request['coefficients'], dtype=torch.float64, requires_grad=True)
            field = torch.tensor(np.array(geometry.basis), dtype=torch.float64) @ coefficients
            density = torch.sigmoid(beta * field).reshape(1, config['grid_y'], config['grid_x'])
            self.mee.ucell = (1 + density * (config['silicon_n_1050'] ** 2 - 1)).sqrt() if request['material_map'] == 'permittivity' else 1 + density * (config['silicon_n_1050'] - 1)
            scores = []
            for pol in (0, 1):
                self.mee.pol = pol
                result = self.mee.conv_solve().res
                scores.append(result.de_ti[self.instance.fidelity['rcwa_order_y'] + config['target_order_y'],
                                          self.instance.fidelity['rcwa_order_x'] + config['target_order_x']])
            mean = (scores[0] + scores[1]) / 2
            gradient, = torch.autograd.grad(mean, coefficients)
            if not torch.isfinite(gradient).all() or not torch.isfinite(mean):
                raise ValueError('MEENT returned nonfinite relaxed gradients')
            output = {'gradient': gradient.detach().tolist(), 'mean': float(mean.detach()),
                'te': float(scores[0].detach()), 'tm': float(scores[1].detach()), 'beta': beta,
                'material_map': request['material_map'], 'forward_solver_executions': 2, 'backward_calls': 1}
        self.mee.ucell = self.mee.ucell.detach()
        return output

    def evaluate_proposal(self, candidate, metadata):
        """Account for hard scoring and an optional gradient at the same geometry.

        Only binary objectives enter the archive. Relaxed scores/gradients are
        auxiliary observation metadata; each forward polarization is charged.
        """
        from dqn_meent.fourier import FourierGeometry

        values = self.instance.candidate_schema.canonicalize(candidate)
        request = metadata.get('flrl_gradient')
        if request is not None:
            config = self.instance.configuration
            geometry = FourierGeometry(request['modes_x'], request['modes_y'], config['grid_x'], config['grid_y'])
            if geometry.mask(request['coefficients']) != values:
                raise ValueError('Gradient request coefficients do not produce the proposed binary mask')
        key = content_hash([self.instance.evaluation_identity, values])
        if key in self.cache:
            result = Evaluation(**self.cache[key]).model_copy(deep=True, update={"solver_executions": 0, "cache_hit": True})
        else:
            result = self.evaluate(values)
            self.cache[key] = result.model_dump(mode='json')
            if len(self.cache) > 256:
                self.cache.pop(next(iter(self.cache)))
        if request is not None:
            result = result.model_copy(update={"metadata": {**result.metadata, 'flrl_gradient': self.relaxed_gradient(request)},
                                               "solver_executions": result.solver_executions + 2})
        return result
