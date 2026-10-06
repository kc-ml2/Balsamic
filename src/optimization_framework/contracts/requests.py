"""Validated request contracts; scientific state is immutable per trial."""
from dataclasses import asdict
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from optimization_framework.evaluation.registry import problems
from .problems import ProblemInstance
from .evaluators import EvaluatorManifest
from .diagnostics import DiagnosticSchedule
from .experiments import RecoveryPolicy
from .study_rules import RuleRequest


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class TaskInput(Model):
    id: str | None = None
    name: str = Field(min_length=1, max_length=200)
    physics: dict[str, Any] = Field(default_factory=dict)
    problem_id: str = "meent_grating"
    configuration: dict[str, Any] = Field(default_factory=dict)
    fidelity: dict[str, Any] = Field(default_factory=dict)
    problem: ProblemInstance | None = None
    evaluator_manifest: EvaluatorManifest | None = None
    split: Literal["development", "selection", "test"] = "development"

    @model_validator(mode="after")
    def resolve_problem(self):
        if self.evaluator_manifest is not None:
            if self.physics or self.problem_id != self.evaluator_manifest.id:
                raise ValueError("A commissioned problem must match its declared manifest and use configuration")
            if self.problem_id in problems.ids():
                raise ValueError("A generated evaluator cannot replace an installed problem adapter")
            resolved = self.evaluator_manifest.resolve("unresolved", self.configuration, self.fidelity)
            if self.problem is not None and self.problem != resolved:
                raise ValueError("An unresolved evaluator requirement cannot supply a runnable problem")
            self.problem, self.configuration, self.fidelity = resolved, resolved.configuration, resolved.fidelity
            return self
        if self.physics and self.configuration:
            legacy = problems.resolve(self.problem_id, self.physics, self.fidelity)
            explicit = problems.resolve(self.problem_id, self.configuration, self.fidelity)
            if legacy != explicit:
                raise ValueError("Specify problem configuration once")
        resolved = problems.resolve(self.problem_id, self.configuration or self.physics, self.fidelity)
        if self.problem is not None and self.problem != resolved:
            raise ValueError("Supplied resolved problem does not match the registered adapter")
        self.problem = resolved
        self.configuration = resolved.configuration
        self.fidelity = resolved.fidelity
        # Legacy form projection; scientific identity is the problem record.
        self.physics = {**resolved.configuration, **resolved.fidelity} if resolved.candidate_schema.representation == "binary" else {}
        return self


class CampaignInput(Model):
    name: str = Field(min_length=1, max_length=200)
    objective: str = Field(default="Develop an effective optimizer for the chosen problem.", max_length=20000)
    compute_budget_seconds: float = Field(default=3600, gt=0)
    llm_budget_usd: float = Field(default=5, ge=0, le=10000)
    autonomy: Literal["manual", "guided", "delegated"] = "guided"
    tasks: list[TaskInput] = Field(min_length=1, max_length=100)
    delegated_trial_seconds: float = Field(default=60, gt=0, le=3600)
    validation_reserve_seconds: float = Field(default=120, ge=0)
    implementation_compute_budget_seconds: float = Field(default=0, ge=0, le=604800)

    @model_validator(mode="after")
    def reserve_fits(self):
        if self.validation_reserve_seconds > self.compute_budget_seconds:
            raise ValueError("Validation reserve exceeds campaign compute budget")
        return self


class CampaignUpdate(Model):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    objective: str | None = Field(default=None, max_length=20000)
    compute_budget_seconds: float | None = Field(default=None, gt=0)
    llm_budget_usd: float | None = Field(default=None, ge=0, le=10000)
    autonomy: Literal["manual", "guided", "delegated"] | None = None
    tasks: list[TaskInput] | None = Field(default=None, min_length=1, max_length=100)
    delegated_trial_seconds: float | None = Field(default=None, gt=0, le=3600)
    validation_reserve_seconds: float | None = Field(default=None, ge=0)
    implementation_compute_budget_seconds: float | None = Field(default=None, ge=0, le=604800)


class TrialInput(Model):
    campaign_id: str
    task_id: str
    algorithm: str = Field(default="", max_length=100)
    implementation_version_id: str | None = None
    algorithm_config: dict[str, Any] = Field(default_factory=dict)
    seed: int = Field(default=0, ge=0, le=2**32 - 1)
    max_steps: int = Field(default=512, ge=1, le=10000000)
    wall_seconds: float = Field(default=60, gt=0, le=86400)
    numerical_threads: int = Field(default=1, ge=1, le=4, strict=True)
    race_id: str | None = None
    race_phase: Literal["preflight", "calibration", "development", "confirmation", "validation"] | None = None
    schedule_steps: int | None = Field(default=None, ge=1, le=10000000)
    hypothesis_id: str | None = None
    question: str = Field(default="Compare search progress under a bounded budget.", max_length=10000,
                          validation_alias=AliasChoices("question", "experiment_question"))
    priority: int = Field(default=0, ge=-100, le=100)
    confirmatory: bool = False
    confirmation_protocol_id: str | None = None
    training: dict[str, Any] = Field(default_factory=dict)
    completion: dict[str, Any] | None = None
    initial_assets: list[str] = Field(default_factory=list)
    reuse_decision_ids: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    diagnostics: list[DiagnosticSchedule] = Field(default_factory=list, max_length=20)
    recovery: RecoveryPolicy = Field(default_factory=RecoveryPolicy)


class ControlInput(Model):
    action: Literal["pause", "resume", "stop", "extend", "prioritize"]
    max_steps: int | None = Field(default=None, ge=1, le=10000000)
    wall_seconds: float | None = Field(default=None, gt=0, le=86400)
    priority: int | None = Field(default=None, ge=-100, le=100)
    rationale: str = Field(default="Researcher changed the resource allocation", max_length=5000)


