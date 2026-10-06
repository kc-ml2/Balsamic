"""Adaptive allocation consumes measured equal-time endpoints and frozen cells."""
import json
import threading
from types import SimpleNamespace

import pytest

from optimization_framework.contracts.racing import RaceCreateInput, RaceDecisionInput
from optimization_framework.contracts.requests import CampaignInput, ControlInput, TaskInput, TrialInput
from optimization_framework.execution.metrics import MetricProjectionCache
from optimization_framework.execution.racing import AdaptiveRacing, activity_gate, adaptive_assessment, confirmation_statistics
from optimization_framework.storage.sqlite import Store


class FakeWorkspace:
    def __init__(self, directory):
        self.directory = directory
        self.store = Store(directory)
        self.lock = threading.RLock()
        self._metric_projections = MetricProjectionCache()
        self.controls = []
        self.created = []

    def job_dir(self, trial_id):
        path = self.directory / "trials" / trial_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def create_trial(self, request):
        trial = {**request.model_dump(mode="json"), "id": "trial_" + str(len(self.created)), "status": "queued",
            "execution_seconds": 0., "progress": {}, "result": None, "attempt": 0, "control_revision": 0}
        self.store.put("trial", trial)
        self.created.append(trial)
        return trial

    def control(self, trial_id, command, *, authority="researcher", reason=None):
        trial = self.store.get(trial_id, "trial")
        if command.action == "stop":
            trial["stopped_by"] = authority
        if command.wall_seconds is not None:
            assert command.wall_seconds >= trial["wall_seconds"]
            trial["wall_seconds"] = command.wall_seconds
        if command.max_steps is not None:
            trial["max_steps"] = command.max_steps
        trial["status"] = {"extend": "queued", "resume": "queued", "pause": "paused", "stop": "stopped"}[command.action]
        self.controls.append((trial_id, command.action))
        self.store.put("trial", trial)
        return trial


def setup_race(tmp_path):
    workspace = FakeWorkspace(tmp_path)
    workspace.store.put("campaign", {"id": "campaign", "active_study_id": "study", "version": 1})
    workspace.store.put("task", {"id": "task", "campaign_id": "campaign", "split": "development", "problem": {
        "configuration": {"wavelength_nm": 1050}, "fidelity": {"rcwa_order_x": 10, "rcwa_order_y": 5}}})
    workspace.store.put("trial", {"id": "old", "campaign_id": "campaign", "task_id": "task", "algorithm": "motif_surgery",
        "algorithm_config": {}, "seed": 17, "execution_seconds": 540., "wall_seconds": 540., "max_steps": 100000,
        "status": "budget_exhausted", "progress": {"elapsed_seconds": 540., "checkpoint_available": True, "resume_supported": True}})
    workspace.store.put("asset", {"id": "fixture", "campaign_id": "campaign", "producer_id": "old", "kind": "solution"})
    controller = AdaptiveRacing(workspace)
    race = controller.create("campaign", {"task_id": "task", "configurations": [
        {"id": "motif", "algorithm": "motif_surgery", "pilot_trial_ids": ["old"]},
        {"id": "ppo", "algorithm": "flrl_ppo", "algorithm_config": {"episode_length": 128}},
        {"id": "random", "algorithm": "flrl_lsf_random"}], "baseline_configuration_id": "random",
        "preflight_subject_trial_ids": ["old"], "preflight_source_asset_ids": ["fixture"]})
    return workspace, controller, race


