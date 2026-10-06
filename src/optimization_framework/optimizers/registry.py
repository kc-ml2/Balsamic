"""Reviewed bundled implementations; generated code cannot select this lane."""
from optimization_framework.contracts.problems import CandidateSchema, ProblemInstance
from optimization_framework.contracts.capabilities import OptimizerCapabilities
from .lifecycle import AskTellAdapter, BoundedSearch
from .config import TrainConfig
from pydantic import TypeAdapter
from .fourier_specs import METHODS as FOURIER_METHODS, PROPERTIES as FOURIER_PROPERTIES

MASK_LIBRARY_METHODS = {"motif_surgery", "nested_fourier", "phenotype_de"}


METHODS = [
    {"id": "random", "name": "Uniform random", "description": "Independent feasible samples; reference baseline.", "representations": ["binary", "discrete", "continuous"], "constraints": True, "parameters": {}},
    {"id": "coordinate", "name": "Coordinate search", "description": "Bounded coordinate improvement with global restarts.", "representations": ["continuous"], "constraints": True, "parameters": {"radius": .2}},
    {"id": "evaluate_asset", "name": "Evaluate a reference solution", "description": "One evaluation of an explicitly declared solution asset; no search move.",
     "representations": ["binary", "discrete", "continuous"], "constraints": True, "parameters": {}, "purpose": "reference_evaluation"},
    {"id": "artifact_inference", "name": "Declared artifact inference", "description": "Evaluate a versioned artifact with its registered inference adapter.",
     "representations": ["binary", "discrete", "continuous"], "constraints": True, "parameters": {}, "purpose": "artifact_inference"},
    *[{"id": name, "name": title, "description": description, "representations": ["binary"], "constraints": False, "parameters": {}} for name, title, description in (
        ("hillclimb", "Restart hill climbing", "Single-coordinate improvement with restarts."),
        ("refinement", "One/two-cell refinement", "Best improvement around a declared starting design with upstream provenance."),
        ("dqn", "Double DQN", "Learn discrete actions with experience replay."),
        ("frozen_policy", "Frozen policy evaluation", "Evaluate a declared learned policy with no weight updates."),
        ("annealing", "Simulated annealing", "Temperature-controlled acceptance of downhill moves."),
        ("block_tabu", "Adaptive block tabu", "Coordinated moves with recent-candidate memory."),
        ("population", "Population search", "Recombination and mutation of a binary population."),
        ("surrogate", "Surrogate-guided search", "Fit observations to select prospective candidates."))],
    *FOURIER_METHODS,
    *[{"id": name, "name": title, "description": "Standalone mask-optimizers method on the 2D MEENT grid.",
       "problem_ids": ["meent_2d_dual_polarization_deflector"], "representations": ["binary"],
       "constraints": False, "parameters": {}} for name, title in (
        ("motif_surgery", "Mirror-paired motif surgery"),
        ("nested_fourier", "Nested Fourier-band continuation"),
        ("phenotype_de", "Phenotype-archive differential evolution"))],
]


