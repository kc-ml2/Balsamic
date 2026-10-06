"""Native race-to-confirmation integration; synthetic development evidence only.

No numerical campaign is started. Deliberately synthetic full-horizon endpoints
exercise the real freeze, source binding, scheduler expansion and worker checks.
"""
import json

from optimization_framework.contracts.base import content_hash
from optimization_framework.contracts.requests import CampaignInput, TaskInput, TrialInput
from optimization_framework.evaluation.confirmation import method_definition
from optimization_framework.evaluation.confirmation_allocations import binding_id, source_matches
from optimization_framework.execution.service import Workspace
from optimization_framework.execution.worker import ExperimentWorker, fingerprint


def prepared_race(tmp_path):
    workspace = Workspace(tmp_path, max_workers=4)
    campaign = workspace.create_campaign(CampaignInput(name="Synthetic native confirmation integration",
        compute_budget_seconds=300000, validation_reserve_seconds=0,
        tasks=[TaskInput(name="Cheap quadratic fixture", problem_id="bounded_continuous")]))
    task = workspace.current_tasks(campaign["id"])[0]
    definitions = [("coordinate_small", "coordinate", {"radius": .1}),
                   ("coordinate_large", "coordinate", {"radius": .3}), ("baseline", "random", {})]
    configurations, originals = [], {}
    for key, algorithm, parameters in definitions:
        trial_ids = []
        for seed in (17, 41, 73):
            wall = 1800 if key == "baseline" else 3600
            trial = workspace.create_trial(TrialInput(campaign_id=campaign["id"], task_id=task["id"],
                algorithm=algorithm, algorithm_config=parameters, numerical_threads=2,
                seed=seed, max_steps=100000, schedule_steps=128, wall_seconds=wall))
            # These numbers are fixture inputs, not measured empirical evidence.
            trial.update(status="budget_exhausted", execution_seconds=wall,
                progress={"elapsed_seconds": wall, "best_objective": .4, "checkpoint_available": True},
                result={"elapsed_seconds": wall, "scientific_complete": False})
            workspace.store.put("trial", trial)
            originals[trial["id"]] = workspace.store.get(trial["id"], "trial")
            trial_ids.append(trial["id"])
        configurations.append({"id": key, "algorithm": algorithm, "algorithm_config": parameters,
                               "pilot_trial_ids": trial_ids})
    view = workspace.racing.create(campaign["id"], {
        "task_id": task["id"], "configurations": configurations, "baseline_configuration_id": "baseline",
        "preflight_subject_trial_ids": [configurations[0]["pilot_trial_ids"][0]],
        "rationale": "Test-only synthetic completed development fixture; no algorithm effectiveness claim"})
    race = workspace.store.get(view["id"], "adaptive_race")
    race.update(status="running", preflight={"status": "passed", "test_fixture": True},
                profile={**race["profile"], "threads": 2})
    for cell in race["cells"]:
        horizon = 1800 if cell["configuration_id"] == "baseline" else 3600
        cell.update(status="finished", rung_seconds=horizon)
        if cell["configuration_id"] != "baseline":
            workspace.store.put_immutable("race_endpoint", {
                "id": "synthetic_endpoint_" + cell["id"], "campaign_id": campaign["id"], "race_id": race["id"],
                "configuration_id": cell["configuration_id"], "seed": cell["seed"],
                "rung_seconds": 3600, "eligible": True, "score": .4,
                "evidence_origin": "synthetic unit-test fixture"})
    workspace.store.put("adaptive_race", race)
    return workspace, campaign, race, originals


def test_native_confirmation_freezes_thirty_cells_and_preserves_source_threads(tmp_path):
    workspace, campaign, race, originals = prepared_race(tmp_path)
    try:
        with workspace.lock, workspace.store.transaction():
            workspace.racing._freeze_confirmation(race, ["coordinate_small", "coordinate_large"])
            workspace.store.put("adaptive_race", race)
        confirmation = race["confirmation"]
        protocol = workspace.store.get(confirmation["protocol_id"], "confirmation_protocol")
        binding = workspace.store.get(binding_id(protocol["id"]), "confirmation_allocation_binding")
        assert len(protocol["methods"]) == 3
        assert protocol["seeds"] == list(range(1001, 1011))
        assert set(confirmation["configuration_by_method"].values()) == {"coordinate_small", "coordinate_large", "baseline"}
        assert confirmation["finalists"] == ["coordinate_small", "coordinate_large"]
        cells = [cell for cell in race["cells"] if cell["phase"] == "confirmation"]
        assert len(cells) == 30
        assert {(cell["configuration_id"], cell["seed"]) for cell in cells} == {
            (key, seed) for key in ("coordinate_small", "coordinate_large", "baseline") for seed in range(1001, 1011)}

        baseline_method_id = next(identity for identity, key in confirmation["configuration_by_method"].items() if key == "baseline")
        baseline_source = workspace.store.get(protocol["prototypes"][baseline_method_id], "trial")
        assert baseline_source["wall_seconds"] == 1800
        assert protocol["methods"][baseline_method_id]["wall_seconds"] == 3600
        assert binding["methods"][baseline_method_id]["source_procedure"]["wall_seconds"] == 1800
        assert binding["methods"][baseline_method_id]["allocation_overrides"] == {"wall_seconds": 3600}

        for identity, method in protocol["methods"].items():
            assert content_hash(method) == identity and method["numerical_threads"] == 2
            source = workspace.store.get(protocol["prototypes"][identity], "trial")
            assert source_matches(workspace.store, protocol, identity, source)
            assert source == originals[source["id"]]
        for cell in cells:
            trial = workspace.store.get(cell["trial_id"], "trial")
            method = protocol["methods"][trial["confirmation_method_id"]]
            source = workspace.store.get(trial["source_prototype_id"], "trial")
            assert trial["race_id"] == race["id"] and trial["race_phase"] == "confirmation"
            assert trial["wall_seconds"] == 3600 and trial["numerical_threads"] == 2
            assert trial["experiment_spec"]["extension_policy"] == "forbidden"
            assert trial["experiment_spec"]["schedule"]["numerical_threads"] == 2
            assert trial["source_hash"] == source["source_hash"]
            assert method_definition(trial) == method
            assert trial["experiment_spec_hash"] == content_hash(trial["experiment_spec"])
            saved = json.loads((workspace.job_dir(trial["id"]) / "spec.json").read_text())
            assert saved["numerical_threads"] == 2
            assert fingerprint(saved) != fingerprint({**saved, "numerical_threads": 1})
            # Initialization verifies the captured source, native frozen spec,
            # runtime/thread policy and creates a cheap local initial checkpoint.
            # The worker is deliberately never run and the scheduler never starts.
            worker = ExperimentWorker(workspace.job_dir(trial["id"]))
            assert worker.spec_hash == fingerprint(saved)
            assert worker.spec["confirmation_method_id"] == trial["confirmation_method_id"]
            assert worker.max_steps == 100000 and worker.wall_seconds == 3600
            for component in (worker.optimizer, worker.evaluator):
                if hasattr(component, "close"):
                    component.close()
        replay = workspace.confirmations.schedule(workspace, protocol["id"])
        assert not replay["created_trial_ids"] and len(replay["existing_trial_ids"]) == 30
        assert len(workspace.store.list("confirmation_protocol")) == 1
        assert all(workspace.store.get(identity, "trial") == source for identity, source in originals.items())
    finally:
        workspace.close()