def pass_preflight(workspace, controller, race, *, changed=False, higher_task=None):
    workspace.store.put("trial", {"id": "source", "campaign_id": "campaign", "task_id": "task", "algorithm": "evaluate_asset",
        "seed": 17, "wall_seconds": 120, "status": "completed", "initial_assets": ["fixture"], "execution_seconds": 5.,
        "progress": {}, "result": {"scientific_complete": True}})
    controller.decide("campaign", {"race_id": race["id"], "action": "register_preflight", "evidence_trial_ids": ["source"], "rationale": "Frozen fixture remeasurement"})
    low = {"mean_plus1_transmission": .2, "te_plus1_transmission": .2, "tm_plus1_transmission": .2}
    high = {key: value + (.04 if changed else .001) for key, value in low.items()}
    finding = {"complete": True, "verdict": "passed", "highest_fidelity": {"rcwa_order_x": 14, "rcwa_order_y": 7}, "observations": [
        {"fidelity": {"rcwa_order_x": 10, "rcwa_order_y": 5}, "objectives": low},
        {"fidelity": {"rcwa_order_x": 14, "rcwa_order_y": 7}, "objectives": high}]}
    workspace.store.put("trial", {"id": "check", "campaign_id": "campaign", "task_id": "task", "algorithm": "recipe",
        "seed": 17, "wall_seconds": 120, "status": "completed", "parent_trial_id": "source", "execution_seconds": 12., "progress": {},
        "result": {"scientific_complete": True, "recipe_result": {"recipe_id": "meent_2d_order_convergence:v1", "complete": True,
            "verdict": "passed", "subjects": [finding]}}})
    controller.decide("campaign", {"race_id": race["id"], "action": "register_preflight", "evidence_trial_ids": ["check"], "rationale": "Actual numerical check"})
    calibration_id = "calibration_" + (higher_task or "original")
    workspace.store.put_immutable("race_resource_calibration", {"id": calibration_id, "campaign_id": "campaign", "race_id": race["id"],
        "complete": True, "safe": True, "measurements": {"max_workers": 2, "numerical_threads": 1, "task_id": higher_task or "task"}})
    profile = {"max_workers": 2, "numerical_threads": 1, "calibrated": True, "calibration_evidence_ids": [calibration_id]}
    if higher_task:
        profile["task_id"] = higher_task
    return controller.decide("campaign", {"race_id": race["id"], "action": "accept_preflight", "evidence_trial_ids": ["check"],
        "profile": profile, "rationale": "Complete actual mask and throughput evidence"})


def test_numerical_gate_and_new_spending_preserve_old_checkpoint_cost(tmp_path):
    workspace, controller, race = setup_race(tmp_path)
    controller.tick()
    assert not workspace.created and not workspace.controls
    assert controller.view(race["id"])["worker_seconds_spent"] == 0
    with pytest.raises(ValueError, match="every declared preflight"):
        controller.decide("campaign", {"race_id": race["id"], "action": "accept_preflight", "evidence_trial_ids": [],
            "profile": {"max_workers": 2}, "rationale": "Insufficient evidence"})
    view = pass_preflight(workspace, controller, race)
    assert view["preflight"]["status"] == "passed"
    assert view["worker_seconds_spent"] == 17
    controller.tick()
    assert ("old", "extend") in workspace.controls
    assert workspace.store.get("old", "trial")["wall_seconds"] == 900
    # A service restart reads the same durable cells without another extension.
    resumed = AdaptiveRacing(workspace)
    resumed.tick()
    assert workspace.controls.count(("old", "extend")) == 1
    assert resumed.view(race["id"])["worker_seconds_spent"] == 17
    assert not workspace.store.list("execution_grant")


def test_fidelity_change_requires_new_common_task_and_discards_checkpoint(tmp_path):
    workspace, controller, race = setup_race(tmp_path)
    with pytest.raises(ValueError, match="new common higher-fidelity"):
        pass_preflight(workspace, controller, race, changed=True)
    task = workspace.store.get("task", "task")
    task.update(id="higher", problem={**task["problem"], "fidelity": {"rcwa_order_x": 14, "rcwa_order_y": 7}})
    workspace.store.put("task", task)
    view = pass_preflight(workspace, controller, race, changed=True, higher_task="higher")
    record = workspace.store.get(race["id"], "adaptive_race")
    assert record["task_id"] == "higher"
    assert all(cell["trial_id"] is None for cell in record["cells"] if cell["phase"] == "development")
    assert len(workspace.store.list("race_fidelity_amendment")) == 1
    assert view["preflight"]["status"] == "passed"


