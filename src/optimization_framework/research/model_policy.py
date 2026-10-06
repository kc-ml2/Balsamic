"""Researcher-owned campaign model routing, independent of provider activation."""
from copy import deepcopy
import re
from typing import Literal

from pydantic import Field, field_validator

from optimization_framework.contracts.base import Contract
from optimization_framework.research.providers import provider_status
from optimization_framework.storage.sqlite import now


class ModelBinding(Contract):
    model: str = Field(min_length=1, max_length=200)
    reasoning_effort: Literal["low", "medium", "high", "xhigh"]
    input_usd_per_million: float | None = Field(default=None, ge=0)
    output_usd_per_million: float | None = Field(default=None, ge=0)

    @field_validator("model")
    @classmethod
    def model_name(cls, value):
        if value != value.strip() or any(c.isspace() for c in value):
            raise ValueError("Use a model identifier without whitespace")
        return value


class ModelPolicy(Contract):
    default: ModelBinding
    roles: dict[str, ModelBinding] = Field(default_factory=dict, max_length=100)

    @field_validator("roles")
    @classmethod
    def role_names(cls, value):
        if any(not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", role) for role in value):
            raise ValueError("Role identifiers use letters, digits, underscores and hyphens")
        return value


class ModelPolicyUpdate(Contract):
    expected_revision: int = Field(ge=0)
    policy: ModelPolicy
    reason: str = Field(default="Researcher changed campaign models", min_length=1, max_length=2000)


ALIASES = {"research_synthesizer": "campaign_manager"}
FAMILIES = ("literature_investigator", "methodology_specialist")
KNOWN_ROLES = ("campaign_manager", "problem_analyst", "skeptical_domain_analyst",
    "literature_investigator", "methodology_specialist", "cross_domain_methodology_specialist",
    "combinatorial_generator", "statistical_generator", "evolution_specialist", "proposal_reviewer",
    "independent_critic", "assumption_reviewer", "comparative_reviewer", "diversity_curator",
    "experiment_designer", "implementation_test_designer", "implementation_builder", "implementation_validator", "technical_report_writer",
    "report_editor", "report_evidence_investigator", "report_scientific_reviewer", "report_reader_reviewer")


def matched_role(policy, role):
    roles = policy.get("roles", {})
    if role in roles:
        return role
    canonical = ALIASES.get(role, role)
    if canonical in roles:
        return canonical
    return next((family for family in FAMILIES if family in roles and
        re.search(r"(?:^|_)" + family + r"(?:_|$)", canonical)), None)


def apply_binding(config, binding):
    result = deepcopy(config)
    binding = {key: value for key, value in binding.items() if key in {
        "model", "reasoning_effort", "input_usd_per_million", "output_usd_per_million"} and value is not None}
    if binding.get("model", result.get("model")) != result.get("model"):
        # A different model must never inherit another model's price estimate.
        result.update(input_usd_per_million=None, output_usd_per_million=None, pricing_known=False)
        result["pricing_basis"] = "Campaign model assignment; explicit rates required for API calls"
    result.update(binding)
    if any(key in binding for key in ("input_usd_per_million", "output_usd_per_million")):
        result["pricing_basis"] = "Researcher-configured campaign model rates"
    if result.get("billing_mode") != "subscription":
        result["pricing_known"] = all(result.get(key) is not None for key in
            ("input_usd_per_million", "output_usd_per_million"))
    return result


def resolve_policy(config, policy, role):
    if not policy:
        return deepcopy(config)
    key = matched_role(policy, role)
    # Saved bindings are complete. Blank price fields mean unknown even when
    # the model happens to match the server default; legacy partial overrides
    # still use apply_binding directly.
    base = {**config, "input_usd_per_million": None, "output_usd_per_million": None,
        "pricing_known": False, "pricing_basis": "Campaign model assignment; explicit rates required for API calls"}
    return apply_binding(base, policy["roles"][key] if key else policy["default"])


class CampaignModels:
    def __init__(self, workspace):
        self.workspace, self.store = workspace, workspace.store

    def record(self, campaign_id):
        try:
            return self.store.get("model_policy_" + campaign_id, "model_policy")
        except KeyError:
            return None

    def snapshot(self, campaign_id):
        record = self.record(campaign_id)
        return deepcopy(record["policy"]) if record else None

    def config(self, campaign_id, role, *, base=None, legacy_roles=None):
        base = base if base is not None else provider_status()
        policy = self.snapshot(campaign_id)
        if policy:
            return resolve_policy(base, policy, role)
        return apply_binding(base, (legacy_roles or {}).get(role, {}))

    def save(self, campaign_id, values):
        values = values if isinstance(values, ModelPolicyUpdate) else ModelPolicyUpdate(**values)
        with self.workspace.lock, self.store.transaction():
            self.store.get(campaign_id, "campaign")
            previous = self.record(campaign_id)
            revision = previous["revision"] if previous else 0
            if revision != values.expected_revision:
                raise ValueError("Model settings changed; reload the panel before saving")
            record = {"id": "model_policy_" + campaign_id, "campaign_id": campaign_id,
                "revision": revision + 1, "policy": values.policy.model_dump(mode="json"),
                "reason": values.reason, "updated_at": now(), "actor": "researcher"}
            self.store.put_immutable("model_policy_revision", {**record,
                "id": record["id"] + "_revision_" + str(record["revision"])}, "models.revised")
            self.store.put("model_policy", record, "models.configured")
            return record

    def view(self, campaign_id):
        self.store.get(campaign_id, "campaign")
        record, base = self.record(campaign_id), provider_status()
        session = self.workspace.discovery.active(campaign_id)
        legacy = session["policy"].get("role_models", {}) if session else {}
        binding = {key: base.get(key) for key in ("model", "reasoning_effort", "input_usd_per_million", "output_usd_per_million")}
        policy = record["policy"] if record else {"default": binding, "roles": {
            key: {field: effective.get(field) for field in binding}
            for key, value in legacy.items() if value
            for effective in [apply_binding(base, value)]}}
        role_ids = set(KNOWN_ROLES) | set(policy["roles"])
        role_ids.update(t["brief"]["role"] for t in self.store.list("discovery_task", campaign_id))
        roles = []
        for role in sorted(role_ids):
            effective = resolve_policy(base, policy, role) if record else apply_binding(base, legacy.get(role, {}))
            match = matched_role(policy, role) if record else (role if role in legacy else None)
            roles.append({"role": role, "label": role.replace("_", " ").capitalize(),
                "model": effective["model"], "reasoning_effort": effective["reasoning_effort"],
                "source": "override" if record and match else "legacy" if match else "default", "matched_role": match})
        return {"revision": record["revision"] if record else 0, "saved": bool(record),
            "configured": base["configured"], "provider": base, "policy": policy, "roles": roles}
