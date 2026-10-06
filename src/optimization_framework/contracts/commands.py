"""Durable application commands; actor authority is supplied by the caller boundary."""
from typing import Any, Literal
import re
from pydantic import Field, model_validator, field_validator

from .base import Contract
from .requests import CampaignUpdate, ControlInput, ValidationInput, ReviewInput, DecisionInput
from optimization_framework.implementations.models import EvaluatorSpec, EvaluatorPackage, EvaluatorCheckSpec, OptimizerCheckSpec

Operation = Literal["agent.activate", "agent.message", "agent.control", "agent.rollback", "agent.configure", "fixed_mask.run", "campaign.create", "campaign.update", "context.edit", "issue.resolve", "trial.create", "trial.control", "trial.extension_request", "trial.validate", "draft.save", "draft.launch", "reproduction.draft", "reproduction.compare", "study.create", "study.nominate", "finalist.set", "study.freeze_template", "study.activate", "study.race.create", "study.race.control", "study.race.decide", "validation.run", "validation.require", "validation.execute", "validation.waive",
    "models.configure", "discovery.start", "discovery.control", "discovery.amend", "discovery.retry", "discovery.assessment.save", "discovery.assessment.launch", "discovery.assessment.decide", "asset.snapshot",
    "validation.revoke_waiver", "context.import", "inference.run", "asset.reuse", "asset.import_reference_set", "cost.reconcile", "comparison.report", "finding.record", "implementation.commission",
    "implementation.reference", "implementation.bind_builtin", "implementation.attach", "evaluator.commission", "evaluator.attach", "implementation.control", "implementation.revalidate", "implementation.reuse", "implementation.resolve_runtime", "bundle.export", "bundle.inspect", "bundle.publish", "research.start", "research.retry", "research.control", "decision.resolve", "decision.refresh", "source.record", "source.ingest", "hypothesis.create", "hypothesis.review", "hypothesis.status", "hypothesis.nominate", "literature.search", "confirmation.schedule", "confirmation.validate", "confirmation.release"]


class Command(Contract):
    id: str = Field(min_length=1, max_length=160, pattern=r"^[a-zA-Z0-9_-]+$")
    campaign_id: str
    operation: Operation
    expected_revision: int = Field(ge=0)
    expected_guidance_revision: int | None = Field(default=None, ge=0)
    expected_authority_hash: str | None = None
    proposal_digest: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def campaign_precondition(self):
        if self.operation == "campaign.create":
            if self.expected_revision != 0:
                raise ValueError("Campaign creation requires revision zero (the campaign must be absent)")
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,160}", self.campaign_id):
                raise ValueError("Choose a stable campaign identity using letters, digits, hyphens or underscores")
            if self.expected_guidance_revision is not None or self.expected_authority_hash is not None:
                raise ValueError("A new campaign has no existing guidance or delegated authority")
        elif self.expected_revision < 1:
            raise ValueError("This command requires an existing campaign revision")
        return self


class CampaignUpdateInput(CampaignUpdate):
    rationale: str = Field(default="Researcher revised the campaign charter", min_length=1, max_length=5000)


class FinalistSetInput(Contract):
    """A revisable researcher shortlist, separate from a frozen nomination."""
    study_id: str
    trial_ids: list[str] = Field(default_factory=list, max_length=500)
    label: str | None = Field(default=None, max_length=200)
    expected_revision: int | None = Field(default=None, ge=0)
    expected_procedure_ids: dict[str, str] = Field(default_factory=dict)


class TrialControlInput(ControlInput):
    trial_id: str
    expected_control_revision: int = Field(ge=0)


class TrialExtensionRequestInput(Contract):
    """The lead's request for more trial budget; only the researcher can approve it."""
    trial_id: str
    additional_seconds: float = Field(gt=0, le=86400)
    additional_evaluations: int = Field(default=0, ge=0, le=10000000)
    rationale: str = Field(min_length=1, max_length=5000, description="Why the extra budget would change a decision")


class TrialValidationInput(ValidationInput):
    trial_id: str


class ResearchControlInput(Contract):
    run_id: str
    action: Literal["stop", "resume"]
    expected_control_revision: int = Field(ge=0)


class ResearchRetryInput(Contract):
    """Retry the exact saved question only when no model run was dispatched."""
    manager_command_id: str = Field(min_length=1, max_length=200)


class DecisionResolveInput(DecisionInput):
    decision_id: str
    expected_resolution_revision: int = Field(ge=0)


class DecisionRefreshItem(Contract):
    decision_id: str
    expected_resolution_revision: int = Field(ge=0)
    desired_choice: str | None = Field(default=None, min_length=1, max_length=200)
    comment: str = Field(default="", max_length=20000)


class DecisionRefreshInput(Contract):
    decisions: list[DecisionRefreshItem] = Field(min_length=1, max_length=50)
    comment: str = Field(default="", max_length=20000)
    max_parallel_reviews: int = Field(default=3, ge=1, le=8)
    retry_run_id: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def distinct_decisions(self):
        if len({item.decision_id for item in self.decisions}) != len(self.decisions):
            raise ValueError("Select each decision only once")
        return self


class SourceRecordInput(Contract):
    title: str = Field(min_length=1, max_length=1000)
    url: str = Field(min_length=1, max_length=2000)
    excerpt: str = Field(default="", max_length=20000)
    supports: str = Field(default="", max_length=5000)

    @field_validator("url")
    @classmethod
    def http_source(cls, value):
        from urllib.parse import urlparse
        if urlparse(value).scheme not in {"https", "http"}:
            raise ValueError("Source URL must use HTTP or HTTPS")
        return value


