"""Numerical comparisons that retain censored and incompatible experiments.

Adaptive development comparisons remain exploratory. Curves are step functions
of observed measurements; no extrapolated performance is turned into evidence.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
from collections import Counter, defaultdict
from dataclasses import asdict
from typing import Any

from optimization_framework.contracts.experiments import DELIBERATE_STOPS

import numpy as np

from optimization_framework.evaluation.registry import problems


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _latest(trial: dict) -> dict:
    return {**trial, **(trial.get("result") or {}), **(trial.get("progress") or {})}


def _physics(trial: dict, task: dict) -> dict:
    values = trial.get("physics", task.get("physics", {}))
    try:
        instance = problems.resolve("meent_grating", values)
        return {**instance.configuration, **instance.fidelity}
    except (TypeError, ValueError):
        # Retain unusual historical conditions separately rather than coercing.
        return dict(values) if isinstance(values, dict) else {"unknown": True}


def _condition(trial: dict, task: dict) -> dict:
    physics = _physics(trial, task)
    latest = _latest(trial)
    return {"task_id": trial.get("task_id"), "physics": physics,
            "charter_version": trial.get("charter_version"),
            "task_split": trial.get("task_split", task.get("split", "development")),
            "fourier_order": latest.get("fourier_order", physics.get("fourier_order")),
            "material_model": trial.get("material_model", physics.get("material")),
            "objective": trial.get("objective_id", "absolute_transmitted_order_+1"),
            "worker_threads": trial.get("worker_threads", trial.get("blas_threads", 1)),
            "numerical_status": latest.get("numerical_status", "screening")}


def _algorithm(trial: dict) -> tuple[str, dict]:
    identity = {"algorithm": trial.get("algorithm", "unknown"),
                "algorithm_config": trial.get("algorithm_config", {}),
                "code_hash": trial.get("code_hash", trial.get("source_hash", trial.get("code_version"))),
                "schedule_steps": trial.get("schedule_steps") if trial.get("algorithm") == "dqn" else None,
                "training": trial.get("training", {}) if trial.get("algorithm") == "dqn" else None,
                "initialization": trial.get("initialization", "independent_per_trial"),
                "shared_data_provenance": trial.get("shared_data_provenance")}
    if trial.get("implementation_version_id"):
        identity.update(implementation_version_id=trial["implementation_version_id"],
                        implementation_artifact_digest=trial.get("implementation_artifact_digest"),
                        implementation_runtime_digest=trial.get("implementation_runtime_digest"),
                        schedule_steps=trial.get("schedule_steps"))
    # Seed is a replicate identifier, not a hyperparameter distinguishing methods.
    if isinstance(identity["training"], dict):
        identity["training"] = {k: v for k, v in identity["training"].items() if k != "seed"}
    return f"{identity['algorithm']}:{_digest(identity)}", identity


def _observations(trial: dict, rows: list[dict]) -> list[dict]:
    points = []
    for row in [*rows, _latest(trial)]:
        efficiency = _number(row.get("best_efficiency", row.get("efficiency")))
        if efficiency is None:
            continue
        point = {"best_efficiency": efficiency}
        for axis in ("solver_calls", "elapsed_seconds", "evaluations", "step"):
            value = _number(row.get(axis))
            if value is not None and value >= 0:
                point[axis] = value
        if "solver_calls" in point or "elapsed_seconds" in point:
            points.append(point)
    # The latest state can repeat a row; retain the latest measured best at each
    # exact coordinate. Do not synthesize a zero-cost initial observation.
    unique = {}
    for point in points:
        coordinate = (point.get("solver_calls"), point.get("elapsed_seconds"), point.get("step"))
        unique[coordinate] = point
    return list(unique.values())


def _axis_curve(points: list[dict], axis: str) -> list[dict]:
    observed = {}
    for point in points:
        if axis in point:
            x = point[axis]
            observed[x] = max(observed.get(x, -float("inf")), point["best_efficiency"])
    return [{"x": x, "efficiency": y} for x, y in sorted(observed.items())]


def _at(curve: list[dict], budget: float) -> float | None:
    if not curve or curve[-1]["x"] < budget:
        return None  # A stopped observation is never extended into the future.
    eligible = [point["efficiency"] for point in curve if point["x"] <= budget]
    return eligible[-1] if eligible else None


def _interval(values: list[float]) -> dict:
    if not values:
        return {"mean": None, "median": None, "n": 0, "ci95": None,
                "method": "insufficient observations"}
    samples = np.asarray(values, dtype=float)
    result = {"mean": float(samples.mean()), "median": float(np.median(samples)), "n": len(values),
              "ci95": None, "method": "single observation; uncertainty unestimated"}
    if len(values) >= 2:
        draws = np.random.default_rng(0).choice(samples, size=(2000, len(samples)), replace=True).mean(axis=1)
        result.update(ci95=[float(x) for x in np.quantile(draws, [.025, .975])],
                      method="2000-resample percentile bootstrap across observed seeds; exploratory")
    return result


def _allocation(trial: dict) -> tuple[Any, Any]:
    return trial.get("max_steps"), trial.get("wall_seconds")


def _complete(trial: dict) -> bool:
    status = trial.get("status")
    if trial.get("stopped_by") in DELIBERATE_STOPS:
        return False
    if status == "completed":
        return True
    if status == "budget_exhausted":
        latest = _latest(trial)
        steps, seconds = _number(latest.get("step")), _number(latest.get("elapsed_seconds"))
        max_steps, max_seconds = _number(trial.get("max_steps")), _number(trial.get("wall_seconds"))
        return bool((steps is not None and max_steps is not None and steps >= max_steps) or
                    (seconds is not None and max_seconds is not None and seconds >= max_seconds))
    return False


def _summary(trial: dict, task: dict, observations: list[dict]) -> dict:
    latest = _latest(trial)
    algorithm_id, identity = _algorithm(trial)
    status = trial.get("status", latest.get("status", "unknown"))
    curves = {axis: _axis_curve(observations, axis) for axis in ("solver_calls", "elapsed_seconds")}
    validation = trial.get("validation") or latest.get("validation")
    validations = validation if isinstance(validation, list) else validation.get("validation", validation.get("designs", [])) if isinstance(validation, dict) else []
    validated = [item for item in validations if item.get("converged") and _number(item.get("efficiency")) is not None]
    return {"id": trial.get("id"), "task_id": trial.get("task_id"), "seed": trial.get("seed"),
            "algorithm": trial.get("algorithm"), "algorithm_id": algorithm_id, "algorithm_identity": identity,
            "status": status, "censored": not _complete(trial), "full_allocation": _complete(trial),
            "reason": trial.get("reason", latest.get("reason")),
            "best_efficiency": _number(latest.get("best_efficiency")),
            "numerical_status": latest.get("numerical_status", "screening"),
            "validated_best_efficiency": max((float(i["efficiency"]) for i in validated), default=None),
            "validated_design_count": len(validated),
            "execution_seconds": max(_number(trial.get("execution_seconds")) or 0, _number(latest.get("elapsed_seconds")) or 0),
            "solver_calls": _number(latest.get("solver_calls")), "cache_hits": _number(latest.get("cache_hits")),
            "unconfirmed_solver_calls": _number(latest.get("unconfirmed_solver_calls")) or 0,
            "allocation": {"max_steps": trial.get("max_steps"), "wall_seconds": trial.get("wall_seconds")},
            "condition": _condition(trial, task), "curves": curves}


def _mean_curve(summaries: list[dict], axis: str) -> dict:
    curves = [s["curves"][axis] for s in summaries if s["curves"][axis]]
    if not curves:
        return {"points": [], "n": 0, "common_observed_budget": None}
    horizon = min(c[-1]["x"] for c in curves)
    start = max(c[0]["x"] for c in curves)
    grid = sorted({p["x"] for c in curves for p in c if start <= p["x"] <= horizon})
    if len(grid) > 100:
        grid = [grid[round(i * (len(grid) - 1) / 99)] for i in range(100)]
    points = []
    seeds = [s["seed"] for s in summaries if s["curves"][axis]]
    independent_seeds = None not in seeds and len(set(seeds)) == len(seeds)
    for budget in grid:
        values = [_at(c, budget) for c in curves]
        if all(v is not None for v in values):
            # Numeric guards above ensure these are observed floats.
            estimate = _interval(values)
            if not independent_seeds:
                estimate.update(ci95=None, method="Repeated or missing seed identifiers; independent uncertainty unestimated")
            points.append({"budget": budget, **estimate})
    return {"points": points, "n": len(curves), "common_observed_budget": horizon,
            "scope": "common observed support; no extension past stopped curves"}


def _paired(group_id: str, members: list[dict], originals: dict) -> list[dict]:
    by_algorithm = defaultdict(list)
    for member in members:
        by_algorithm[member["algorithm_id"]].append(member)
    result = []
    for a, b in itertools.combinations(sorted(by_algorithm), 2):
        seeds_a, seeds_b = defaultdict(list), defaultdict(list)
        for item in by_algorithm[a]:
            seeds_a[item["seed"]].append(item)
        for item in by_algorithm[b]:
            seeds_b[item["seed"]].append(item)
        common_seeds = set(seeds_a) & set(seeds_b) - {None}
        unique_seeds = [s for s in common_seeds if len(seeds_a[s]) == len(seeds_b[s]) == 1]
        pairs = [(seeds_a[s][0], seeds_b[s][0]) for s in sorted(unique_seeds)]
        ambiguous = sorted(common_seeds - set(unique_seeds))
        for axis in ("solver_calls", "elapsed_seconds"):
            available = [(left, right) for left, right in pairs if left["curves"][axis] and right["curves"][axis]]
            if not available:
                continue
            horizon = min(min(left["curves"][axis][-1]["x"], right["curves"][axis][-1]["x"]) for left, right in available)
            differences = []
            for left, right in available:
                value_a, value_b = _at(left["curves"][axis], horizon), _at(right["curves"][axis], horizon)
                if value_a is not None and value_b is not None:
                    differences.append({"seed": left["seed"], "trial_a": left["id"], "trial_b": right["id"],
                                        "efficiency_a": value_a, "efficiency_b": value_b, "difference": value_a - value_b,
                                        "contains_censored_run": left["censored"] or right["censored"]})
            result.append({"group_id": group_id, "algorithm_a": a, "algorithm_b": b,
                           "budget_axis": axis, "budget": horizon, "pairs": differences,
                           "n_pairs": len(differences), "difference": _interval([d["difference"] for d in differences]),
                           "ambiguous_seeds_excluded": ambiguous,
                           "scope": "matched task and seed, common observed budget; a minus b; exploratory",
                           "timing_caveat": "Concurrent worker contention is uncontrolled; use dedicated timing runs for a final cost claim." if axis == "elapsed_seconds" else None})
        full = [(left, right) for left, right in pairs if left["full_allocation"] and right["full_allocation"] and
                _allocation(originals[left["id"]]) == _allocation(originals[right["id"]]) and
                left["best_efficiency"] is not None and right["best_efficiency"] is not None]
        # Distinct allocation protocols cannot be pooled into a single endpoint.
        full_by_allocation = defaultdict(list)
        for left, right in full:
            full_by_allocation[_allocation(originals[left["id"]])].append((left, right))
        for allocation, matched in full_by_allocation.items():
            differences = [{"seed": left["seed"], "trial_a": left["id"], "trial_b": right["id"],
                            "difference": left["best_efficiency"] - right["best_efficiency"]} for left, right in matched]
            result.append({"group_id": group_id, "algorithm_a": a, "algorithm_b": b, "budget_axis": "full_allocation",
                           "allocation": {"max_steps": allocation[0], "wall_seconds": allocation[1]}, "pairs": differences,
                           "n_pairs": len(differences), "difference": _interval([d["difference"] for d in differences]),
                           "scope": "completed matched task/seed/allocation; a minus b; exploratory",
                           "ambiguous_seeds_excluded": ambiguous})
    return result


def _thresholds(group_id: str, members: list[dict]) -> list[dict]:
    grouped = defaultdict(list)
    for member in members:
        grouped[member["algorithm_id"]].append(member)
    results = []
    for algorithm, runs in grouped.items():
        for threshold in (.5, .8, .9):
            rows = []
            for run in runs:
                curve = run["curves"]["solver_calls"]
                reached = next((p for p in curve if p["efficiency"] >= threshold), None)
                time_curve = run["curves"]["elapsed_seconds"]
                timed = next((p for p in time_curve if p["efficiency"] >= threshold), None)
                rows.append({"trial_id": run["id"], "reached": reached is not None,
                             "solver_calls_to_threshold": reached["x"] if reached else None,
                             "seconds_to_threshold": timed["x"] if timed else None,
                             "observed_until_solver_calls": curve[-1]["x"] if curve else None,
                             "right_censored": reached is None and run["censored"],
                             "complete_allocation_without_hit": reached is None and run["full_allocation"]})
            observed = [r for r in rows if r["observed_until_solver_calls"] is not None]
            hit = sum(r["reached"] for r in observed)
            results.append({"group_id": group_id, "algorithm_id": algorithm, "threshold": threshold,
                            "n_observed": len(observed), "hits": hit, "right_censored": sum(r["right_censored"] for r in observed),
                            "observed_hit_fraction": hit / len(observed) if observed else None,
                            "cost_among_hits": _interval([r["solver_calls_to_threshold"] for r in observed if r["reached"]]),
                            "trials": rows, "interpretation": "Descriptive hits over observed allocations, not an uncensored success probability; cost among hits excludes failures.",
                            "censoring_warning": "Researcher stops may depend on promise, so censoring is not assumed independent."})
    return results


def _aggregate_pairs(paired: list[dict], groups: list[dict], summaries: list[dict]) -> list[dict]:
    """Equal-task weighting after matching seeds within each task.

    Pool only the supplied family at a common observed budget. Charter/fidelity
    and numerical-validity boundaries remain intact across target conditions.
    """
    group_map = {g["id"]: g for g in groups}
    trial_map = {s["id"]: s for s in summaries}
    families = defaultdict(list)
    for comparison in paired:
        if comparison["budget_axis"] not in {"solver_calls", "elapsed_seconds"} or not comparison["pairs"]:
            continue
        condition = group_map[comparison["group_id"]]["condition"]
        family = {key: condition[key] for key in ("charter_version", "task_split", "fourier_order", "material_model", "objective", "worker_threads", "numerical_status")}
        family["geometry"] = {key: condition["physics"].get(key) for key in ("n_cells", "thickness_nm", "n_incident", "n_exit")}
        families[(comparison["algorithm_a"], comparison["algorithm_b"], comparison["budget_axis"], _digest(family))].append(comparison)
    results = []
    for (a, b, axis, family_id), comparisons in families.items():
        horizon = min(c["budget"] for c in comparisons)
        task_differences = []
        for comparison in comparisons:
            differences = []
            for pair in comparison["pairs"]:
                left = _at(trial_map[pair["trial_a"]]["curves"][axis], horizon)
                right = _at(trial_map[pair["trial_b"]]["curves"][axis], horizon)
                if left is not None and right is not None:
                    differences.append(left - right)
            if differences:
                task_differences.append({"task_id": group_map[comparison["group_id"]]["task_id"],
                                         "differences": differences, "mean": float(np.mean(differences))})
        if not task_differences:
            continue
        ci = None
        n_tasks = len(task_differences)
        if n_tasks > 1 or len(task_differences[0]["differences"]) > 1:
            rng = np.random.default_rng(0)
            bootstrap = []
            for _ in range(2000):
                chosen = rng.integers(0, n_tasks, n_tasks)
                means = []
                for index in chosen:
                    differences = task_differences[index]["differences"]
                    means.append(rng.choice(differences, len(differences), replace=True).mean())
                bootstrap.append(np.mean(means))
            ci = [float(x) for x in np.quantile(bootstrap, [.025, .975])]
        results.append({"algorithm_a": a, "algorithm_b": b, "family_id": family_id, "budget_axis": axis,
                        "budget": horizon, "n_tasks": n_tasks, "n_pairs": sum(len(t["differences"]) for t in task_differences),
                        "mean_difference": float(np.mean([t["mean"] for t in task_differences])), "ci95": ci,
                        "tasks": task_differences, "weighting": "equal task weights, matched seed differences within each task",
                        "method": "hierarchical percentile bootstrap of observed tasks then seeds" if ci else "insufficient independent observations",
                        "scope": "supplied compatible task family only; adaptive task selection limits generalization"})
    return results


def analyze_trials(trials: list[dict], metrics_by_id: dict[str, list[dict]], tasks: list[dict]) -> dict:
    """Return auditable JSON for the comparison workspace and exports.

    Every run remains visible, including failed and stopped trials. Pairing is
    restricted to matching task, physics, fidelity, charter, seed, and algorithm
    identity; duplicate seed runs are excluded instead of cherry-picked.
    """
    task_map = {task["id"]: task for task in tasks}
    originals = {trial["id"]: trial for trial in trials}
    summaries, grouped, validation_runs = [], defaultdict(list), []
    for trial in trials:
        task = task_map.get(trial.get("task_id"), {})
        rows = metrics_by_id.get(trial["id"], [])
        if isinstance(rows, dict):
            rows = rows.get("metrics", rows.get("rows", []))
        summary = _summary(trial, task, _observations(trial, rows))
        summaries.append(summary)
        if trial.get("algorithm") == "validate":
            validation_runs.append(summary)
        else:
            grouped[_digest(summary["condition"])].append(summary)
    groups, paired, thresholds = [], [], []
    for group_id, members in sorted(grouped.items()):
        algorithms = []
        by_algorithm = defaultdict(list)
        for member in members:
            by_algorithm[member["algorithm_id"]].append(member)
        for algorithm_id, runs in sorted(by_algorithm.items()):
            complete = [r for r in runs if r["full_allocation"] and r["best_efficiency"] is not None]
            allocations = {_allocation(originals[r["id"]]) for r in complete}
            endpoint = _interval([r["best_efficiency"] for r in complete]) if len(allocations) <= 1 else None
            seeds = [r["seed"] for r in complete]
            if endpoint is not None and (None in seeds or len(set(seeds)) != len(seeds)):
                endpoint.update(ci95=None, method="Repeated or missing seed identifiers; independent uncertainty unestimated")
            algorithms.append({"algorithm_id": algorithm_id, "algorithm": runs[0]["algorithm"],
                               "identity": runs[0]["algorithm_identity"], "trial_ids": [r["id"] for r in runs],
                               "n_trials": len(runs), "n_completed": len(complete), "n_censored": sum(r["censored"] for r in runs),
                               "completed_efficiency": endpoint,
                               "allocation_compatible": len(allocations) <= 1,
                               "curves": {axis: _mean_curve(runs, axis) for axis in ("solver_calls", "elapsed_seconds")},
                               "execution_seconds": sum(r["execution_seconds"] for r in runs),
                               "solver_calls": sum(r["solver_calls"] or 0 for r in runs)})
        groups.append({"id": group_id, "task_id": members[0]["task_id"], "condition": members[0]["condition"],
                       "algorithms": algorithms, "trial_ids": [r["id"] for r in members],
                       "evidence_label": "frozen_per_finalist_protocol" if all(originals[r["id"]].get("confirmation_protocol_hash") for r in members) else "exploratory_development"})
        paired.extend(_paired(group_id, members, originals))
        thresholds.extend(_thresholds(group_id, members))
    best = max((s["best_efficiency"] for s in summaries if s["best_efficiency"] is not None and s["algorithm"] != "validate"), default=None)
    validated_best = max((s["validated_best_efficiency"] for s in summaries if s["validated_best_efficiency"] is not None), default=None)
    return {"summary": {"n_trials": len(trials), "n_comparison_groups": len(groups),
                        "status_counts": dict(Counter(s["status"] for s in summaries)),
                        "execution_seconds": sum(s["execution_seconds"] for s in summaries),
                        "solver_calls": sum(s["solver_calls"] or 0 for s in summaries),
                        "unconfirmed_solver_calls": sum(s["unconfirmed_solver_calls"] for s in summaries),
                        "cache_hits": sum(s["cache_hits"] or 0 for s in summaries),
                        "best_observed_screening_efficiency": best,
                        "best_converged_efficiency": validated_best,
                        "censored_trials": sum(s["censored"] for s in summaries)},
            "groups": groups, "paired_comparisons": paired, "aggregate_comparisons": _aggregate_pairs(paired, groups, summaries), "thresholds": thresholds,
            "trials": summaries, "validation_runs": validation_runs,
            "limitations": [
                "Finalist protocols freeze at each finalist's first confirmation launch; this is not a jointly preregistered comparison with fixed test families and seeds.",
                "Adaptively chosen development data support exploratory decisions, not a generalization claim.",
                "Intervals describe variation in observed seeds; a single seed has no estimated uncertainty.",
                "Stopped, failed, and ongoing runs are censored; their curves are never extrapolated to full budget.",
                "Screening efficiencies are separate from convergence-checked binary-device measurements.",
                "Timing under concurrent workers includes contention; final efficiency-cost claims need controlled resources.",
                "Differences in charter, physics, solver fidelity, and initialization remain separate conditions or method variants.",
                "Algorithm execution costs exclude LLM reasoning; the campaign ledger supplies total research spending.",
            ]}