INTEGER = {"type": "integer", "minimum": 1}
POSITIVE = {"type": "number", "exclusiveMinimum": 0}
PROPERTIES = {
    **FOURIER_PROPERTIES,
    "artifact_inference": {"adapter_id": {"type": "string"}, "parameters": {"type": "object"}},
    "random": {}, "evaluate_asset": {}, "coordinate": {"radius": {**POSITIVE, "maximum": 1, "default": .2}},
    "hillclimb": {"restart_patience": INTEGER, "neighborhood": {"type": "string", "enum": ["permuted", "random"]},
        "accept_equal": {"type": "boolean"}, "initialization": {"type": "string", "enum": ["random", "ones"]}},
    "refinement": {"improvement_tolerance": {"type": "number", "minimum": 0, "exclusiveMaximum": 1}},
    "frozen_policy": {"epsilon": {"type": "number", "minimum": 0, "maximum": 1}, "horizon": INTEGER,
        "tie_break": {"type": "string", "enum": ["first", "random"]}},
    "annealing": {"temperature": POSITIVE, "min_temperature": POSITIVE, "max_block_size": INTEGER},
    "block_tabu": {"max_block_size": INTEGER, "restart_patience": INTEGER, "tabu_tenure": INTEGER},
    "population": {"population_size": {"type": "integer", "minimum": 2}, "mutation_rate": {**POSITIVE, "maximum": 1}},
    "surrogate": {"warmup": INTEGER, "candidate_pool": INTEGER, "ridge": POSITIVE, "exploration": POSITIVE, "max_samples": INTEGER},
    "motif_surgery": {"radius": INTEGER, "initial_count": INTEGER},
    "nested_fourier": {"stage_patience": INTEGER},
    "phenotype_de": {"population_size": {"type": "integer", "minimum": 4}, "bins": {"type": "integer", "minimum": 2},
        "differential_weight": {**POSITIVE, "maximum": 2},
        "crossover_rate": {**POSITIVE, "maximum": 1},
        "modes_x": INTEGER, "modes_y": {"type": "integer", "minimum": 0}},
}
for method in METHODS:
    name = method["id"]
    properties = dict(TypeAdapter(TrainConfig).json_schema()["properties"]) if name == "dqn" else dict(PROPERTIES[name])
    for key in ("seed", "total_steps"):
        properties.pop(key, None)  # The worker owns these frozen experiment fields.
    if name not in FOURIER_PROPERTIES and name not in MASK_LIBRARY_METHODS | {"coordinate", "frozen_policy", "evaluate_asset", "artifact_inference"}:
        properties["initial_design"] = {"type": "array", "description": "A candidate in this instance's declared schema"}
    method.update(contract="optimizer_v1", contract_version=1, batch_size=1, supports_failure_observations=False,
        parameter_schema={"type": "object", "properties": properties, "additionalProperties": False})
    method["execution_capabilities"] = OptimizerCapabilities(completion_units=["evaluation_requests", "optimizer_decisions"]
        if name in {"dqn", "frozen_policy", "coordinate", "flrl_autograd_adam"} else ["evaluation_requests"]).model_dump(mode="json")
    if name == "dqn":
        from .policy import POLICY_FORMAT
        method["execution_capabilities"] = OptimizerCapabilities(completion_units=["evaluation_requests", "optimizer_decisions"],
            exports=[POLICY_FORMAT]).model_dump(mode="json")


def capabilities(name, parameters=None, implementation=None):
    if name == "package":
        return OptimizerCapabilities(**((implementation or {}).get("execution_capabilities") or {}))
    if name == "artifact_inference":
        from optimization_framework.evaluation.inference import adapters
        return adapters.get((parameters or {}).get("adapter_id"))[1].capabilities
    method = next((item for item in METHODS if item["id"] == name), None)
    return OptimizerCapabilities(**(method["execution_capabilities"] if method else {}))


def validate_parameters(name, instance, parameters, training=None, inference_registry=None):
    """Reject unsupported or malformed procedures before allocation and launch."""
    from optimization_framework.implementations.models import check_parameters
    method = next(item for item in METHODS if item["id"] == name)
    if set(parameters) - set(method["parameter_schema"]["properties"]):
        raise ValueError("Unsupported optimizer parameters: " + ", ".join(sorted(set(parameters) - set(method["parameter_schema"]["properties"]))))
    if "initial_design" in parameters:
        if instance.candidate_schema.representation != "binary" or instance.candidate_schema.constraints:
            raise ValueError("This bundled implementation does not accept inline initial candidates for this domain; declare a supported input asset")
        instance.candidate_schema.canonicalize(parameters["initial_design"])
    if name == "dqn":
        TrainConfig(**{**(training or {}), **{key: value for key, value in parameters.items() if key != "initial_design"}})
    else:
        check_parameters(parameters, method["parameter_schema"])
    if name in FOURIER_PROPERTIES:
        from .fourier_specs import validate
        validate(name, instance, parameters)
    if name in MASK_LIBRARY_METHODS and (parameters.get("modes_x", 8) > 16 or parameters.get("modes_y", 4) > 8):
        raise ValueError("Mask-library Fourier modes exceed the supported basis")
    if name == "artifact_inference":
        from optimization_framework.evaluation.inference import prepare
        prepare(parameters.get("adapter_id"), instance, parameters.get("parameters", {}), registry=inference_registry)
    if name == "annealing" and parameters.get("min_temperature", .0001) > parameters.get("temperature", .05):
        raise ValueError("min_temperature must not exceed temperature")
    if name == "dqn" and parameters.get("device", (training or {}).get("device", "cpu")) != "cpu":
        raise ValueError("GPU scheduling is not supported by this workspace; use the CPU implementation")