def test_endpoint_uses_only_observed_prefix_and_failed_costs_are_censored(tmp_path):
    workspace, controller, view = setup_race(tmp_path)
    race = workspace.store.get(view["id"], "adaptive_race")
    cell = race["cells"][0]
    journal = workspace.job_dir("old") / "metrics.jsonl"
    journal.write_text("\n".join(json.dumps(row) for row in [
        {"elapsed_seconds": 898, "best_objective": .31, "confirmed_observations": 20},
        {"elapsed_seconds": 905, "best_objective": .95, "confirmed_observations": 21}]) + "\n")
    (workspace.job_dir("old") / "observations.jsonl").write_text(json.dumps({"id": "observed", "status": "ok", "candidate": [1, 0],
        "objectives": {"mean_plus1_transmission": .31, "te_plus1_transmission": .42, "tm_plus1_transmission": .20,
            "min_plus1_transmission": .20}}) + "\n")
    endpoint = controller._endpoint(race, cell, {"status": "budget_exhausted", "elapsed_seconds": 906, "diagnostics": {}})
    assert endpoint["eligible"] and endpoint["score"] == .31
    assert endpoint["observed_seconds"] == 898
    assert endpoint["objectives"]["te_plus1_transmission"] == .42
    assert endpoint["observation_id"] == "observed" and "candidate" not in endpoint
    other = {**cell, "id": "different", "rung_seconds": 1800}
    incomplete = controller._endpoint(race, other, {"status": "stopped", "elapsed_seconds": 906, "diagnostics": {}})
    assert not incomplete["eligible"] and incomplete["censored"]


def test_activity_gates_protect_slow_learners_and_require_actual_progress():
    assert not activity_gate("flrl_ppo", {"episode_length": 128}, {"training_updates": 20, "decisions": 100})["eligible"]
    assert activity_gate("flrl_ppo", {"episode_length": 128}, {"training_updates": 10, "decisions": 128})["eligible"]
    assert not activity_gate("flrl_autograd_adam", {"reference_epochs": 80}, {"gradient_evaluations": 70, "decisions": 70})["eligible"]
    assert activity_gate("flrl_lsf_es", {"population_size": 8}, {"evaluations": 168})["eligible"]
    assert not activity_gate("nested_fourier", {}, {"evaluations": 100, "stage": 0})["eligible"]
    assert activity_gate("nested_fourier", {}, {"evaluations": 100, "stage": 1})["eligible"]


def endpoint(key, seed, rung, score, mature=True):
    return {"configuration_id": key, "seed": seed, "rung_seconds": rung, "score": score, "eligible": True,
        "maturity": {"eligible": mature, "reasons": [] if mature else ["Undertrained"]}}


def test_crossed_learning_curves_and_mature_plateau_are_treated_differently():
    configurations = [{"id": key} for key in ("leader", "second", "third", "slow", "plateau")]
    rows = []
    for seed in (17, 41, 73):
        for key, before, after in (("leader", .3, .4), ("second", .3, .38), ("third", .3, .37), ("slow", .1, .2), ("plateau", .1, .101)):
            rows += [endpoint(key, seed, 900, before, key != "slow"), endpoint(key, seed, 1800, after, key != "slow")]
    decision = adaptive_assessment(configurations, rows, rung=1800, previous_rung=900)
    assert decision["slow"]["action"] == "extend"
    assert decision["plateau"]["action"] == "deprioritize"
    assert decision["leader"]["action"] == "extend"


