"""Scientific records are immutable; attempts and resource grants are separate."""
from __future__ import annotations

from typing import Any, Literal
from pydantic import Field

from .base import Contract
from .problems import ProblemInstance

# Trial stop causes ("stopped_by") that are a deliberate choice, not a budget,
# deadline or host failure. "manager" is the campaign's lead agent.
DELIBERATE_STOPS = frozenset({"researcher", "manager"})


class ArtifactReference(Contract):
    id: str
    sha256: str
    bytes: int = Field(ge=0)
    media_type: str = "application/octet-stream"
    availability: Literal["local", "external", "unavailable"] = "local"


class ImplementationVersion(Contract):
    id: str
    name: str
    source_digest: str
    runtime_digest: str
    contract_version: int = 1
    representations: list[str]
    required_capabilities: list[str] = Field(default_factory=list)
    supports_constraints: bool = False
    supports_failure_observations: bool = False
    supports_batches: bool = False
    parameter_schema: dict[str, Any] = Field(default_factory=dict)
    validation_ids: list[str] = Field(default_factory=list)
    status: Literal["ready", "revoked", "unavailable"] = "ready"


class StudySpec(Contract):
    id: str
    campaign_id: str
    parent_study_id: str | None = None
    goal: str
    scope: Literal["exploratory", "confirmation", "historical"] = "exploratory"
    instance_ids: list[str]
    assumptions: list[str] = Field(default_factory=list)
    method_roster: list[str] = Field(default_factory=list)
    comparison: dict[str, Any] = Field(default_factory=lambda: {"cost_axis": "full_worker_seconds"})
    selection: dict[str, Any] = Field(default_factory=dict)
    confirmation: dict[str, Any] = Field(default_factory=dict)
    validation_policy: dict[str, Any] = Field(default_factory=dict)
    allowed_choices: list[str] = Field(default_factory=lambda: ["develop_method", "run_experiment"])
    created_at: str
    authority: str


class RequirementStudySpec(StudySpec):
    """A frozen exploratory scope can commission a declared missing evaluator."""
    schema_version: Literal[2] = 2
    problem_requirement_ids: list[str] = Field(min_length=1)


def study_for_tasks(tasks, **values):
    requirements = [task["evaluator_requirement_id"] for task in tasks if task.get("evaluator_requirement_id")]
    instances = [task["problem_instance_id"] for task in tasks if task.get("problem_instance_id")]
    if requirements:
        return RequirementStudySpec(instance_ids=instances, problem_requirement_ids=requirements, **values)
    return StudySpec(instance_ids=instances, **values)


def task_in_study(task, study):
    return bool(task.get("problem_instance_id") in study["instance_ids"] or
                task.get("evaluator_requirement_id") in study.get("problem_requirement_ids", []))


class RecoveryPolicy(Contract):
    mode: Literal["latest_checkpoint"] = "latest_checkpoint"
    every_observations: int = Field(default=100, ge=1)
    every_seconds: float = Field(default=30, gt=0)
    chunk_bytes: int = Field(default=8 * 1024 * 1024, ge=1024, le=64 * 1024 * 1024)
    max_checkpoint_bytes: int = Field(default=4 * 1024**3, ge=1024)


class CompletionCondition(Contract):
    unit: Literal["evaluation_requests", "optimizer_decisions"] = "evaluation_requests"
    count: int = Field(ge=1)


class ExperimentSpec(Contract):
    id: str
    campaign_id: str
    study_id: str
    problem: ProblemInstance
    implementation: ImplementationVersion
    parameters: dict[str, Any] = Field(default_factory=dict)
    seed: int = Field(ge=0, le=2**32 - 1)
    initial_assets: list[str] = Field(default_factory=list)
    contribution_asset_ids: list[str] = Field(default_factory=list)
    asset_digests: dict[str, str] = Field(default_factory=dict)
    dependencies: list[str] = Field(default_factory=list)
    schedule: dict[str, Any] = Field(default_factory=dict)
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    recovery: RecoveryPolicy = Field(default_factory=RecoveryPolicy)
    completion: CompletionCondition
    initial_wall_seconds: float = Field(gt=0)
    extension_policy: Literal["forbidden", "explicit_amendment"] = "explicit_amendment"
    created_at: str


class ExecutionAttempt(Contract):
    id: str
    experiment_id: str
    revision: int = 1
    status: Literal["queued", "running", "paused", "stopped", "completed", "interrupted", "failed", "budget_exhausted"]
    worker_identity: dict[str, Any] = Field(default_factory=dict)
    checkpoint_id: str | None = None
    process_exit: int | None = None
    allocation_stop: str | None = None
    enforcement_id: str | None = None
    scientific_complete: bool = False
    actual_costs: dict[str, float | None] = Field(default_factory=dict)
    reason: str | None = None


class BudgetAmendment(Contract):
    id: str
    experiment_id: str
    campaign_id: str
    previous_wall_seconds: float
    wall_seconds: float
    previous_count: int
    count: int
    authority: str
    rationale: str
    created_at: str
