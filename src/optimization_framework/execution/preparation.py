"""Normalize a procedure with the same captured contracts that will execute it."""
from optimization_framework.contracts.experiments import CompletionCondition
from optimization_framework.contracts.problems import ProblemInstance
from optimization_framework.contracts.requests import TrialInput
from optimization_framework.evaluation.diagnostics import prepare as prepare_diagnostics
from optimization_framework.optimizers.config import normalize_training
from optimization_framework.optimizers.registry import bind_inputs, capabilities, capability_reason, methods, validate_parameters


def prepare(request, problem, assets, implementation=None, *, evaluator_manifest=None):
    request = request if isinstance(request, TrialInput) else TrialInput(**request)
    problem = problem if isinstance(problem, ProblemInstance) else ProblemInstance(**problem)
    registry = None
    if evaluator_manifest is not None:
        from optimization_framework.evaluation.generated import declared_registry
        registry = declared_registry(problem, evaluator_manifest)
    native = {item["id"] for item in methods()}
    if request.algorithm not in native | {"recipe", "validate", "package"}:
        raise ValueError("Unknown executable algorithm; proposed code must be verified before execution")
    parameters = request.algorithm_config
    if request.algorithm in native:
        reason = capability_reason(request.algorithm, problem)
        if reason:
            raise ValueError(reason)
        parameters = bind_inputs(request.algorithm, problem, parameters, assets)
    try:
        training = normalize_training(request.training)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid training configuration: {exc}") from exc
    if request.algorithm in native:
        validate_parameters(request.algorithm, problem, parameters, training)
    completion = CompletionCondition(**(request.completion or {"count": request.max_steps}))
    if request.algorithm == "evaluate_asset" and (request.max_steps != 1 or completion.unit != "evaluation_requests" or completion.count != 1):
        raise ValueError("Reference evaluation freezes one request; repeated evaluations need separate declared cells")
    declared = capabilities(request.algorithm, parameters, implementation)
    if completion.unit not in declared.completion_units:
        raise ValueError("This implementation does not declare an optimizer decision counter")
    inference_binding = {}
    if request.algorithm in {"artifact_inference", "frozen_policy"}:
        from optimization_framework.evaluation.inference import adapters
        identity = parameters["adapter_id"] if request.algorithm == "artifact_inference" else "dqn_policy:v1"
        inference_binding["inference_adapter"] = adapters.get(identity)[1].model_dump(mode="json")
    return {**inference_binding, "algorithm_config": parameters, "training": training, "completion": completion.model_dump(exclude={"schema_version"}),
            "diagnostics": [schedule.model_dump(mode="json") for schedule in prepare_diagnostics(request, problem, declared, registry=registry)]}
