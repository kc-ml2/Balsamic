"""Fixed trusted compiler operations, executed from a captured source tree."""
import argparse
import json
from pathlib import Path

from optimization_framework.execution.provenance import verify
from optimization_framework.storage.sqlite import atomic_json


def install_entries(manifest):
    from optimization_framework.evaluation.registry import ProblemRegistry
    from optimization_framework.evaluation import registry

    registry.problems = ProblemRegistry.from_entries(manifest["entry_points"])
    from optimization_framework.evaluation import inference
    if "inference_entry_points" in manifest:
        inference.adapters = inference.InferenceRegistry(manifest["inference_entry_points"])
    from optimization_framework.evaluation import registered_recipes
    registered_recipes.recipes = registered_recipes.RecipeRegistry(manifest.get("recipe_entry_points", {}))
    if "optimizer_entry_points" in manifest:
        from optimization_framework.optimizers import plugins
        plugins.installed = plugins.OptimizerPlugins(manifest["optimizer_entry_points"])


def problem_registry(directory, problem):
    from optimization_framework.evaluation.registry import problems
    registry = problems
    if directory is not None:
        from optimization_framework.storage.sqlite import read_json
        specification = read_json(directory / "spec.json", {})
        if specification and specification.get("problem") != problem:
            raise ValueError("Analysis must use the captured experiment's frozen problem")
        if specification.get("evaluator_version_id"):
            from optimization_framework.evaluation.generated import pinned_evaluator
            registry = registry.extended(pinned_evaluator(directory, specification))
    return registry


def execute(operation, payload, *, directory=None):
    if operation == "trial.prepare":
        from optimization_framework.execution.preparation import prepare
        return prepare(payload["request"], payload["problem"], payload["assets"], payload.get("implementation"),
            evaluator_manifest=payload.get("evaluator_manifest"))
    if operation == "inference.compile":
        from optimization_framework.evaluation.inference import compile_inference
        return compile_inference(payload["problem"], payload["rollout"], payload["assets"])
    if operation.startswith("rule."):
        from optimization_framework.analysis import rules
        if operation == "rule.freeze":
            from optimization_framework.contracts.problems import ProblemInstance
            return rules.freeze(payload["request"], payload["kind"], [ProblemInstance(**value) for value in payload["instances"]])
        if operation == "rule.evaluate":
            return rules.evaluate(payload["binding"], payload["evidence"])
        if operation == "rule.check_design":
            return rules.check_design(payload["binding"], payload["design"])
    if operation == "problem.describe":
        from optimization_framework.contracts.problems import ProblemInstance
        problem = ProblemInstance.model_validate(payload["problem"])
        registry = problem_registry(directory, payload["problem"])
        adapter = registry.get(problem.definition_id)
        if adapter.resolve(problem.configuration, problem.fidelity) != problem:
            raise ValueError("The captured adapter does not resolve this problem version")
        return adapter.describe().model_dump(mode="json")
    if operation == "recipe.compile":
        from optimization_framework.evaluation.recipes import compile_recipe
        registry = problem_registry(directory, payload["problem"])
        return compile_recipe(payload["problem"], payload["recipe_id"], payload["parameters"], payload["subjects"], registry=registry)
    raise ValueError("Unsupported frozen compiler operation")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        manifest = json.loads((args.directory / "execution-manifest.json").read_text())
        verify(args.directory, manifest)
        install_entries(manifest)
        request = json.loads(args.request.read_text())
        result = {"result": execute(request["operation"], request["payload"], directory=args.directory)}
    except (ValueError, KeyError, TypeError, OSError) as exc:
        result = {"error": str(exc)}
    atomic_json(args.output, result)


if __name__ == "__main__":
    main()
