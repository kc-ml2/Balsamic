"""Parameter contracts reject invalid procedures before reserving execution."""
import pytest

from optimization_framework.contracts.requests import CampaignInput, TaskInput, TrialInput
from optimization_framework.execution.service import Workspace


@pytest.mark.parametrize("algorithm,configuration,training", [
    ("hillclimb", {"restart_patience": 2.5}, {}),
    ("annealing", {"temperature": .01, "min_temperature": .1}, {}),
    ("random", {"silently_ignored_option": True}, {}),
    ("dqn", {"gamma": float("inf")}, {}),
    ("dqn", {}, {"horizon": 3.5}),
    ("dqn", {"device": "cuda"}, {}),
])
def test_invalid_parameters_never_allocate_a_worker(tmp_path, algorithm, configuration, training):
    workspace = Workspace(tmp_path)
    campaign = workspace.create_campaign(CampaignInput(name="Parameter contract", tasks=[TaskInput(name="Grating", physics={"n_cells": 4, "fourier_order": 1})]))
    task = workspace.current_tasks(campaign["id"])[0]
    with pytest.raises(ValueError):
        workspace.create_trial(TrialInput(campaign_id=campaign["id"], task_id=task["id"], algorithm=algorithm,
            algorithm_config=configuration, training=training))
    assert workspace.store.list("trial") == []
    assert workspace.allocated_seconds(campaign["id"]) == 0


def test_manifest_describes_parameters_and_supported_lifecycle():
    from optimization_framework.optimizers.registry import methods
    coordinate = next(item for item in methods() if item["id"] == "coordinate")
    assert coordinate["parameter_schema"]["properties"]["radius"]["maximum"] == 1
    assert coordinate["contract"] == "optimizer_v1"
    assert coordinate["batch_size"] == 1
