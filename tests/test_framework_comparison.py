"""General comparisons retain raw minimization scores and incomplete runs."""
from optimization_framework.analysis.general import report
from optimization_framework.contracts.requests import CampaignInput, TaskInput, TrialInput
from optimization_framework.execution.service import Workspace
from optimization_framework.execution.worker import run


def test_general_report_separates_problem_identities_and_clean_incomplete_exits(tmp_path):
    workspace = Workspace(tmp_path)
    campaign = workspace.create_campaign(CampaignInput(name="Two problems", compute_budget_seconds=200, validation_reserve_seconds=0,
        tasks=[TaskInput(name="Continuous", problem_id="bounded_continuous", configuration={"dimensions": 2}),
               TaskInput(name="Optical", physics={"n_cells": 4, "fourier_order": 1})]))
    tasks = workspace.current_tasks(campaign["id"])
    for task, method in zip(tasks, ["coordinate", "hillclimb"]):
        trial = workspace.create_trial(TrialInput(campaign_id=campaign["id"], task_id=task["id"], algorithm=method, max_steps=4, wall_seconds=20))
        result = run(workspace.job_dir(trial["id"]))
        assert result["status"] == "completed"
        trial.update(result=result, progress=result, attempt=1, status="completed")
        workspace.store.put("trial", trial)
        workspace.capture_evidence(trial)
    result = report(workspace, campaign["id"], cost_axis="evaluation_requests")
    assert result["cost_view"] == "full_attributed_cost" and len(result["groups"]) == 2
    continuous = next(group for group in result["groups"] if group["problem"]["definition_id"] == "bounded_continuous")
    assert continuous["condition"]["primary_objective"]["direction"] == "minimize"
    assert all("efficiency" not in point for point in continuous["trials"][0]["curve"])
    assert continuous["common_observed_cost"] == 4
    assert result["actual_campaign_costs"]["quantities"]["evaluation_requests"]["total"] == 8
    row = workspace.store.get(continuous["trials"][0]["id"], "trial")
    row["result"].update(scientific_complete=False, process_exit=0, allocation_stop="wall_budget_exhausted")
    workspace.store.put("trial", row)
    revised = report(workspace, campaign["id"], cost_axis="evaluation_requests")
    row = next(trial for group in revised["groups"] for trial in group["trials"] if trial["id"] == row["id"])
    assert row["censored"] and row["process_exit"] == 0


def test_a_thinned_curve_reads_the_same_at_every_cost():
    import random
    from optimization_framework.analysis.general import observed_at, thin
    random.seed(3)
    best, curve = 0., []
    for cost in range(1, 60001):   # one row per evaluation of a long run; the best changes at a few of them
        if random.random() < .002:
            best += random.random() / 100
        curve.append({"cost": float(cost), "objective": best})
    thinned = thin(curve)
    assert len(thinned) < 400 and thinned[0] == curve[0] and thinned[-1] == curve[-1]
    for budget in [.5, 1, 1.5, 60000, 60000.5, *(random.uniform(1, 60000) for _ in range(300))]:
        assert observed_at(thinned, budget) == observed_at(curve, budget)
    changes = [index for index in range(1, len(curve)) if curve[index]["objective"] != curve[index - 1]["objective"]]
    assert all(curve[index] in thinned and curve[index - 1] in thinned for index in changes)   # it draws as before
    assert thin([]) == [] and thin(curve[:1]) == curve[:1] and thin(curve[:2]) == curve[:2]


def test_evaluator_measurements_are_not_reported_as_methods(tmp_path):
    workspace = Workspace(tmp_path)
    campaign = workspace.create_campaign(CampaignInput(name="Measured evaluator", compute_budget_seconds=200, validation_reserve_seconds=0,
        tasks=[TaskInput(name="Continuous", problem_id="bounded_continuous", configuration={"dimensions": 2})]))
    task = workspace.current_tasks(campaign["id"])[0]
    trials = []
    for method in ("coordinate", "random", "random"):
        trial = workspace.create_trial(TrialInput(campaign_id=campaign["id"], task_id=task["id"], algorithm=method, max_steps=4, wall_seconds=20,
                                                  seed=len(trials)))
        result = run(workspace.job_dir(trial["id"]))
        trial.update(result=result, progress=result, attempt=1, status="completed")
        workspace.store.put("trial", trial)
        workspace.capture_evidence(trial)
        trials.append(trial["id"])
    search, probe, replay = trials
    workspace.store.put("runtime_profile", {"id": "runtime_profile_one", "campaign_id": campaign["id"], "levels": [{"workers": 1, "trial_ids": [probe]}]})
    workspace.store.put("evaluator_equivalence", {"id": "evaluator_equivalence_one", "campaign_id": campaign["id"], "replay_trial_id": replay})
    result = report(workspace, campaign["id"], cost_axis="evaluation_requests")
    assert [trial["id"] for group in result["groups"] for trial in group["trials"]] == [search]
    assert {row["trial_id"] for row in result["excluded"] if "measurement of the evaluator" in row["reason"]} == {probe, replay}
    assert [row["algorithm"] for row in result["groups"][0]["method_summaries"]] == ["coordinate"]


def test_runs_on_the_gpu_and_on_cpu_are_compared_apart_on_time_and_together_on_evaluations(tmp_path):
    workspace = Workspace(tmp_path)
    campaign = workspace.create_campaign(CampaignInput(name="Two devices", compute_budget_seconds=200, validation_reserve_seconds=0,
        tasks=[TaskInput(name="Continuous", problem_id="bounded_continuous", configuration={"dimensions": 2})]))
    task = workspace.current_tasks(campaign["id"])[0]
    for seed, device in enumerate(("cuda", "cpu", "cpu")):
        trial = workspace.create_trial(TrialInput(campaign_id=campaign["id"], task_id=task["id"], algorithm="random", max_steps=4, wall_seconds=20, seed=seed))
        result = run(workspace.job_dir(trial["id"]))
        # Placed as the scheduler would place a run of an evaluator that can use the GPU.
        trial.update(result=result, progress=result, attempt=1, status="completed", device=device)
        workspace.store.put("trial", trial)
        workspace.capture_evidence(trial)
    on_time = report(workspace, campaign["id"], cost_axis="worker_seconds")
    assert sorted((group["condition"]["device"], len(group["trials"])) for group in on_time["groups"]) == [("cpu", 2), ("cuda", 1)]
    assert "compared separately" in on_time["interpretation"]
    on_evaluations = report(workspace, campaign["id"], cost_axis="evaluation_requests")
    assert [len(group["trials"]) for group in on_evaluations["groups"]] == [3] and "device" not in on_evaluations["groups"][0]["condition"]
    assert "compared separately" not in on_evaluations["interpretation"]
