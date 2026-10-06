"""Explicit authorization for a durable, time-bounded adaptive study."""
from typing import Any, Literal

from pydantic import Field, model_validator

from .base import Contract


class RaceConfiguration(Contract):
    id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    algorithm: str = Field(min_length=1, max_length=100)
    algorithm_config: dict[str, Any] = Field(default_factory=dict)
    hypothesis_id: str | None = None
    pilot_trial_ids: list[str] = Field(default_factory=list, max_length=20)


class RaceCreateInput(Contract):
    task_id: str
    configurations: list[RaceConfiguration] = Field(min_length=3, max_length=16)
    baseline_configuration_id: str
    preflight_subject_trial_ids: list[str] = Field(min_length=1, max_length=40)
    preflight_source_asset_ids: list[str] = Field(default_factory=list, max_length=40)
    development_seeds: list[int] = Field(default_factory=lambda: [17, 41, 73])
    additional_development_seeds: list[int] = Field(default_factory=lambda: [101, 131, 173, 211, 257])
    confirmation_seeds: list[int] = Field(default_factory=lambda: list(range(1001, 1011)))
    rungs_seconds: list[int] = Field(default_factory=lambda: [900, 1800, 3600])
    batch_seconds: float = Field(default=14400, gt=0, le=86400)
    total_seconds: float = Field(default=57600, gt=0, le=604800)
    max_workers: int = Field(default=4, ge=1, le=4)
    worker_seconds: float = Field(default=230400, gt=0, le=2419200)
    exploration_fraction: float = Field(default=.2, ge=.2, le=.5)
    practical_margin: float = Field(default=.02, gt=0, le=.1)
    plateau_margin: float = Field(default=.01, gt=0, le=.1)
    max_steps: int = Field(default=100000, ge=1, le=10000000)
    validation_wall_seconds: float = Field(default=900, gt=0, le=86400)
    numerical_recipe_id: str = "meent_2d_order_convergence:v1"
    numerical_parameters: dict[str, Any] = Field(default_factory=dict)
    rationale: str = Field(default="Execute the authorized adaptive empirical testing protocol", min_length=1, max_length=5000)

    @model_validator(mode="after")
    def consistent_protocol(self):
        ids = [item.id for item in self.configurations]
        if len(ids) != len(set(ids)) or self.baseline_configuration_id not in ids:
            raise ValueError("Declare distinct configuration identities and one roster baseline")
        if self.rungs_seconds != sorted(set(self.rungs_seconds)) or len(self.rungs_seconds) != 3 or min(self.rungs_seconds) <= 0:
            raise ValueError("Declare three strictly increasing cumulative time rungs")
        if self.batch_seconds > self.total_seconds or self.worker_seconds > self.total_seconds * self.max_workers:
            raise ValueError("Execution batches and worker capacity must fit the elapsed envelope")
        groups = (self.development_seeds, self.additional_development_seeds, self.confirmation_seeds)
        if len(self.development_seeds) < 3 or len(self.confirmation_seeds) != 10:
            raise ValueError("At least three development seeds and exactly ten confirmation seeds are required")
        flat = [seed for group in groups for seed in group]
        if len(flat) != len(set(flat)) or any(type(seed) is not int or not 0 <= seed < 2**32 for seed in flat):
            raise ValueError("Development and confirmation seed rosters must be distinct valid seeds")
        if len(set(self.preflight_subject_trial_ids)) != len(self.preflight_subject_trial_ids):
            raise ValueError("Each declared numerical preflight subject is distinct")
        return self


class RaceControlInput(Contract):
    race_id: str
    action: Literal["pause", "resume", "stop"]
    expected_revision: int | None = Field(default=None, ge=1)


class RaceDecisionInput(Contract):
    race_id: str
    action: Literal["register_preflight", "register_calibration", "record_calibration", "accept_preflight", "extend", "replicate", "hold", "freeze_confirmation"]
    configuration_ids: list[str] = Field(default_factory=list, max_length=16)
    evidence_trial_ids: list[str] = Field(default_factory=list, max_length=100)
    profile: dict[str, Any] = Field(default_factory=dict)
    rationale: str = Field(min_length=1, max_length=5000)
