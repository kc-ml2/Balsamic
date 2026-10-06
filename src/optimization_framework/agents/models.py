from typing import Literal
from pydantic import Field
from optimization_framework.contracts.base import Contract


# The campaign lead coordinates the other roles. ("pi" was a misreading of the
# Pi harness as a "principal investigator"; stored records are migrated.)
LEAD = "lead"
ROLES = (LEAD, "literature_specialist", "methodology_specialist", "cross_domain_explorer",
         "skeptical_domain_analyst", "empirical_assessor", "proposal_reviewer", "implementation_builder",
         "implementation_test_designer", "implementation_validator", "results_analyst")


THINKING = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]


class ModelChoice(Contract):
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    effort: THINKING | None = None


class Activate(Contract):
    objective: str = Field(default="Continue the campaign from its saved evidence and unfinished requests.", min_length=1, max_length=20000)
    max_subagents: int = Field(default=4, ge=1, le=8)
    delegated: bool = True
    # Default model for the campaign's agents; it also fixes the campaign's model family.
    model: ModelChoice | None = None


class Configure(Contract):
    """Dev mode: change the model or thinking level of one agent, or the campaign default."""
    agent_id: str | None = None
    model: ModelChoice


class Message(Contract):
    agent_id: str | None = None
    message: str = Field(min_length=1, max_length=50000)
    mode: Literal["steer", "follow_up"] = "steer"
    question_id: str | None = None


class Control(Contract):
    agent_id: str | None = None
    action: Literal["pause", "resume", "stop"]
    expected_control_revision: int = Field(ge=0)


class Rollback(Contract):
    reason: str = Field(min_length=1, max_length=5000)