def test_one_seed_outlier_triggers_replication_instead_of_peak_ranking():
    configurations = [{"id": key} for key in ("steady", "outlier", "other")]
    rows = [endpoint("steady", s, 1800, .35) for s in (17, 41, 73)]
    rows += [endpoint("outlier", s, 1800, score) for s, score in zip((17, 41, 73), (.8, .1, .1))]
    rows += [endpoint("other", s, 1800, .3) for s in (17, 41, 73)]
    decision = adaptive_assessment(configurations, rows, rung=1800, previous_rung=900)
    assert decision["outlier"]["action"] == "replicate"


def test_followup_reserves_exploration_and_selects_only_equal_horizon_finalists(tmp_path, monkeypatch):
    workspace, controller, view = setup_race(tmp_path)
    race = workspace.store.get(view["id"], "adaptive_race")
    race.update(status="running", stage="development")
    race["preflight"]["status"] = "passed"
    for index, cell in enumerate(race["cells"]):
        trial_id = "develop_" + str(index)
        cell.update(trial_id=trial_id, rung_seconds=1800, status="finished")
        workspace.store.put("trial", {"id": trial_id, "campaign_id": "campaign", "task_id": "task", "status": "budget_exhausted",
            "algorithm": "motif_surgery", "seed": cell["seed"], "wall_seconds": 1800, "execution_seconds": 1801, "progress": {}})
        score = {"motif": .4, "ppo": .3, "random": .1}[cell["configuration_id"]]
        for rung in (900, 1800):
            row = endpoint(cell["configuration_id"], cell["seed"], rung, score)
            workspace.store.put_immutable("race_endpoint", {**row, "id": f"develop_endpoint_{index}_{rung}",
                "race_id": race["id"], "campaign_id": "campaign", "phase": "development"})
    controller._adaptive(race)
    assert race["stage"] == "adaptive_followup"
    pending = [cell for cell in race["cells"] if cell["status"] == "pending"]
    assert len(pending) == 9
    protected = sum(3600 - cell.get("exploration_start_seconds", 1801) for cell in pending if cell.get("exploration"))
    assert protected / (1799 * len(pending)) >= .2
    for index, cell in enumerate(pending):
        cell["status"] = "finished"
        row = endpoint(cell["configuration_id"], cell["seed"], 3600, {"motif": .5, "ppo": .4, "random": .2}[cell["configuration_id"]])
        workspace.store.put_immutable("race_endpoint", {**row, "id": f"full_endpoint_{index}", "race_id": race["id"],
            "campaign_id": "campaign", "phase": "development"})
    selected = []
    monkeypatch.setattr(controller, "_freeze_confirmation", lambda race, finalists: selected.extend(finalists))
    controller._adaptive(race)
    assert selected == ["motif", "ppo"]


def test_resource_evidence_requires_registered_completed_gradient_and_forward_work(tmp_path):
    workspace, controller, view = setup_race(tmp_path)
    with pytest.raises(ValueError, match="memory samples"):
        controller.decide("campaign", {"race_id": view["id"], "action": "record_calibration", "evidence_trial_ids": [],
            "profile": {"samples": [{"available_memory_bytes": 1024}] * 3}, "rationale": "Unsafe profile cannot be selected"})
    workspace.store.put("trial", {"id": "probe", "campaign_id": "campaign", "task_id": "task", "algorithm": "motif_surgery",
        "algorithm_config": {}, "seed": 501, "race_id": view["id"], "race_phase": "calibration", "numerical_threads": 1,
        "status": "queued", "wall_seconds": 90, "progress": {}, "execution_seconds": 0.})
    controller.decide("campaign", {"race_id": view["id"], "action": "register_calibration", "evidence_trial_ids": ["probe"],
        "profile": {"max_workers": 1, "numerical_threads": 1}, "rationale": "Actual forward workload probe"})
    with pytest.raises(ValueError, match="actual numerical work"):
        controller.decide("campaign", {"race_id": view["id"], "action": "record_calibration", "evidence_trial_ids": ["probe"],
            "profile": {"samples": [{"available_memory_bytes": 8 * 1024**3}] * 3}, "rationale": "Queued jobs are not numerical evidence"})


