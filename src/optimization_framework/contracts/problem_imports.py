"""Problem examples and importer drafts: reviewable setups that exist before a campaign does."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from .base import Contract
from .evaluators import EvaluatorManifest


class ProblemSetup(Contract):
    """One problem instance, in the shape the New campaign form submits."""
    name: str = Field(min_length=1, max_length=200)
    split: Literal["development", "selection", "test"] = "development"
    problem_id: str | None = Field(default=None, description="An installed problem adapter")
    configuration: dict[str, Any] = Field(default_factory=dict)
    fidelity: dict[str, Any] = Field(default_factory=dict)
    evaluator_manifest: EvaluatorManifest | None = Field(default=None, description="A declared problem whose evaluator is still needed")
    rationale: str = Field(default="", max_length=5000)

    @model_validator(mode="after")
    def one_problem(self):
        if (self.problem_id is None) == (self.evaluator_manifest is None):
            raise ValueError("Choose either an installed problem adapter or a declared problem manifest")
        return self

    def task_input(self):
        """The campaign's own validation; a setup that fails here cannot start a campaign."""
        from .requests import TaskInput
        manifest = self.evaluator_manifest
        return TaskInput(name=self.name, split=self.split, problem_id=manifest.id if manifest else self.problem_id,
            configuration=self.configuration, fidelity=self.fidelity, evaluator_manifest=manifest)


class CampaignDefaults(Contract):
    name: str | None = Field(default=None, max_length=200)
    objective: str | None = Field(default=None, max_length=20000)
    compute_budget_seconds: float | None = Field(default=None, gt=0)
    validation_reserve_seconds: float | None = Field(default=None, ge=0)
    delegated_trial_seconds: float | None = Field(default=None, gt=0, le=3600)
    implementation_compute_budget_seconds: float | None = Field(default=None, ge=0, le=604800)
    llm_budget_usd: float | None = Field(default=None, ge=0, le=10000)
    autonomy: Literal["manual", "guided", "delegated"] | None = None


class ProblemExample(Contract):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,99}$")
    name: str = Field(min_length=1, max_length=200)
    summary: str = Field(default="", max_length=2000)
    source: Literal["installed", "saved"] = "installed"
    order: int = Field(default=100, ge=0, le=1000, description="Lower installed examples are offered first")
    instances: list[ProblemSetup] = Field(min_length=1, max_length=20)
    campaign: CampaignDefaults = Field(default_factory=CampaignDefaults)
    import_id: str | None = None


class DraftCitation(Contract):
    source: str = Field(min_length=1, max_length=300, description="Path under documents/ or code/")
    location: str = Field(default="", max_length=200, description="Page, section or line range")
    quote: str = Field(default="", max_length=600)


class ProblemDraft(Contract):
    """What the importer proposes; the researcher reviews and edits it before use."""
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=5000)
    objective: str = Field(min_length=1, max_length=20000, description="Campaign charter text")
    instances: list[ProblemSetup] = Field(min_length=1, max_length=20)
    assumptions: list[str] = Field(default_factory=list, max_length=50)
    open_questions: list[str] = Field(default_factory=list, max_length=50)
    evaluator_notes: str = Field(default="", max_length=20000,
        description="For a declared problem: what the evaluator must compute and which supplied code implements it")
    citations: list[DraftCitation] = Field(default_factory=list, max_length=100)

    def validate_setups(self):
        for setup in self.instances:
            setup.task_input()
        return self
