"""Raw objectives at observed, fully attributed costs on compatible problems."""
from collections import defaultdict
import math
import statistics

from optimization_framework.contracts.base import content_hash
from optimization_framework.contracts.problems import ProblemInstance


AXES = {"worker_seconds": "elapsed_seconds", "evaluation_requests": "evaluations", "solver_executions": "solver_calls"}


def method_identity(trial):
    procedure = trial.get("experiment_spec", {})
    parameters = procedure.get("parameters") or {key: trial.get(key, {}) for key in ("algorithm", "algorithm_config", "training")}
    training = {key: value for key, value in parameters.get("training", {}).items() if key != "seed"}
    identity = {"algorithm": trial["algorithm"], "parameters": parameters.get("algorithm_config", {}),
        "training": training if trial["algorithm"] == "dqn" else {}, "schedule": procedure.get("schedule", {"steps": trial["schedule_steps"]}),
        "implementation": trial.get("implementation_version_id") or trial.get("scientific_source_hash") or "unknown:" + trial["id"],
        "runtime": trial.get("implementation_runtime_digest") or trial.get("scientific_environment"),
        "initial_assets": procedure.get("initial_assets", trial.get("initial_assets", [])),
        "asset_digests": procedure.get("asset_digests", {})}
    return content_hash(identity), identity


def number(value):
    return float(value) if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) else None


def observed_at(curve, budget):
    if not curve or budget < curve[0]["cost"] or budget > curve[-1]["cost"]:
        return None
    return next(point["objective"] for point in reversed(curve) if point["cost"] <= budget)


def thin(curve):
    """The same curve without the inside of each run of equal objectives.

    A best-so-far journal has one row per evaluation and changes at a few of them. Both ends
    of every run stay, so the curve reads and draws as before; a 60,000-evaluation experiment
    gives a few hundred points instead of 60,000.
    """
    last = len(curve) - 1
    return [point for index, point in enumerate(curve) if index in (0, last)
            or point["objective"] != curve[index - 1]["objective"] or point["objective"] != curve[index + 1]["objective"]]