def final_validation_fixture(tmp_path, search_order=22):
    workspace, controller, view = setup_race(tmp_path)
    race = workspace.store.get(view["id"], "adaptive_race")
    task = workspace.store.get("task", "task")
    task["problem"]["fidelity"] = {"rcwa_order_x": search_order, "rcwa_order_y": search_order // 2}
    workspace.store.put("task", task)
    race.update(status="running", stage="final_validation")
    race["preflight"] = {"status": "passed", "evidence_trial_ids": ["check"], "highest_fidelities": [task["problem"]["fidelity"]]}
    race["profile"]["validation_memory_samples"] = [{"phase": "validation", "trial_id": "check", "completed_evaluations": 4,
        "fidelity": task["problem"]["fidelity"], "peak_rss_bytes": 3 * 1024**3}]
    race["confirmation"] = {"status": "numerical_validation_pending"}
    endpoint = {"id": "confirmation_endpoint", "eligible": True, "trial_id": "old", "configuration_id": "motif",
        "seed": 17, "score": .31, "phase": "confirmation"}
    return workspace, controller, race, endpoint


def test_final_validation_follows_amended_search_fidelity_and_actual_memory(tmp_path):
    workspace, controller, race, endpoint = final_validation_fixture(tmp_path)
    plan = controller._final_validation_plan(race)
    assert plan["search_fidelity"] == {"rcwa_order_x": 22, "rcwa_order_y": 11}
    assert plan["parameters"]["fidelities"] == [{"rcwa_order_x": 22, "rcwa_order_y": 11}, {"rcwa_order_x": 26, "rcwa_order_y": 13}]
    assert plan["predicted_peak_bytes"] > 6 * 1024**3
    assert plan["measured_samples"] == 1
    assert "measured" in plan["prediction_basis"]
    assert race["numerical_parameters"] == plan["parameters"]
    # The validation amendment cannot silently alter the source scientific protocol.
    assert controller._protocol(race)["numerical_parameters"] == {}
    assert controller._final_validation_plan(race) == plan


def test_higher_order_memory_denial_holds_then_releases_exact_plan(tmp_path, monkeypatch):
    workspace, controller, race, endpoint = final_validation_fixture(tmp_path)
    compiled = []
    monkeypatch.setattr("optimization_framework.execution.racing.memory_available_bytes", lambda: 6 * 1024**3)
    monkeypatch.setattr(controller, "_validation_candidate", lambda trial_id, score: [1, 0])
    workspace.compile_recipe = lambda parent, recipe_id, parameters, candidates: (compiled.append(parameters) or {"cases": [{}]})
    workspace._queue_recipe = lambda parent, recipe, request, **kwargs: workspace.create_trial(request)
    controller._queue_final_validation(race, [endpoint])
    assert race["confirmation"]["status"] == "numerical_validation_held"
    assert compiled == [] and workspace.created == []
    assert race["status"] == "running"
    monkeypatch.setattr("optimization_framework.execution.racing.memory_available_bytes", lambda: 40 * 1024**3)
    controller._queue_final_validation(race, [endpoint])
    assert race["confirmation"]["status"] == "numerical_validation_pending"
    assert len(workspace.created) == 1
    assert compiled[0]["fidelities"][-1] == {"rcwa_order_x": 26, "rcwa_order_y": 13}
    trial = workspace.store.get(workspace.created[0]["id"], "trial")
    assert trial["race_phase"] == "validation" and trial["race_validation_peak_bytes"] > 6 * 1024**3
    # A retry cannot duplicate a completed or queued fixed-endpoint validation cell.
    controller._queue_final_validation(race, [endpoint])
    assert len(workspace.created) == 1


def test_final_validation_cannot_pass_using_only_lower_orders_at_fidelity_limit(tmp_path, monkeypatch):
    workspace, controller, race, endpoint = final_validation_fixture(tmp_path, search_order=34)
    plan = controller._final_validation_plan(race)
    assert plan["status"] == "higher_fidelity_unavailable"
    assert plan["parameters"]["fidelities"] == []
    controller._queue_final_validation(race, [endpoint])
    assert workspace.created == []
    assert race["confirmation"]["status"] == "numerically_inconclusive"
    assert "unassessed" in race["reason"]


def test_original_search_preserves_the_reviewed_higher_order_ladder(tmp_path):
    workspace, controller, view = setup_race(tmp_path)
    race = workspace.store.get(view["id"], "adaptive_race")
    race["preflight"] = {"status": "passed", "evidence_trial_ids": ["check"],
        "highest_fidelities": [{"rcwa_order_x": 14, "rcwa_order_y": 7}]}
    plan = controller._final_validation_plan(race)
    assert [(item["rcwa_order_x"], item["rcwa_order_y"]) for item in plan["parameters"]["fidelities"]] == [(10, 5), (12, 6), (14, 7)]


def test_confirmation_statistics_are_blocked_adjusted_and_need_frozen_roster():
    scores = {"baseline": [.2] * 10, "first": [.31] * 10, "second": [.205] * 10}
    result = confirmation_statistics(scores, "baseline")
    assert result["bootstrap_resamples"] == 20000
    assert result["confirmed_promising"] == ["first"]
    assert len(result["comparisons"]) == 3
    assert confirmation_statistics({"baseline": [.2] * 9, "first": [.3] * 10}, "baseline")["status"] == "inconclusive"


def test_deadline_halt_is_recorded_as_the_deadline_not_the_researcher(tmp_path, monkeypatch):
    workspace, controller, race = setup_race(tmp_path)
    pass_preflight(workspace, controller, race)
    controller.tick()
    deadline = workspace.store.get(race["id"], "adaptive_race")["deadline_at"]
    monkeypatch.setattr("optimization_framework.execution.racing.time.time", lambda: deadline + 1)
    controller.tick()
    halted = [workspace.store.get(trial_id, "trial") for trial_id, action in workspace.controls if action == "stop"]
    assert halted and {trial["stopped_by"] for trial in halted} == {"deadline"}


def test_global_deadline_survives_restart_and_confirmation_cannot_be_extended(tmp_path, monkeypatch):
    workspace, controller, view = setup_race(tmp_path)
    race = workspace.store.get(view["id"], "adaptive_race")
    monkeypatch.setattr("optimization_framework.execution.racing.time.time", lambda: race["deadline_at"] + 1)
    AdaptiveRacing(workspace).tick()
    assert controller.view(race["id"])["status"] == "budget_exhausted"
    with pytest.raises(ValueError, match="ended"):
        controller.control("campaign", {"race_id": race["id"], "action": "resume"})
    with pytest.raises(ValueError, match="immutable"):
        controller.guard_control({"race_phase": "confirmation", "wall_seconds": 3600, "max_steps": 100000},
            ControlInput(action="extend", wall_seconds=4000))


def test_protocol_rejects_seed_leakage_and_invalid_resource_envelope():
    base = {"task_id": "task", "configurations": [{"id": key, "algorithm": "random"} for key in ("a", "b", "baseline")],
        "baseline_configuration_id": "baseline", "preflight_subject_trial_ids": ["observed"]}
    with pytest.raises(ValueError, match="distinct valid seeds"):
        RaceCreateInput(**base, confirmation_seeds=[17, *range(1002, 1011)])
    with pytest.raises(ValueError, match="worker capacity"):
        RaceCreateInput(**base, worker_seconds=230401)


def test_native_confirmation_freezes_complete_roster_and_threaded_prototypes(tmp_path):
    """Exercise real source pinning and cell expansion without numerical work."""
    from optimization_framework.execution.service import Workspace
    workspace = Workspace(tmp_path)
    import math
    configuration = {"wavelength_nm": 1050, "target_angle_deg": 75, "period_x_nm": 1050 / math.sin(math.radians(75)),
        "period_y_nm": 525, "thickness_nm": 325, "grid_x": 32, "grid_y": 16, "incident_n": 1.45,
        "exit_n": 1., "incident_angle_deg": 0., "target_order_x": 1, "target_order_y": 0,
        "silicon_index_source": "Fixture", "silicon_n_1050": 3.567390909090909, "reference_meent_version": "0.9.5"}
    campaign = workspace.create_campaign(CampaignInput(name="Native adaptive freeze", compute_budget_seconds=400000,
        validation_reserve_seconds=0, tasks=[TaskInput(name="2D physical design", problem_id="meent_2d_dual_polarization_deflector",
            configuration=configuration, fidelity={"rcwa_order_x": 10, "rcwa_order_y": 5})]))
    task = workspace.current_tasks(campaign["id"])[0]
    prototypes, configurations = {}, []
    for key, algorithm in (("motif", "motif_surgery"), ("es", "flrl_lsf_es"), ("random", "flrl_lsf_random")):
        trials = []
        for seed in (17, 41, 73):
            trial = workspace.create_trial(TrialInput(campaign_id=campaign["id"], task_id=task["id"], algorithm=algorithm,
                seed=seed, numerical_threads=2, wall_seconds=3600, max_steps=100000, schedule_steps=128))
            trial.update(status="budget_exhausted", execution_seconds=3601, progress={"elapsed_seconds": 3601}, attempt=1)
            workspace.store.put("trial", trial)
            trials.append(trial)
        prototypes[key] = trials
        configurations.append({"id": key, "algorithm": algorithm, "algorithm_config": trials[0]["algorithm_config"]})
    controller = AdaptiveRacing(workspace)
    view = controller.create(campaign["id"], {"task_id": task["id"], "configurations": configurations,
        "baseline_configuration_id": "random", "preflight_subject_trial_ids": [prototypes["motif"][0]["id"]]})
    race = workspace.store.get(view["id"], "adaptive_race")
    race["preflight"]["status"] = "passed"
    race["profile"].update(threads=2)
    for cell in race["cells"]:
        trial = next(row for row in prototypes[cell["configuration_id"]] if row["seed"] == cell["seed"])
        cell.update(trial_id=trial["id"], status="finished", rung_seconds=3600)
        workspace.store.put_immutable("race_endpoint", {"id": "endpoint_" + cell["id"], "campaign_id": campaign["id"],
            "race_id": race["id"], "configuration_id": cell["configuration_id"], "seed": cell["seed"], "rung_seconds": 3600,
            "eligible": True, "phase": "development", "score": .3, "maturity": {"eligible": True, "reasons": []}})
    controller._freeze_confirmation(race, ["motif", "es"])
    assert race["stage"] == "confirmation"
    cells = [cell for cell in race["cells"] if cell["phase"] == "confirmation"]
    assert len(cells) == 30
    assert {cell["seed"] for cell in cells} == set(range(1001, 1011))
    frozen = workspace.store.get(race["confirmation"]["protocol_id"], "confirmation_protocol")
    assert len(frozen["methods"]) == 3
    assert all(method["numerical_threads"] == 2 and method["wall_seconds"] == 3600 for method in frozen["methods"].values())
    for cell in cells:
        trial = workspace.store.get(cell["trial_id"], "trial")
        assert trial["numerical_threads"] == 2
        assert trial["experiment_spec"]["schedule"]["numerical_threads"] == 2
        assert trial["race_id"] == race["id"] and trial["confirmation_protocol_hash"]
    scheduled = workspace.confirmations.schedule(workspace, frozen["id"])
    assert scheduled["created_trial_ids"] == [] and len(scheduled["existing_trial_ids"]) == 30