def capability_reason(name, instance):
    if isinstance(instance, dict):
        instance = ProblemInstance(**instance)
    method = next((m for m in METHODS if m["id"] == name), None)
    if method is None:
        return "Missing implementation; commission or reuse a validated version"
    if name in MASK_LIBRARY_METHODS:
        from dqn_meent.mask_library_adapter import library_available, LIBRARY_VERSION
        if not library_available():
            return f"Install mask-optimizers {LIBRARY_VERSION} before using this campaign method"
    if method.get("problem_ids") and instance.definition_id not in method["problem_ids"]:
        return f"{method['name']} requires its declared 2D MEENT problem"
    if instance.candidate_schema.representation not in method["representations"]:
        return f"{method['name']} requires {' or '.join(method['representations'])} candidates"
    if instance.candidate_schema.constraints and not method["constraints"]:
        return f"{method['name']} does not implement this problem's explicit feasibility constraints"
    return None


def bind_inputs(name, instance, parameters, assets):
    """Resolve asset-dependent procedure parameters before allocating work."""
    parameters = dict(parameters)
    if name == "evaluate_asset":
        if len(assets) != 1 or assets[0]["kind"] != "solution":
            raise ValueError("Reference evaluation requires exactly one declared solution asset")
        instance.candidate_schema.canonicalize(assets[0]["payload"]["candidate"])
    if name == "refinement":
        solutions = [item for item in assets if item["kind"] == "solution"]
        if len(solutions) != 1:
            raise ValueError("Refinement requires one versioned solution asset and an explicit input reuse decision")
        candidate = instance.candidate_schema.canonicalize(solutions[0]["payload"]["candidate"])
        if parameters.get("initial_design", candidate) != candidate:
            raise ValueError("Refinement starting candidate conflicts with the declared solution asset")
        parameters["initial_design"] = candidate
    if name == "frozen_policy":
        policies = [item for item in assets if item["kind"] == "policy"]
        if len(policies) != 1:
            raise ValueError("Frozen policy evaluation requires one declared policy asset")
        from optimization_framework.evaluation.inference import prepare
        prepare("dqn_policy:v1", instance, parameters, asset=policies[0])
    if name == "artifact_inference":
        if len(assets) != 1:
            raise ValueError("Artifact inference requires exactly one declared input asset")
        from optimization_framework.evaluation.inference import prepare
        _, _, values = prepare(parameters.get("adapter_id"), instance, parameters.get("parameters", {}), asset=assets[0])
        parameters = {"adapter_id": parameters["adapter_id"], "parameters": values}
    return parameters


def create(name, instance, parameters, seed, schedule_steps, training=None, assets=None, artifact_store=None, inference_registry=None):
    reason = capability_reason(name, instance)
    if reason:
        raise ValueError(reason)
    validate_parameters(name, instance, parameters, training, inference_registry)
    if name in FOURIER_PROPERTIES:
        from dqn_meent.flrl_optimizers import FourierOptimizer
        return FourierOptimizer(name, instance, parameters, seed)
    if name in MASK_LIBRARY_METHODS:
        from dqn_meent.mask_library_adapter import MaskLibraryAdapter
        return MaskLibraryAdapter(name, instance, parameters, seed)
    if name == "artifact_inference":
        from optimization_framework.evaluation.inference import create as create_inference
        return create_inference(instance, parameters, seed, assets or [], artifact_store, inference_registry)

    def factory(descriptor, config, seed, declared_assets):
        schema = CandidateSchema(**descriptor["candidate_schema"])
        if name == "evaluate_asset":
            from .reference import FixedSolution
            bind_inputs(name, instance, config, declared_assets)
            return FixedSolution(schema.canonicalize(declared_assets[0]["payload"]["candidate"]))
        if name == "frozen_policy":
            from .policy import FrozenPolicy
            config = bind_inputs(name, instance, config, declared_assets)
            policy = next(item for item in declared_assets if item["kind"] == "policy")
            return FrozenPolicy(instance, config, seed, policy, artifact_store)
        if name == "coordinate" or schema.representation != "binary" or schema.constraints:
            return BoundedSearch(schema, config, seed, coordinate=name == "coordinate")
        from .binary import make_optimizer
        optimizer = make_optimizer(name, schema.dimensions, seed, schedule_steps, config, training)
        bounds = descriptor.get("public", {}).get("objective_bounds", {}).get(instance.primary_objective.name)
        if name == "dqn" and bounds is not None:
            lo, hi = sorted(instance.primary_objective.utility(value) for value in bounds)
            optimizer.diagnostic_reward_upper_bound = hi ** 3 if optimizer.training.reward_mode == "paper" else hi - lo
        return optimizer

    optimizer = AskTellAdapter(factory)
    optimizer.initialize(instance.descriptor(), parameters, seed, assets or [])
    return optimizer