def report(workspace, campaign_id, *, study_id=None, cost_axis=None, cost_view="full_attributed_cost", trial_ids=None):
    """Compare a study's experiments, or only those named in trial_ids, at the costs they were observed.

    A run that was continued (trial.continue) is one run: its last experiment stands for it, with the
    earlier ones' curves in front and their costs added. Methods are compared at the largest cost every
    compared run reached, so a comparison of long runs names them: one short run in the same study
    would otherwise set that cost.
    """
    if cost_view not in {"full_attributed_cost", "actual_expenditure"}:
        raise ValueError("Choose full_attributed_cost or actual_expenditure")
    campaign = workspace.store.get(campaign_id, "campaign")
    study_id = study_id or campaign.get("active_study_id")
    study = workspace.store.get(study_id, "study") if study_id else None
    if study and study["campaign_id"] != campaign_id:
        raise ValueError("Study belongs to another campaign")
    cost_axis = cost_axis or (study or {}).get("comparison", {}).get("cost_axis", "worker_seconds")
    cost_axis = "worker_seconds" if cost_axis == "full_worker_seconds" else cost_axis
    if cost_axis not in AXES:
        raise ValueError("Comparison supports worker_seconds, evaluation_requests, or solver_executions")
    # Capture output asset references together with their ledger: a trial cannot
    # advance to an asset newer than this report's cost snapshot. Release the
    # database before reading metrics or calculating any projection.
    with workspace.store.connection() as db:
        if not db.in_transaction:
            db.execute("BEGIN")
        trials = workspace.store.list("trial", campaign_id)
        tasks = {item["id"]: item for item in workspace.store.list("task", campaign_id)}
        hypotheses = {item["id"]: item for item in workspace.store.list("hypothesis", campaign_id)}
        amendments = {item["experiment_id"] for item in workspace.store.list("budget_amendment", campaign_id)}
        # Runtime-profile probes and equivalence replays measure the evaluator; they are no method's result.
        measurements = {identity for profile in workspace.store.list("runtime_profile", campaign_id)
                        for level in profile.get("levels", []) for identity in level.get("trial_ids", [])}
        measurements.update(item["replay_trial_id"] for item in workspace.store.list("evaluator_equivalence", campaign_id) if item.get("replay_trial_id"))
        costs = workspace.assets.cost_view()
    by_id = {trial["id"]: trial for trial in trials}
    chosen = None if trial_ids is None else set(trial_ids)
    if chosen is not None and chosen - set(by_id):
        raise ValueError("Not experiments of this campaign: " + ", ".join(sorted(chosen - set(by_id))))
    groups = {}
    excluded = []
    compared = []
    for trial in trials:
        if chosen is not None and trial["id"] not in chosen:
            continue
        if study_id and trial.get("study_id") != study_id:
            if chosen is not None:
                excluded.append({"trial_id": trial["id"], "reason": "The experiment belongs to another study"})
            continue
        if trial["id"] in measurements:
            excluded.append({"trial_id": trial["id"], "reason": "A measurement of the evaluator (runtime profile or equivalence replay), not an optimization experiment"})
            continue
        if trial.get("recipe") or trial.get("algorithm") == "validate" or trial.get("diagnostic_grant_id"):
            excluded.append({"trial_id": trial["id"], "reason": "Scientific diagnostics are separate evidence"})
            continue
        if not trial.get("problem"):
            excluded.append({"trial_id": trial["id"], "reason": "Historical experiment needs an explicit problem mapping"})
            continue
        compared.append(trial)
    continued = {trial["continues"]["trial_id"]: trial["id"] for trial in compared if trial.get("continues")}
    for trial in compared:
        if trial["id"] in continued:
            excluded.append({"trial_id": trial["id"], "reason": f"Continued by {continued[trial['id']]}; reported as the beginning of that run"})
            continue
        chain = [trial]
        while (previous := (chain[0].get("continues") or {}).get("trial_id")) in by_id and by_id[previous] not in chain:
            chain.insert(0, by_id[previous])
        root = chain[0]
        problem = ProblemInstance(**trial["problem"])
        condition = {"study_id": trial.get("study_id"), "evaluation_identity": problem.evaluation_identity,
            "primary_objective": problem.primary_objective.model_dump(mode="json"),
            "confirmation_protocol_id": trial.get("confirmation_protocol_id")}
        if cost_axis == "worker_seconds" and trial.get("device"):
            # A worker-second on the GPU buys other evaluations than one on CPU: on time, each device is its own comparison.
            condition["device"] = trial["device"]
        group_id = content_hash(condition)
        group = groups.setdefault(group_id, {"id": group_id, "condition": condition, "problem": problem.model_dump(mode="json"), "trials": []})
        latest = trial.get("result") or trial.get("progress") or {}
        # The run's method is the one it began as: a later part differs only in the source it was carried on under.
        identity, definition = method_identity(root)
        upstream = costs.attributed_costs(root.get("initial_assets", []) + root.get("contribution_asset_ids", root.get("implementation_cost_asset_ids", [])) + root.get("operational_cost_asset_ids", []), axes=[cost_axis])
        if root["algorithm"] == "package" and not root.get("implementation_cost_asset_ids"):
            upstream["quantities"][cost_axis].update(total=None, unknown_provenance=1)
            upstream["reason"] = "Implementation development cost has not been declared"
        upstream_cost = upstream["quantities"][cost_axis]["total"]
        points = {}
        unknown = False
        before = 0.   # what the earlier experiments of the run spent on this axis; None once one of them is unknown
        for member in chain:
            last = member.get("result") or member.get("progress") or {}
            rows = [*workspace.metrics(member["id"], fields=("best_objective", "elapsed_seconds", "evaluations", "solver_calls",
                "unknown_worker_cost", "unknown_solver_cost")), last]
            for row in rows:
                objective = number(row.get("best_objective"))
                direct = number(row.get(AXES[cost_axis]))
                if cost_axis == "solver_executions" and row.get("unknown_solver_cost"):
                    direct = None
                if cost_axis == "worker_seconds" and row.get("unknown_worker_cost"):
                    direct = None
                if objective is None:
                    continue
                direct = None if direct is None or before is None else direct + before
                cost = direct if cost_view == "actual_expenditure" else None if direct is None or upstream_cost is None else direct + upstream_cost
                if cost is None:
                    unknown = True
                    continue
                previous = points.get(cost)
                if previous is None or problem.primary_objective.better(objective, previous["objective"]):
                    points[cost] = {"cost": cost, "objective": objective, "direct_cost": direct,
                                    "upstream_cost": upstream_cost if cost_view == "full_attributed_cost" else 0.}
            if member is not trial:
                spent = number(last.get(AXES[cost_axis]))
                if (cost_axis == "solver_executions" and last.get("unknown_solver_cost")) or (cost_axis == "worker_seconds" and last.get("unknown_worker_cost")):
                    spent = None
                before = None if before is None or spent is None else before + spent
        assets = trial.get("latest_output_asset_ids", [])
        full = costs.attributed_costs(assets, axes=[cost_axis]) if assets else None
        # A measured final asset cost includes final checkpoint/export overhead.
        # No curve extrapolation is made: only its actually observed endpoint is added.
        final_cost = full["quantities"][cost_axis]["total"] if full and cost_view == "full_attributed_cost" else None
        if final_cost is not None and len(chain) > 1:
            final_cost = None if before is None else final_cost + before
        best = number(latest.get("best_objective"))
        if final_cost is not None and best is not None:
            points[final_cost] = {"cost": final_cost, "objective": best, "upstream_cost": upstream_cost,
                                  "direct_cost": None if upstream_cost is None else max(0., final_cost - upstream_cost)}
        scientific_complete = bool(latest.get("scientific_complete"))
        from .finalists import prototype_details
        details = prototype_details(trial, study, tasks.get(trial.get("task_id")), hypotheses.get(trial.get("hypothesis_id")))
        group["trials"].append({"id": trial["id"], "seed": trial["seed"], "method_id": identity, "method": definition,
            **details,
            **({"chain_trial_ids": [member["id"] for member in chain]} if len(chain) > 1 else {}),
            "algorithm": trial["algorithm"], "status": trial["status"], "best_objective": best,
            "scientific_complete": scientific_complete, "process_exit": latest.get("process_exit"),
            "allocation_stop": latest.get("allocation_stop"), "censored": not scientific_complete,
            "adaptive_extension": any(member["id"] in amendments for member in chain),
            "unknown_cost": unknown or upstream_cost is None, "upstream": upstream, "full_cost": full,
            "curve": thin([points[key] for key in sorted(points)])})
    for group in groups.values():
        usable = [trial for trial in group["trials"] if trial["curve"]]
        horizon = min((trial["curve"][-1]["cost"] for trial in usable), default=None)
        start = max((trial["curve"][0]["cost"] for trial in usable), default=None)
        if horizon is not None and start > horizon:
            horizon = None
        methods = defaultdict(list)
        for trial in group["trials"]:
            value = observed_at(trial["curve"], horizon) if horizon is not None else None
            methods[trial["method_id"]].append((trial, value))
        summaries = []
        for method_id, rows in methods.items():
            values = [value for _, value in rows if value is not None]
            seeds = [trial["seed"] for trial, value in rows if value is not None]
            summaries.append({"method_id": method_id, "algorithm": rows[0][0]["algorithm"], "trial_count": len(rows),
                "observed_count": len(values), "mean": statistics.mean(values) if values else None,
                "median": statistics.median(values) if values else None, "matched_cost": horizon,
                "independent_seed_ids": len(set(seeds)) == len(seeds), "uncertainty": "Not estimated by this descriptive report"})
        group["method_summaries"] = summaries
        group["common_observed_cost"] = horizon
    return {"schema_version": 1, "campaign_id": campaign_id, "study_id": study_id, "cost_view": cost_view,
            "cost_axis": cost_axis, "groups": list(groups.values()), "excluded": excluded,
            "actual_campaign_costs": costs.actual_costs(campaign_id,
                axes=("worker_seconds", "evaluation_requests", "solver_executions", "implementation_seconds", "model_seconds", "model_calls", "model_input_tokens", "model_output_tokens", "api_usd")),
            "interpretation": "Raw objectives on compatible instances and fidelities at observed costs; no extrapolation or cross-problem ranking."
                + (" On worker-seconds, runs on the GPU and on CPU are compared separately (condition.device); compare on evaluation_requests "
                   "to put them together." if len({group["condition"].get("device") for group in groups.values()} - {None}) > 1 else ""),
            "claim_level": "descriptive", "confirmation_protocol": (study or {}).get("confirmation") or None}
