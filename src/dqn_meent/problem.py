"""The optical application boundary; numerical implementation stays unchanged."""
from __future__ import annotations

from dataclasses import asdict

from optimization_framework.contracts.problems import CandidateSchema, Evaluation, Objective, ProblemDefinition, ProblemInstance

from .config import PhysicsConfig


def physical_identity(physics):
    config = asdict(PhysicsConfig(**physics))
    for name in ("fourier_order", "cache_size", "energy_tolerance"):
        config.pop(name)
    if config["material"] == "meent_green":
        config.pop("silicon_n")
        config.pop("silicon_k")
    for name, value in config.items():
        if isinstance(value, (int, float)) and name != "n_cells":
            config[name] = float(value)
    # Preserve the historical physical condition identity byte for byte.
    import hashlib
    import json
    return hashlib.sha256(json.dumps({"physics": config, "objective": "absolute_transmitted_order_+1"},
                                    sort_keys=True, allow_nan=False).encode()).hexdigest()


class MeentProblem:
    def reference_sets(self):
        from .study_templates import reference_sets
        return reference_sets()

    def study_templates(self):
        from .study_templates import registered
        return registered()

    def study_rules(self):
        from .study_rules import registered
        return registered()

    def seed_hypotheses(self):
        from .research_seeds import seed_hypotheses
        return seed_hypotheses()

    def legacy_probe(self, tasks, hypotheses, trials):
        from .research_seeds import select_probe
        return select_probe(tasks, hypotheses, trials)

    def exposure_fields(self, store, campaign_id, instance):
        from optimization_framework.evaluation.legacy_confirmation import task_exposure_fields
        return task_exposure_fields(store, campaign_id, {**instance.configuration, **instance.fidelity})

    def check_unseen_instance(self, store, campaign_id, instance):
        if any(item["condition_key"] == instance.scientific_identity for item in store.list("implementation_exposure", campaign_id)):
            raise ValueError("Implementation development or correctness validation already exposed this MEENT condition")
        if any(item.get("physical_condition_key") == instance.scientific_identity and item.get("exposed")
               for item in store.list("confirmation_condition", campaign_id)):
            raise ValueError("This MEENT condition was exposed by an earlier confirmation cohort")

    def examples(self):
        """The original 1D deflector, as a New campaign starting point."""
        return [{
            "id": "meent_grating_1100nm_50deg", "order": 10,
            "name": "1D binary grating \u00b7 1100 nm, 50\u00b0 deflector",
            "summary": "64-cell silicon/air binary grating on silica deflecting 1100 nm light into the +1 transmitted "
                       "order at 50 degrees; 325 nm thick, RCWA with MEENT.",
            "instances": [{
                "name": "1100 nm \u00b7 50\u00b0 deflector", "problem_id": "meent_grating",
                "configuration": {"n_cells": 64, "wavelength_nm": 1100, "deflection_angle_deg": 50, "thickness_nm": 325,
                    "n_incident": 1.45, "n_exit": 1, "material": "constant", "silicon_n": 3.551726470588235,
                    "silicon_k": 0, "fourier_order": 15},
            }],
            "campaign": {"name": "Optimizer research",
                "objective": "Develop an effective optimizer for the selected problem under the declared resource budget."},
        }]

    def describe(self):
        return ProblemDefinition(id="meent_grating", version="1", name="MEENT binary grating",
            evaluator_id="meent_rcwa", evaluator_version="0.13.2-v1",
            configuration_schema={"type": "object", "properties": {
                "n_cells": {"type": "integer", "minimum": 2, "maximum": 1024, "default": 64},
                "wavelength_nm": {"type": "number", "default": 1100},
                "deflection_angle_deg": {"type": "number", "default": 50}}},
            capabilities=["binary", "binary_forward", "scalar_objective"],
            fidelity_schema={"type": "object", "properties": {"fourier_order": {"type": "integer", "minimum": 1, "maximum": 480}}},
            validation_recipes=["fourier_convergence:v1", "reevaluate:v1", "fields:v1", "sensitivity:v1"], renderer="meent",
            recipe_schemas={
                "reevaluate:v1": {"title": "Fixed-fidelity reevaluation", "type": "object", "properties": {
                    "orders": {"title": "Fourier orders", "type": "array", "items": {"type": "integer", "minimum": 1, "maximum": 480}, "default": [160]}}},
                "fourier_convergence:v1": {"title": "Fourier convergence", "assertion_kind": "solution_fidelity", "type": "object", "properties": {
                    "orders": {"title": "Fourier orders", "type": "array", "items": {"type": "integer", "minimum": 1, "maximum": 480}, "default": [25, 40, 60, 80]},
                    "tolerance": {"title": "Absolute tolerance", "type": "number", "default": .005, "exclusiveMinimum": 0, "maximum": .1}}},
                "sensitivity:v1": {"title": "Physical sensitivity", "type": "object", "properties": {
                    "parameter": {"title": "Physical parameter", "type": "string", "enum": ["wavelength_nm", "deflection_angle_deg", "thickness_nm"], "default": "wavelength_nm"},
                    "values": {"title": "Parameter values", "type": "array", "items": {"type": "number"}, "default": [1000, 1100, 1200]}}},
                "fields:v1": {"title": "Electromagnetic fields", "type": "object", "properties": {
                    "fourier_order": {"title": "Fourier order", "type": "integer", "minimum": 1, "maximum": 480, "default_from_fidelity": "fourier_order"},
                    "nx": {"title": "Horizontal samples", "type": "integer", "minimum": 2, "maximum": 2048, "default": 256},
                    "nz_pattern": {"title": "Pattern layer samples", "type": "integer", "minimum": 2, "maximum": 512, "default": 80}}}})

    def resolve(self, configuration, fidelity=None):
        values = dict(configuration)
        settings = dict(fidelity or {})
        if set(settings) - {"fourier_order"}:
            raise ValueError("MEENT fidelity supports only fourier_order")
        if "fourier_order" in settings:
            values["fourier_order"] = settings["fourier_order"]
        config = PhysicsConfig(**values)
        for name in ("n_cells", "fourier_order", "cache_size"):
            if isinstance(getattr(config, name), bool) or not isinstance(getattr(config, name), int):
                raise ValueError(f"{name} must be an integer")
        if config.n_cells > 1024 or config.fourier_order > 480:
            raise ValueError("Local MEENT adapter supports at most 1024 cells and Fourier order 480")
        definition = self.describe()
        resolved = asdict(config)
        order = resolved.pop("fourier_order")
        return ProblemInstance(definition_id=definition.id, definition_version=definition.version,
            evaluator_id=definition.evaluator_id, evaluator_version=definition.evaluator_version,
            configuration=resolved, candidate_schema=CandidateSchema(representation="binary", dimensions=config.n_cells),
            primary_objective=Objective(name="efficiency", direction="maximize", units="fraction of incident power"),
            extra_metrics=[Objective(name=key, direction="minimize") for key in ("reflectance", "absorption")],
            public_descriptor={"n_cells": config.n_cells, "wavelength_nm": config.wavelength_nm,
                               "deflection_angle_deg": config.deflection_angle_deg, "objective": "absolute_transmitted_order_+1",
                               "objective_bounds": {"efficiency": [0., 1.]}},
            capabilities=definition.capabilities, fidelity={"fourier_order": order}, scientific_identity=physical_identity(asdict(config)))

    def evaluator(self, instance):
        return MeentEvaluator(instance)

    def implementation_fixture(self, configuration, dimensions):
        return self.resolve({**configuration, "n_cells": dimensions}, {"fourier_order": 2})

    def plan_recipe(self, instance, recipe_id, parameters, subjects):
        from .recipes import plan
        return plan(self, instance, recipe_id, parameters, subjects)

    def summarize_recipe(self, recipe, observations):
        from .recipes import summarize
        return summarize(recipe, observations)

    def recipe_evaluator(self, case):
        from .recipes import FieldEvaluator
        if case.get("operation") != "fields:v1":
            raise ValueError("Unsupported evaluator operation")
        return FieldEvaluator(case)


class MeentEvaluator:
    def __init__(self, instance, solver_factory=None):
        if solver_factory is None:
            from .physics import ForwardSolver
            solver_factory = ForwardSolver
        self.instance = instance
        self.solver = solver_factory(PhysicsConfig(**instance.configuration, **instance.fidelity))

    def evaluate(self, candidate):
        values = self.instance.candidate_schema.canonicalize(candidate)
        before = self.solver.solver_calls
        result = self.solver.evaluate(values)
        solves = self.solver.solver_calls - before
        # The numerical evaluator already enforces energy conservation and finite results.
        return Evaluation(objectives={"efficiency": result.efficiency, "reflectance": result.reflectance,
                                      "transmittance": result.transmittance, "absorption": result.absorption},
                          metadata={"meent": result.to_dict()}, solver_executions=solves, cache_hit=not solves)

    def checkpoint(self):
        return {"identity": self.instance.evaluation_identity, "solver": self.solver.state_dict()}

    def restore(self, state):
        if state["identity"] != self.instance.evaluation_identity:
            raise ValueError("MEENT checkpoint belongs to another instance or fidelity")
        self.solver.load_state_dict(state["solver"])
