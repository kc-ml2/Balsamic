"""Model tiers (dev mode): named model choices that agent roles are assigned to.

One workspace-wide mapping: each tier names one model and thinking level, from any
family, and each role points at a tier. Changing a tier moves every agent that
follows it at its next turn, except agents pinned to their own model and agents
whose conversation would have to continue on another model family.
"""
from __future__ import annotations

import re

from pydantic import Field, model_validator

from optimization_framework.contracts.base import Contract
from optimization_framework.storage.sqlite import now

from .models import LEAD, ROLES, ModelChoice

RECORD_ID = "model_tiers"
IMPORTER = "problem_importer"
ASSIGNABLE = (*ROLES, IMPORTER)
STRONG_ROLES = {LEAD, "proposal_reviewer", "implementation_validator", IMPORTER}


class Tier(Contract):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    label: str = Field(min_length=1, max_length=60)
    model: ModelChoice


class TierSettings(Contract):
    tiers: list[Tier] = Field(min_length=1, max_length=12)
    roles: dict[str, str] = Field(default_factory=dict)
    expected_revision: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def consistent(self):
        ids = [tier.id for tier in self.tiers]
        if len(set(ids)) != len(ids):
            raise ValueError("Tier identifiers must be distinct")
        if unknown := sorted(set(self.roles) - set(ASSIGNABLE)):
            raise ValueError("Unknown agent roles: " + ", ".join(unknown))
        if missing := sorted({tier for tier in self.roles.values() if tier not in ids}):
            raise ValueError("Roles point at undefined tiers: " + ", ".join(missing))
        return self


def slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:40] or "tier"


def defaults(profile_model: dict | None) -> dict:
    """Until the researcher saves tiers, every tier uses the profile's default model, if any."""
    model = profile_model or {"provider": "", "model": "", "effort": None}
    tiers = [{"id": tier, "label": tier.capitalize(), "model": dict(model)} for tier in ("strong", "medium", "fast")]
    return {"id": RECORD_ID, "revision": 0, "tiers": tiers,
            "roles": {role: "strong" if role in STRONG_ROLES else "medium" for role in ASSIGNABLE}, "saved": False}


def load(store, profile_model=None) -> dict:
    try:
        return store.get(RECORD_ID, "model_tiers")
    except KeyError:
        return defaults(profile_model)


def choice_for(settings: dict, role: str) -> tuple[dict | None, str | None]:
    """The role's tier model and tier id, or (None, None) when the role has no tier."""
    tier_id = settings.get("roles", {}).get(role)
    tier = next((tier for tier in settings.get("tiers", []) if tier["id"] == tier_id), None)
    return (dict(tier["model"]), tier_id) if tier else (None, None)


def save(store, values: TierSettings) -> dict:
    current = load(store)
    if current["revision"] != values.expected_revision:
        raise ValueError("Model tiers changed since this page loaded; refresh and apply your change again")
    record = {"id": RECORD_ID, "revision": current["revision"] + 1, "saved": True, "updated_at": now(),
              "tiers": [tier.model_dump(mode="json", exclude={"schema_version"}) for tier in values.tiers],
              "roles": dict(values.roles)}
    for tier in record["tiers"]:
        tier["model"].pop("schema_version", None)
    store.put("model_tiers", record)
    return record