class SourceIngestInput(Contract):
    identifier: str = Field(min_length=1, max_length=2000)


class InferenceRunInput(Contract):
    trial_id: str
    asset_id: str
    adapter_id: str = Field(pattern=r"^[A-Za-z0-9_.-]+:v[1-9][0-9]*$")
    parameters: dict[str, Any] = Field(default_factory=dict)
    seed: int = Field(ge=0, lt=2**32)
    reuse_decision_ids: list[str] = Field(min_length=1)
    wall_seconds: float = Field(default=120, gt=0, le=86400)


class ComparisonReportInput(Contract):
    study_id: str | None = None
    cost_axis: Literal["worker_seconds", "full_worker_seconds", "evaluation_requests", "solver_executions"] | None = None
    cost_view: Literal["full_attributed_cost", "actual_expenditure"] = "full_attributed_cost"


class HypothesisReviewInput(ReviewInput):
    hypothesis_id: str


class HypothesisStatusInput(Contract):
    hypothesis_id: str
    status: Literal["proposed", "investigating", "archived", "finalist"]
    expected_status_revision: int = Field(ge=0)


class HypothesisNominateInput(Contract):
    hypothesis_id: str
    expected_status_revision: int | None = Field(default=None, ge=0)


class ContextEditInput(Contract):
    content: str = Field(min_length=1, max_length=49152)
    expected_revision: int = Field(ge=0)
    reason: str = Field(default="Researcher edited campaign memory", min_length=1, max_length=2000)


class IssueResolveInput(Contract):
    issue_id: str
    expected_revision: int = Field(ge=1)
    choice: Literal["resolved", "deferred"]
    comment: str = Field(default="", max_length=20000)


class ReuseInput(Contract):
    asset_id: str
    study_id: str
    decision: Literal["reuse", "decline", "reference"]
    intended_use: Literal["optimizer_input", "manager_evidence", "procedure"]
    rationale: str = Field(min_length=1)
    consequences: dict[str, Any] = Field(default_factory=dict)


class AssetSnapshotInput(Contract):
    trial_id: str
    observation_id: str


class WaiverInput(Contract):
    requirement_id: str
    rationale: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class WaiverRevocationInput(Contract):
    waiver_id: str
    rationale: str = Field(min_length=1)


class ExecuteValidationInput(Contract):
    requirement_id: str
    wall_seconds: float = Field(default=120, gt=0, le=86400)


class ConfirmationReleaseInput(Contract):
    protocol_id: str
    allow_incomplete: bool = False
    rationale: str = Field(default="The fixed protocol and required evidence are complete", min_length=1)


class ConfirmationScheduleInput(Contract):
    protocol_id: str
    reuse_decision_ids: list[str] = Field(default_factory=list)


class FindingInput(Contract):
    content: str = Field(min_length=1, max_length=20000)
    source_ids: list[str] = Field(min_length=1)
    interpretation: Literal["observation", "interpretation", "researcher_endorsed", "counterevidence"] = "interpretation"
    problem_scope: str = Field(default="", max_length=4000)
    study_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list, max_length=30)
    counterevidence_for: list[str] = Field(default_factory=list)
    publish: bool = False

    @model_validator(mode="after")
    def reusable_scope(self):
        if self.publish and (not self.problem_scope.strip() or not self.limitations or not all(item.strip() for item in self.limitations)):
            raise ValueError("A reusable finding requires its problem scope and explicit limitations")
        if self.interpretation == "counterevidence" and not self.counterevidence_for:
            raise ValueError("Counterevidence must name the earlier finding it qualifies")
        return self


class CommissionInput(Contract):
    hypothesis_id: str
    spec: dict[str, Any]
    compute_seconds: float = Field(gt=0, le=86400)
    max_calls: int = Field(default=12, ge=1, le=20)
    api_budget_usd: float = Field(default=0, ge=0, le=10000)
    package: dict[str, Any] | None = None
    service_idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


class AttachInput(Contract):
    hypothesis_id: str
    version_id: str


class EvaluatorCommissionInput(Contract):
    task_id: str
    spec: EvaluatorSpec
    compute_seconds: float = Field(gt=0, le=86400)
    max_calls: int = Field(default=12, ge=1, le=20)
    api_budget_usd: float = Field(default=0, ge=0, le=10000)
    package: EvaluatorPackage | None = None


class EvaluatorAttachInput(Contract):
    task_id: str
    version_id: str
    rationale: str = Field(min_length=1, max_length=5000)


class ImplementationControlInput(Contract):
    grant_id: str
    action: Literal["cancel", "resume", "close_uncertain"]


class RevalidationInput(Contract):
    version_id: str
    checks: EvaluatorCheckSpec | OptimizerCheckSpec
    compute_seconds: float = Field(default=120, gt=0, le=86400)


class RuntimeResolutionInput(Contract):
    version_id: str


class ExecutableReuseInput(Contract):
    version_id: str
    study_id: str
    hypothesis_id: str | None = None
    task_id: str | None = None
    decision: Literal["reuse", "decline"]
    rationale: str = Field(min_length=1, max_length=5000)

    @model_validator(mode="after")
    def target(self):
        if bool(self.hypothesis_id) == bool(self.task_id):
            raise ValueError("Select exactly one optimizer idea or evaluator problem for this decision")
        return self


class SearchInput(Contract):
    query: str = Field(min_length=2, max_length=500)
    provider: Literal["arxiv", "crossref"] = "crossref"
    limit: int = Field(default=5, ge=1, le=10)