class ValidationInput(Model):
    orders: list[int] = Field(default_factory=lambda: [25, 40, 60, 80], min_length=2, max_length=12)
    max_designs: int = Field(default=3, ge=1, le=10)
    wall_seconds: float = Field(default=120, gt=0, le=86400)
    tolerance: float = Field(default=0.005, gt=0, le=0.1)

    @field_validator("orders")
    @classmethod
    def distinct_orders(cls, orders):
        if min(orders) < 1 or max(orders) > 480 or len(set(orders)) < 2:
            raise ValueError("Provide at least two distinct Fourier orders from 1 through 480")
        return sorted(set(orders))


class RecipeInput(Model):
    recipe_id: str = Field(min_length=1, max_length=100)
    parameters: dict[str, Any] = Field(default_factory=dict)
    subject_limit: int = Field(default=1, ge=1, le=10)
    wall_seconds: float = Field(default=120, gt=0, le=86400)


class PrototypeAllocationInput(Model):
    """Explicit final-run allocation, pinned to the reviewed source prototype."""
    expected_control_revision: int = Field(ge=0, strict=True)
    max_steps: int | None = Field(default=None, ge=1, le=10000000, strict=True)
    wall_seconds: float | None = Field(default=None, gt=0, le=86400, strict=True)
    schedule_steps: int | None = Field(default=None, ge=1, le=10000000, strict=True)
    completion_count: int | None = Field(default=None, ge=1, strict=True)


class StudyInput(Model):
    goal: str = Field(min_length=1, max_length=20000)
    scope: Literal["exploratory", "confirmation"] = "exploratory"
    task_ids: list[str] = Field(default_factory=list)
    prototype_trial_ids: list[str] = Field(default_factory=list)
    prototype_allocations: dict[str, PrototypeAllocationInput] = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=list)
    validation_policy: dict[str, Any] = Field(default_factory=dict)
    comparison: dict[str, Any] = Field(default_factory=lambda: {"cost_axis": "worker_seconds", "cost_view": "full_attributed_cost"})
    confirmation_kind: Literal["seed_replication", "unseen_instance", "policy_transfer"] | None = None
    seeds: list[int] = Field(default_factory=list)
    selection_rule: str = "Use every method/instance/seed cell in the frozen roster"
    selection: RuleRequest | None = None
    analysis: RuleRequest | None = None
    nomination_id: str | None = None
    finalist_selection_id: str | None = None
    finalist_selection_revision: int | None = Field(default=None, ge=1)
    reference_trial_ids: list[str] = Field(default_factory=list)
    policy_asset_id: str | None = None
    adaptation: Literal["forbidden", "budgeted"] = "forbidden"
    adaptation_procedure: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def finalist_selection_pin(self):
        if (self.finalist_selection_id is None) != (self.finalist_selection_revision is None):
            raise ValueError("Pin both the finalist selection identity and revision")
        if self.finalist_selection_id is not None and self.scope != "confirmation":
            raise ValueError("Finalist prototypes are imported into a confirmation study")
        if self.prototype_allocations and self.scope != "confirmation":
            raise ValueError("Per-method final allocations belong to a confirmation study")
        return self


class HypothesisInput(Model):
    campaign_id: str
    title: str = Field(min_length=1, max_length=300)
    mechanism: str = Field(default="", max_length=20000)
    rationale: str = Field(default="", max_length=20000)
    assumptions: list[Any] = Field(default_factory=list, max_length=50)
    risks: list[str] = Field(default_factory=list, max_length=50)
    sources: list[Any] = Field(default_factory=list, max_length=50)
    parent_ids: list[str] = Field(default_factory=list, max_length=10)
    algorithm: str = "hillclimb"
    algorithm_config: dict[str, Any] = Field(default_factory=dict)
    status: Literal["proposed", "investigating", "archived", "finalist"] = "proposed"
    source: str | None = Field(default=None, max_length=100000)
    implementation_version_id: str | None = None


class ReviewInput(Model):
    text: str = Field(min_length=1, max_length=20000)

    @field_validator("text")
    @classmethod
    def nonempty_comment(cls, value):
        if not value.strip():
            raise ValueError("Write a comment before saving")
        return value


class DecisionInput(Model):
    choice: str = Field(min_length=1, max_length=200)
    comment: str = Field(default="", max_length=20000)


class ResearchInput(Model):
    campaign_id: str
    message: str = Field(min_length=1, max_length=20000)
    mode: Literal["discuss", "generate", "review", "compare", "evolve", "probe", "plan"] = "discuss"
    hypothesis_id: str | None = None
    feedback_review_ids: list[str] = Field(default_factory=list, max_length=100)
    proposal_operation: Literal["expand", "diversify", "hybrid"] | None = None
    parent_hypothesis_ids: list[str] = Field(default_factory=list, max_length=2)
    proposal_count: int = Field(default=3, ge=1, le=6)
    max_calls: int = Field(default=6, ge=1, le=20)
    max_output_tokens: int = Field(default=2048, ge=256, le=8192)

    @model_validator(mode="after")
    def proposal_parents(self):
        if self.proposal_operation:
            required = {"expand": 0, "diversify": 1, "hybrid": 2}[self.proposal_operation]
            if self.mode != "generate" or self.hypothesis_id or self.feedback_review_ids:
                raise ValueError("Proposal exploration uses generate mode and explicit parent_hypothesis_ids")
            if len(set(self.parent_hypothesis_ids)) != required or len(self.parent_hypothesis_ids) != required:
                raise ValueError(f"{self.proposal_operation} requires {required} distinct parent proposals")
        elif self.parent_hypothesis_ids:
            raise ValueError("Parent proposals require a proposal operation")
        return self
