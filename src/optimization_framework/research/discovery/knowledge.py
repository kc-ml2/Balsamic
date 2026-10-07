"""Scientific work products, source support and candidate lineage.

These records describe conjectures and the basis for testing them. Producing a
well-formed proposal never validates code or establishes optimizer performance.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from optimization_framework.contracts.base import Contract, content_hash
from .assessment import AssessmentPlan
from .proposals import ProposalReview, request_context, validate_review


class Claim(Contract):
    statement: str = Field(min_length=1, max_length=6000)
    basis: Literal["evaluator_contract", "measurement", "literature", "assumption", "unknown"]
    evidence_ids: list[str] = Field(default_factory=list, max_length=30)


class ProblemDossier(Contract):
    representation: str = Field(min_length=1, max_length=6000)
    objectives_and_constraints: str = Field(min_length=1, max_length=8000)
    available_operations: str = Field(min_length=1, max_length=6000)
    cost_and_noise: str = Field(min_length=1, max_length=6000)
    claims: list[Claim] = Field(min_length=1, max_length=40)
    unknowns: list[str] = Field(default_factory=list, max_length=30)
    characterization_needs: list[str] = Field(default_factory=list, max_length=20)


class Citation(Contract):
    claim: str = Field(min_length=1, max_length=4000)
    source_id: str
    capture_id: str
    passage_ids: list[str] = Field(min_length=1, max_length=12)


class MethodApplicability(Contract):
    name: str = Field(min_length=1, max_length=300)
    mechanism: str = Field(min_length=1, max_length=8000)
    applicability: str = Field(min_length=1, max_length=8000)
    assumptions: list[str] = Field(min_length=1, max_length=30)
    parameter_guidance: str = Field(min_length=1, max_length=6000)
    support: list[Citation] = Field(default_factory=list, max_length=20)
    coverage_gaps: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def support_or_gap(self):
        if not self.support and not self.coverage_gaps:
            raise ValueError("A methodology needs captured passage support or an explicit literature coverage gap")
        return self


class LiteratureMap(Contract):
    search_strategy: str = Field(min_length=1, max_length=8000)
    retrieval_ids: list[str] = Field(min_length=1, max_length=100,
        description="Exact IDs from supplied source_retrieval records or source.* tool receipts (discovery.retrieval_receipt_ids). Include failed retrievals as coverage evidence. Source IDs and capture IDs are not retrieval receipts.")
    methods: list[MethodApplicability] = Field(min_length=1, max_length=30)
    unresolved_gaps: list[str] = Field(default_factory=list, max_length=30)


class Candidate(Contract):
    key: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    title: str = Field(min_length=1, max_length=300)
    family: str = Field(min_length=1, max_length=300)
    mechanism: str = Field(min_length=1, max_length=10000)
    applicability: str = Field(min_length=1, max_length=10000)
    assumptions: list[str] = Field(min_length=1, max_length=30)
    predictions: list[str] = Field(min_length=1, max_length=30)
    failure_modes: list[str] = Field(min_length=1, max_length=30)
    parameter_space: dict[str, Any] = Field(default_factory=dict)
    startup_requirements: str = Field(min_length=1, max_length=8000)
    implementation_needs: str = Field(min_length=1, max_length=8000)
    algorithm: str | None = Field(default=None, max_length=100)
    algorithm_config: dict[str, Any] = Field(default_factory=dict)
    implementation_version_id: str | None = None
    cheapest_test: str = Field(min_length=1, max_length=8000)
    support: list[Citation] = Field(default_factory=list, max_length=30)
    conjectures: list[str] = Field(default_factory=list, max_length=30)
    parent_candidate_ids: list[str] = Field(default_factory=list, max_length=10)
    parent_hypothesis_ids: list[str] = Field(default_factory=list, max_length=10,
        description="Exact supplied hypothesis IDs for researcher-authored parents without a discovery candidate ID.")
    revision_basis: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def scientific_basis(self):
        if not self.support and not self.conjectures:
            raise ValueError("A candidate needs source-backed support or explicit unverified conjectures")
        if (self.parent_candidate_ids or self.parent_hypothesis_ids) and not self.revision_basis:
            raise ValueError("A revision must state the evidence or reasoning motivating its changes")
        return self


class CandidateBatch(Contract):
    dossier_ids: list[str] = Field(min_length=1, max_length=10)
    literature_map_ids: list[str] = Field(min_length=1, max_length=10)
    candidates: list[Candidate] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def unique_keys(self):
        if len({row.key for row in self.candidates}) != len(self.candidates):
            raise ValueError("Candidate keys must be unique in a batch")
        return self


SCHEMAS = {"problem_dossier": ProblemDossier, "literature_map": LiteratureMap, "candidate_batch": CandidateBatch,
           "proposal_review": ProposalReview, "assessment_plan": AssessmentPlan}


def schemas():
    return {key: model.model_json_schema() for key, model in SCHEMAS.items()}


def validate_stage(task, result, prior_artifacts=()):
    expected = {"analyze": "problem_dossier", "study": "literature_map", "generate": "candidate_batch"}.get(task["brief"]["stage"])
    if expected and task["brief"]["role"] != "campaign_manager" and result.disposition == "complete":
        if not any(artifact.kind == expected for artifact in result.artifacts) and not any(
                artifact.get("kind") == expected and not artifact.get("stale") for artifact in prior_artifacts):
            raise ValueError(f"A completed {task['brief']['stage']} task must produce a {expected} artifact, or explicitly report blocked work")
    if task["brief"]["role"] == "proposal_reviewer" and result.disposition == "complete":
        reviewed = {artifact.content.get("candidate_id") for artifact in result.artifacts if artifact.kind == "proposal_review"}
        reviewed.update(row["content"].get("candidate_id") for row in prior_artifacts
                        if row.get("kind") == "proposal_review" and not row.get("stale"))
        if set(task["brief"]["evidence_ids"]) - reviewed:
            raise ValueError("Review every assigned candidate with a proposal_review artifact, or explicitly report blocked work")


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def supplied_passages(value):
    if isinstance(value, dict):
        if all(key in value for key in ("id", "capture_id", "text")):
            yield (value["id"], content_hash([value["capture_id"], value["text"]]))
        for item in value.values():
            yield from supplied_passages(item)
    elif isinstance(value, list):
        for item in value:
            yield from supplied_passages(item)


def validate(controller, session, task, artifact):
    from .references import validate as validate_references
    validate_references(controller, session, task, artifact)
    if artifact.kind not in SCHEMAS:
        return artifact.content
    parsed = SCHEMAS[artifact.kind].model_validate(artifact.content)
    if artifact.kind == "candidate_batch" and task["brief"]["stage"] != "generate":
        raise ValueError("Candidate generation requires an independent generation task")
    store = controller.store
    if isinstance(parsed, AssessmentPlan):
        candidate = controller._evidence(session, parsed.candidate_id, task=task)
        if store.get_entry(candidate["id"])["kind"] != "discovery_candidate":
            raise ValueError("Assessment plans must name an exact candidate revision")
    attempt = store.get(task["attempt_id"], "discovery_attempt") if task.get("attempt_id") else None
    supplied = set(strings(attempt["context_snapshot"])) if attempt else set()
    read_passages = set(supplied_passages(attempt["context_snapshot"])) if attempt else set()
    if isinstance(parsed, ProposalReview):
        validate_review(controller, session, task, parsed, supplied)
    for claim in getattr(parsed, "claims", []):
        if claim.basis in {"measurement", "literature"} and not claim.evidence_ids:
            raise ValueError("Observed claims require evidence identifiers")
        for identity in claim.evidence_ids:
            if identity not in supplied:
                raise ValueError("Dossier claims must cite evidence supplied to this task")
            controller._evidence(session, identity, task=task)
    for method in getattr(parsed, "methods", getattr(parsed, "candidates", [])):
        for citation in method.support:
            capture = controller._evidence(session, citation.capture_id, task=task)
            if capture.get("source_id") != citation.source_id:
                raise ValueError("Citation source does not match its captured document")
            for identity in citation.passage_ids:
                passage = controller._evidence(session, identity, task=task)
                if passage.get("capture_id") != capture["id"]:
                    raise ValueError("Citation passage belongs to a different capture")
                if (identity, content_hash([passage["capture_id"], passage["text"]])) not in read_passages:
                    raise ValueError("Technical claims must cite passages actually supplied to this task")
    for identity in getattr(parsed, "retrieval_ids", []):
        retrieval = controller._evidence(session, identity, task=task)
        kind = store.get_entry(identity)["kind"]
        if (identity not in supplied or kind not in {"source_retrieval", "discovery_tool_receipt"}
                or kind == "discovery_tool_receipt" and not retrieval.get("tool", "").startswith("source.")):
            raise ValueError(f"Literature maps require actual supplied retrieval receipts, including documented failures. {identity} is {kind}; use discovery.retrieval_receipt_ids, not a source or capture ID")
    if isinstance(parsed, CandidateBatch):
        for kind, identities in (("problem_dossier", parsed.dossier_ids), ("literature_map", parsed.literature_map_ids)):
            for identity in identities:
                evidence = controller._evidence(session, identity, task=task)
                if identity not in supplied or evidence.get("kind") != kind or evidence.get("stale"):
                    raise ValueError("Generate candidates from the approved dossier and literature map")
        for candidate in parsed.candidates:
            for identity in candidate.parent_candidate_ids:
                parent = controller._evidence(session, identity, task=task)
                if identity not in supplied or store.get_entry(identity)["kind"] != "discovery_candidate":
                    raise ValueError("Candidate parents must be explicitly supplied earlier candidate revisions")
            for identity in candidate.parent_hypothesis_ids:
                controller._evidence(session, identity, task=task)
                if identity not in supplied or store.get_entry(identity)["kind"] != "hypothesis":
                    raise ValueError("Hypothesis parents must be explicitly supplied earlier proposals")
            context = request_context(store, task)
            if context and context["request"].get("proposal_operation") in {"diversify", "hybrid"}:
                parents = set(candidate.parent_hypothesis_ids) | {"hypothesis_" + key for key in candidate.parent_candidate_ids}
                if not set(context["request"]["parent_hypothesis_ids"]) <= parents:
                    raise ValueError("Every variant or hybrid must preserve the researcher's selected parent proposals")
    return parsed.model_dump(mode="json")


def project_candidates(controller, session, task, artifact):
    if artifact["kind"] != "candidate_batch" or artifact["stale"]:
        return
    from optimization_framework.optimizers.registry import methods
    known = {row["id"] for row in methods()}
    store = controller.store
    batch = CandidateBatch.model_validate(artifact["content"])
    for candidate in batch.candidates:
        identity = "candidate_" + content_hash([artifact["id"], candidate.key])[:28]
        family_id = "family_" + content_hash([session["id"], candidate.family.casefold()])[:28]
        try:
            store.get(family_id, "methodology_family")
        except KeyError:
            store.put_immutable("methodology_family", {"id": family_id, "campaign_id": session["campaign_id"],
                "session_id": session["id"], "name": candidate.family, "created_at": artifact["created_at"]}, "discovery.family_created")
        try:
            # Replaying an artifact produced by an earlier schema must retain
            # its immutable candidate rather than retroactively adding policy.
            record = store.get(identity, "discovery_candidate")
        except KeyError:
            record = store.put_immutable("discovery_candidate", {"id": identity, "campaign_id": session["campaign_id"],
                "session_id": session["id"], "task_id": task["id"], "family_id": family_id, "artifact_id": artifact["id"],
                "created_at": artifact["created_at"], "origin": "llm", "claim_level": "conjecture",
                "requires_concept_review": True, "proposal_request_id": task.get("proposal_request_id"),
                **candidate.model_dump(mode="json")}, "discovery.candidate_created")
        hypothesis_id = "hypothesis_" + identity
        try:
            store.get(hypothesis_id, "hypothesis")
            continue
        except KeyError:
            pass
        algorithm = candidate.algorithm if candidate.algorithm in known else "custom"
        # A model-suggested version is a reuse request, not an accepted binding.
        # Attaching it still passes the implementation service's compatibility
        # and correctness gates through the existing application command.
        hypothesis = {"id": hypothesis_id, "campaign_id": session["campaign_id"], "candidate_id": identity,
            "family_id": family_id, "title": candidate.title, "mechanism": candidate.mechanism,
            "rationale": candidate.applicability, "assumptions": candidate.assumptions, "risks": candidate.failure_modes,
            "predictions": candidate.predictions, "protocol": candidate.cheapest_test, "cheapest_test": candidate.cheapest_test,
            "startup_cost": candidate.startup_requirements,
            "algorithm": algorithm, "algorithm_config": candidate.algorithm_config,
            "implementation_needs": candidate.implementation_needs, "suggested_implementation_version_id": candidate.implementation_version_id,
            "implementation_status": "builtin" if algorithm in known else "missing", "executable": algorithm in known,
            "sources": [store.get(row.source_id, "source") for row in candidate.support],
            "parent_ids": list(dict.fromkeys([*["hypothesis_" + key for key in candidate.parent_candidate_ids], *candidate.parent_hypothesis_ids])),
            "requires_concept_review": record.get("requires_concept_review", False), "proposal_request_id": record.get("proposal_request_id"),
            "change_summary": "\n".join(candidate.revision_basis),
            "reviews": [], "status": "proposed", "status_revision": 0, "origin": "llm", "claim_level": "rationale_only",
            "research_run_id": task["run_id"], "charter_version": session["charter_version"], "created_at": artifact["created_at"]}
        store.put("hypothesis", hypothesis, "hypothesis.created")
