"""Campaign allocations, readiness, and immutable implementation bindings."""
from __future__ import annotations

import copy
import json
import time

from optimization_framework.implementations.client import ImplementationClient
from optimization_framework.implementations.models import JobRequest, RevalidationRequest, LibraryUnavailable, check_parameters, digest
from optimization_framework.implementations.runtime import tree_hashes, verify_runtime, write_package, bundle_runtime_root, pin_runtime
from optimization_framework.implementations.validation import evaluator_identity, profile_identity
from optimization_framework.research.providers import api_spend
from optimization_framework.storage.sqlite import atomic_json, identifier, now


SETTLED = {"completed", "failed", "cancelled", "closed_uncertain"}


def settled(grant):
    return grant["status"] in SETTLED and grant.get("accounting_final", True)


def public_grant(grant):
    """Keep source bundles out of the frequently refreshed campaign response."""
    result = copy.deepcopy(grant)
    result["request"].pop("package", None)
    for attempt in result.get("attempts", []):
        attempt.pop("package", None)
    return result


class ImplementationBridge:
    def __init__(self, workspace, client=None):
        self.workspace = workspace
        self.store = workspace.store
        self.client = client or ImplementationClient()
        self.connection_error = None
        self.workspace_id = self.store.identity()
        # Unsettled jobs are refetched when the library's event feed names them;
        # this sweep covers a missed event or a library without the feed.
        self._seen_jobs = set()
        self._job_sweep_at = 0.0
        # Backfill cached evidence with its actual admission time. Historical
        # service timestamps cannot establish evidence before a workspace cutoff.
        from optimization_framework.evaluation.executables import record
        for version in self.store.list("implementation_cache"):
            record(self.store, version)

    def cache_version(self, version):
        from optimization_framework.evaluation.executables import record
        with self.workspace.lock, self.store.transaction():
            self.store.put("implementation_cache", version)
            record(self.store, version)
            if version.get("kind") == "evaluator" and getattr(self.workspace, "evaluators", None):
                self.workspace.evaluators.sync_version_evidence(version)
        return version

    def catalog(self, *, refresh=False):
        if refresh:
            try:
                for version in self.client.versions():
                    self.cache_version(version)
                self.connection_error = None
            except ValueError as exc:
                self.connection_error = str(exc)
        return {"versions": self.store.list("implementation_cache"), "connection_error": self.connection_error}

    def validation_current(self, version):
        report = version["validation_report"]
        evaluator_id = version["spec"].get("evaluator_version_id")
        if evaluator_id:
            from optimization_framework.implementations.evaluator_validation import current
            from optimization_framework.evaluation.generated import evaluator_identity as bound_identity
            try:
                evaluator = self.store.get(evaluator_id, "implementation_cache")
            except KeyError:
                return False
            if evaluator.get("kind") != "evaluator" or evaluator["status"] not in {"validated", "contract_validated"} or not current(evaluator, numerical=False):
                return False
            expected = bound_identity(evaluator)
        else:
            expected = evaluator_identity()
        from optimization_framework.implementations.revalidation import report_matches
        return (not version.get("blocking_validation_report_ids") and report.get("passed") is True
            and report["profile_digest"] == profile_identity() and report["evaluator_digest"] == expected
            and report.get("runtime_digest") == version["runtime_digest"] and report_matches(version, report))

    def readiness(self, hypothesis, task=None):
        from optimization_framework.execution.service import ALGORITHMS
        version_id = hypothesis.get("implementation_version_id")
        job = next((j for j in reversed(self.store.list("implementation_grant", hypothesis.get("campaign_id")))
                    if j.get("hypothesis_id") == hypothesis.get("id") and j["status"] not in SETTLED), None)
        if version_id:
            try:
                version = self.store.get(version_id, "implementation_cache")
            except KeyError:
                return {"state": "unavailable", "runnable": False, "reason": "Implementation version has not been retrieved from the library.", "version_id": version_id}
            report = version["validation_report"]
            if version.get("kind") == "evaluator":
                return {"state": "incompatible", "runnable": False, "reason": "An evaluator version cannot be used as an optimizer.", "version_id": version_id}
            if version["status"] != "validated":
                return {"state": "unavailable", "runnable": False, "reason": version.get("revocation_reason", "Implementation is not validated"), "version_id": version_id}
            if not self.validation_current(version):
                return {"state": "validation_required", "runnable": False, "reason": "The evaluator or validation profile changed; revalidate this version.", "version_id": version_id}
            try:
                self.compatible(version, task, hypothesis.get("algorithm_config", {}))
            except ValueError as exc:
                return {"state": "incompatible", "runnable": False, "reason": str(exc), "version_id": version_id}
            return {"state": "ready", "runnable": True, "reason": "Implementation validation passed. Experimental performance is assessed separately.",
                    "version_id": version_id, "validation_report_id": report["id"]}
        if hypothesis.get("algorithm") in {a["id"] for a in ALGORITHMS}:
            from optimization_framework.optimizers.registry import capability_reason
            candidates = [task] if task else self.workspace.current_tasks(hypothesis["campaign_id"])
            reasons = [capability_reason(hypothesis["algorithm"], candidate["problem"]) for candidate in candidates if candidate.get("problem")]
            if reasons and all(reasons):
                return {"state": "incompatible", "runnable": False, "reason": "; ".join(sorted(set(reasons)))}
            return {"state": "ready", "runnable": True, "reason": "Built-in implementation is available.", "validation_level": "bundled_regression"}
        if job:
            return {"state": job["status"], "runnable": False, "reason": job.get("error") or "Implementation work is in progress.", "job_id": job.get("job_id"), "grant_id": job["id"]}
        if hypothesis.get("source"):
            return {"state": "validation_required", "runnable": False,
                    "reason": "Source is available, but requires independent implementation validation. Previous protocol checks remain in the record."}
        from .references import catalog
        references = catalog(self.store, hypothesis["campaign_id"], hypothesis)
        if references:
            return {"state": "reference_available", "runnable": False, "references": references,
                    "reason": "Reference code is available. Adapt this source to the campaign execution interface and validate it before running trials."}
        reason = "No campaign implementation is attached. Link existing reference code or request an implementation."
        if hypothesis.get("algorithm") == "relaxed_gradient":
            reason += " It also requires continuous evaluation and RCWA gradients, which the current evaluator does not provide."
        return {"state": "missing", "runnable": False, "reason": reason}

    @staticmethod
    def compatible(version, task, parameters):
        if version.get("kind") == "evaluator":
            raise ValueError("An evaluator version cannot be used as an optimizer")
        spec = version["spec"]
        if task and task.get("problem") and spec.get("problem_id", "meent_grating") != task["problem"]["definition_id"]:
            raise ValueError("This implementation was validated for a different problem adapter; request compatible correctness evidence")
        if task and spec.get("evaluator_version_id") and spec["evaluator_version_id"] != task.get("problem", {}).get("evaluator_version"):
            raise ValueError("The optimizer's correctness evidence requires the exact commissioned evaluator version")
        if task and task.get("problem", {}).get("candidate_schema", {}).get("constraints") and not spec.get("supports_constraints"):
            raise ValueError("This implementation does not support the problem's explicit feasibility constraints")
        from optimization_framework.evaluation.registry import problems
        capabilities = (set(task["problem"]["capabilities"]) if task and task.get("problem") else
                        set(version["validation_report"]["scope"]["evaluator_capabilities"]) if spec.get("evaluator_version_id") else
                        set(problems.get(spec.get("problem_id", "meent_grating")).describe().capabilities))
        missing = set(spec["capabilities"]) - capabilities
        if missing:
            raise ValueError("Unsupported evaluator capabilities: " + ", ".join(sorted(missing)))
        check_parameters({**spec["parameters"], **parameters}, spec["parameter_schema"])
        dimensions = task["problem"]["candidate_schema"]["dimensions"] if task and task.get("problem") else (task or {}).get("physics", {}).get("n_cells")
        if task and not spec["n_cells_min"] <= dimensions <= spec["n_cells_max"]:
            raise ValueError("Task cell count is outside this implementation's validated scope")

    def attach(self, hypothesis_id, version_id, *, expected_context=None, _bundle=None):
        bundle = _bundle if _bundle is not None else self.client.artifact(version_id)
        version = self.cache_version(bundle["version"])
        if version["spec"].get("evaluator_version_id") and _bundle is None:
            self.cache_version(self.client.version(version["spec"]["evaluator_version_id"]))
        with self.workspace.lock:
            hypothesis = self.store.get(hypothesis_id, "hypothesis")
            self.workspace.check_manager_context(hypothesis["campaign_id"], expected_context)
            if hypothesis["status"] == "finalist":
                raise ValueError("Fork this frozen finalist before changing its implementation")
            self.compatible(version, None, hypothesis.get("algorithm_config", {}))
            if hypothesis.get("implementation_version_id") == version_id:
                return hypothesis
            hypothesis.setdefault("implementation_history", []).append({"algorithm": hypothesis.get("algorithm"),
                "version_id": hypothesis.get("implementation_version_id"), "changed_at": now()})
            hypothesis.update(implementation_version_id=version_id, algorithm="package", executable=True,
                              implementation_status="validated",
                              algorithm_config={**version["spec"]["parameters"], **hypothesis.get("algorithm_config", {})})
            self.store.put("hypothesis", hypothesis, "implementation.attached")
            self._exposure(hypothesis["campaign_id"], version)
            return hypothesis

    def _exposure(self, campaign_id, version):
        for condition in version.get("exposed_conditions", []):
            self.store.put("implementation_exposure", {"id": "impl_exposure_" + digest([campaign_id, version["id"], condition])[:24],
                "campaign_id": campaign_id, "condition_key": condition, "implementation_version_id": version["id"],
                "provenance": version["spec"].get("provenance", []), "created_at": now()})

    def api_committed(self, campaign_id):
        return sum(api_spend(g.get("usage")) if settled(g) else max(g["request"]["api_budget_usd"], api_spend(g.get("usage")))
                   for g in self.store.list("implementation_grant", campaign_id))

    def compute_committed(self, campaign_id):
        return sum(g.get("compute_seconds", 0) if settled(g) else max(g["request"]["compute_seconds"], g.get("compute_seconds", 0))
                   for g in self.store.list("implementation_grant", campaign_id))

    def commission(self, hypothesis_id, spec, *, compute_seconds, max_calls=12, api_budget_usd=0, idempotency_key, package=None, expected_context=None, accounting_mode="wall"):
        self.catalog(refresh=True)
        grant = self.reserve_commission(hypothesis_id, spec, compute_seconds=compute_seconds, max_calls=max_calls,
            api_budget_usd=api_budget_usd, idempotency_key=idempotency_key, package=package, expected_context=expected_context,
            accounting_mode=accounting_mode)
        self.workspace.dispatch_outbox()
        return self.store.get(grant["id"], "implementation_grant")

    def reserve_commission(self, hypothesis_id, spec, *, compute_seconds, max_calls=12, api_budget_usd=0, idempotency_key, package=None, expected_context=None, task_id=None, accounting_mode="wall"):
        """Commit the grant and delivery intent together; never send from a transaction."""
        with self.workspace.lock, self.store.transaction():
            if task_id is not None:
                from optimization_framework.implementations.models import EvaluatorSpec
                spec = EvaluatorSpec.model_validate(spec)
                task = self.store.get(task_id, "task")
                if hypothesis_id is not None or task.get("archived") or not task.get("evaluator_requirement_id"):
                    raise ValueError("Commission an evaluator against a current problem requirement")
                requirement = self.store.get(task["evaluator_requirement_id"], "evaluator_requirement")
                if spec.manifest.model_dump(mode="json") != requirement["manifest"]:
                    raise ValueError("Commissioned evaluator differs from the declared problem requirement")
                hypothesis = None
                campaign_id = task["campaign_id"]
            else:
                hypothesis = self.store.get(hypothesis_id, "hypothesis")
                if hypothesis["status"] in {"archived", "finalist"}:
                    raise ValueError("Revive or fork this idea before commissioning implementation work")
                campaign_id = hypothesis["campaign_id"]
            self.workspace.check_manager_context(campaign_id, expected_context)
            campaign = self.store.get(campaign_id, "campaign")
            identity = "grant_" + digest([self.workspace_id, idempotency_key])[:24]
            try:
                grant = self.store.get(identity, "implementation_grant")
            except KeyError:
                grant = None
            # A retry delivers its accepted grant, even if the researcher has
            # since selected different models for newly commissioned work.
            model_policy = (grant["request"].get("model_policy") if grant else
                            self.workspace.models.snapshot(campaign_id))
            request = JobRequest(workspace_id=self.workspace_id, campaign_id=campaign_id, hypothesis_id=hypothesis_id,
                grant_id=identity, idempotency_key=idempotency_key, spec=spec, package=package,
                compute_seconds=compute_seconds, max_attempts=1 if accounting_mode == "execution_v1" else 3,
                max_calls=max_calls, api_budget_usd=api_budget_usd,
                model_policy=model_policy,
                agent_parent_id=(grant["request"].get("agent_parent_id") if grant else
                    (self.workspace.pi.configuration(campaign_id) or {}).get("lead_id") if self.workspace.pi.owns(campaign_id) else None),
                accounting_mode=accounting_mode).model_dump()
            if (request["spec"].get("kind") == "evaluator") != (task_id is not None):
                raise ValueError("Commission this executable against the matching optimizer or evaluator requirement")
            if grant:
                if grant["request"] != request or grant.get("task_id") != task_id:
                    raise ValueError("This request identity already refers to different implementation work")
                if grant.get("job_id"):
                    return grant
            else:
                if self.compute_committed(campaign_id) + compute_seconds > campaign.get("implementation_compute_budget_seconds", 0):
                    raise ValueError("Assign a separate implementation compute allocation in the campaign charter before commissioning this job")
                research_spent = sum(api_spend(r.get("usage"))
                    + (r.get("decision_review") or {}).get("budget_hold_usd", 0)
                    for r in self.store.list("research_run", campaign_id))
                from optimization_framework.agents.usage import charged as agent_api_spend
                research_spent += agent_api_spend(self.store, campaign_id)
                if research_spent + self.api_committed(campaign_id) + api_budget_usd > campaign["llm_budget_usd"] + 1e-9:
                    raise ValueError("Implementation model allocation exceeds the campaign's remaining API cap")
                grant = {"id": identity, "campaign_id": campaign_id, "hypothesis_id": hypothesis_id,
                    "charter_version": campaign["version"], "hypothesis_digest": self.hypothesis_digest(hypothesis) if hypothesis else None,
                    "request": request, "status": "dispatching", "created_at": now(), "job_id": None,
                    "upstream_cost_asset_ids": (hypothesis or {}).get("research_cost_asset_ids", []),
                    "usage": {}, "compute_seconds": 0}
                if task_id:
                    grant.update(task_id=task_id, evaluator_requirement_id=task["evaluator_requirement_id"])
                self.store.put("implementation_grant", grant, "implementation.reserved")
                self.store.put("message", {"id": "message_" + identity, "campaign_id": campaign_id,
                    "role": "assistant", "origin": "manager_system", "grant_id": identity, "created_at": now(),
                    "content": f"Commissioned {spec.name if hasattr(spec, 'name') else spec['name']} with {compute_seconds:g}s of implementation time and at most {max_calls} model calls. The specification and acceptance criteria are frozen for this job. Unresolved validation findings will return here."}, "message.created")
            effect_id = "submit_" + identity
            try:
                self.store.get(effect_id, "outbox")
            except KeyError:
                self.store.put("outbox", {"id": effect_id, "campaign_id": campaign_id,
                    "kind": "implementation_submit", "grant_id": identity, "status": "pending", "created_at": now()}, "effect.queued")
            return grant

    def deliver_submission(self, grant_id):
        grant = self.store.get(grant_id, "implementation_grant")
        if grant.get("job_id"):
            return grant
        return self._apply_job(grant, self.client.submit(grant["request"]))

    def reserve_revalidation(self, campaign_id, version_id, checks, *, compute_seconds, idempotency_key):
        """Reserve one mechanical check grant and durable delivery in the command transaction."""
        with self.workspace.lock, self.store.transaction():
            campaign = self.store.get(campaign_id, "campaign")
            identity = "grant_" + digest([self.workspace_id, idempotency_key])[:24]
            request = RevalidationRequest(workspace_id=self.workspace_id, campaign_id=campaign_id,
                grant_id=identity, idempotency_key=idempotency_key, version_id=version_id,
                checks=checks, compute_seconds=compute_seconds).model_dump(mode="json")
            try:
                old = self.store.get(identity, "implementation_grant")
            except KeyError:
                old = None
            if old:
                if old["request"] != request:
                    raise ValueError("This grant already belongs to different implementation work")
                return old
            version = self.store.get(version_id, "implementation_cache")
            if version["status"] == "revoked":
                raise ValueError("Revoked artifacts require a corrected version")
            from optimization_framework.implementations.revalidation import plan
            plan(version, RevalidationRequest.model_validate(request).checks)
            if self.compute_committed(campaign_id) + compute_seconds > campaign.get("implementation_compute_budget_seconds", 0):
                raise ValueError("Revalidation exceeds the campaign's separate implementation compute allocation")
            grant = {"id": identity, "campaign_id": campaign_id, "hypothesis_id": None,
                "charter_version": campaign["version"], "request": request, "status": "dispatching",
                "created_at": now(), "job_id": None, "version_id": version_id,
                "upstream_cost_asset_ids": [], "usage": {}, "compute_seconds": 0}
            self.store.put("implementation_grant", grant, "implementation.reserved")
            self.store.put("outbox", {"id": "submit_" + identity, "campaign_id": campaign_id,
                "kind": "implementation_submit", "grant_id": identity, "status": "pending", "created_at": now()}, "effect.queued")
            self.store.put("message", {"id": "message_" + identity, "campaign_id": campaign_id,
                "role": "assistant", "origin": "manager_system", "grant_id": identity, "created_at": now(),
                "content": f"Reserved {compute_seconds:g}s to revalidate {version['name']} against independent checks. The existing executable stays pinned; this job makes no model calls."}, "message.created")
            return grant

    @staticmethod
    def hypothesis_digest(hypothesis):
        return digest({key: hypothesis.get(key) for key in ("id", "mechanism", "algorithm", "algorithm_config", "source", "implementation_version_id")})

    def _apply_job(self, grant, job):
        with self.workspace.lock, self.store.transaction():
            # Preserve the fixed grant even if the caller loses an acknowledgement.
            # Admit a producer revision and its usage in the same transaction;
            # an older concurrent HTTP reply cannot append stale accounting.
            current = self.store.get(grant["id"], "implementation_grant")
            if job.get("revision") is not None and job["revision"] < current.get("source_job_revision", 0):
                return current
            accounted = None
            if job["status"] in SETTLED | {"interrupted", "blocked", "needs_reconciliation"} and job.get("accounting_final", False):
                from optimization_framework.assets.service_costs import record_implementation_job
                record_implementation_job(self.workspace.assets, {**job, "campaign_id": current["campaign_id"],
                    "created_at": job.get("created_at", current["created_at"])})
                accounted = digest({key: job.get(key) for key in ("id", "usage", "compute_seconds", "unknown_compute_cost", "attempts")})
            current.update(status=job["status"], job_id=job["id"], usage=job.get("usage", {}),
                           accounting_final=job.get("accounting_final", True),
                           compute_seconds=job.get("compute_seconds", 0), error=job.get("error"),
                           version_id=job.get("version_id"), attempts=job.get("attempts", []), updated_at=job.get("updated_at", now()))
            if accounted:
                current["cost_capture_digest"] = accounted
            if job.get("revision") is not None:
                current["source_job_revision"] = job["revision"]
            if current == self.store.get(grant["id"], "implementation_grant") and (current.get("evidence_reconciled") or
                    not current["request"].get("operation") == "revalidation" and (current["status"] != "completed" or current.get("attached"))):
                return current
            self.store.put("implementation_grant", current, "implementation.updated")
        if current["request"].get("operation") == "revalidation":
            if job["status"] in SETTLED:
                self.cache_version(self.client.version(current["request"]["version_id"]))
                current["evidence_reconciled"] = True
                self.store.put("implementation_grant", current, "implementation.evidence_updated")
            if job["status"] in {"failed", "blocked", "interrupted"}:
                self.workspace.memory.issue(grant["campaign_id"], "implementation_job", job.get("error") or "Revalidation needs attention.",
                    affected=grant["id"], evidence=[job["id"]])
            return current
        if job["status"] == "completed" and current["request"].get("accounting_mode") == "execution_v1":
            # Validation of a sandbox submission publishes evidence, not a
            # campaign selection. Only an explicit attach promotes its version.
            self.cache_version(self.client.version(job["version_id"]))
            current["evidence_reconciled"] = True
            self.store.put("implementation_grant", current, "implementation.validated_unattached")
            return current
        if job["status"] == "completed" and not current.get("attached"):
            if grant.get("task_id"):
                try:
                    task = self.store.get(grant["task_id"], "task")
                    if task.get("evaluator_requirement_id") != grant["evaluator_requirement_id"]:
                        raise ValueError("The problem requirement changed while its evaluator was built")
                    self.workspace.evaluators.attach(task["id"], job["version_id"], authority="manager",
                        rationale="Completed the evaluator commissioned for this frozen requirement")
                    current["attached"] = True
                    self.store.put("implementation_grant", current, "implementation.ready")
                except ValueError as exc:
                    self.workspace.memory.issue(grant["campaign_id"], "implementation_binding", str(exc), affected=grant["id"])
                return current
            hypothesis = self.store.get(grant["hypothesis_id"], "hypothesis")
            if self.hypothesis_digest(hypothesis) == grant["hypothesis_digest"] and hypothesis["status"] not in {"archived", "finalist"}:
                try:
                    self.attach(hypothesis["id"], job["version_id"])
                    current["attached"] = True
                    self.store.put("implementation_grant", current, "implementation.ready")
                except ValueError as exc:
                    self.workspace.memory.issue(grant["campaign_id"], "implementation_binding", str(exc), affected=grant["id"])
            elif hypothesis.get("implementation_version_id") == job["version_id"]:
                current["attached"] = True
                self.store.put("implementation_grant", current)
            else:
                self.workspace.memory.issue(grant["campaign_id"], "implementation_binding",
                    "A validated implementation is available, but the proposal changed while it was built. Review the version before attaching it.", affected=grant["id"])
        elif job["status"] in {"failed", "blocked", "interrupted", "needs_reconciliation"}:
            self.workspace.memory.issue(grant["campaign_id"], "implementation_job", job.get("error") or "Implementation work needs attention.",
                                        affected=grant["id"], evidence=[job["id"]])
        return current

    def reconcile_agent_logs(self, grants):
        if not hasattr(self.client, "agent_log"):
            return
        from optimization_framework.research.log import encode
        import hashlib
        for grant in grants:
            if not grant.get("job_id"):
                continue
            identity = "agent_log_import_" + grant["id"]
            try:
                state = self.store.get(identity, "agent_log_import")
            except KeyError:
                state = {"id": identity, "campaign_id": grant["campaign_id"], "position": 0}
            if state.get("terminal") and state.get("job_revision") == grant.get("source_job_revision"):
                continue
            try:
                page = self.client.agent_log(grant["job_id"], self.workspace_id, grant["id"], after=state["position"])
                source = page["origin_service_id"]
                for event in page["events"]:
                    if event["seq"] != state["position"] + 1:
                        raise ValueError("Implementation trace has a cursor gap")
                    payload = (self.client.agent_payload(grant["job_id"], event["payload_ref"], self.workspace_id, grant["id"])
                               if event.get("payload_ref") else event.get("payload", {}))
                    if hashlib.sha256(encode(payload).encode()).hexdigest() != event["payload_hash"]:
                        raise ValueError("Implementation trace payload hash mismatch")
                    with self.store.transaction():
                        fields = {key: event[key] for key in ("task_id", "call_id", "from_agent", "to_agent", "occurred_at", "message_id", "in_reply_to") if key in event}
                        if event.get("parent_event_id"):
                            fields["origin_parent_event_id"] = event["parent_event_id"]
                            with self.store.connection() as db:
                                parent = db.execute("SELECT event_id FROM agent_events WHERE campaign_id=? AND event_key=?",
                                    (grant["campaign_id"], f"import:{source}:{event['parent_event_id']}")).fetchone()
                            if parent:
                                fields["parent_event_id"] = parent[0]
                        self.workspace.agent_log.record(grant["campaign_id"], event["event_type"],
                            agent_id=event["agent_id"], role=event["role"], summary=event["summary"], payload=payload,
                            event_key=f"import:{source}:{event['event_id']}", origin_service=source,
                            origin_event_id=event["event_id"], job_id=grant["job_id"], grant_id=grant["id"], **fields)
                        state.update(position=event["seq"], error=None)
                        self.store.put("agent_log_import", state)
                state.update(terminal=page["terminal"] and state["position"] == page["latest_seq"],
                             job_revision=page["job_revision"], error=page.get("error"))
            except ValueError as exc:
                state.update(error=str(exc), terminal=False)
                self.workspace.agent_log.record(grant["campaign_id"], "trace.remote_unavailable",
                    summary="Implementation log is unavailable; saved events will reconcile later",
                    payload={"error": str(exc)}, grant_id=grant["id"], job_id=grant["job_id"],
                    event_key=f"trace_error:{grant['id']}:{hashlib.sha256(str(exc).encode()).hexdigest()}")
            self.store.put("agent_log_import", state)

    def reconcile(self):
        grants = self.store.list("implementation_grant")
        self.reconcile_agent_logs(grants)
        cursor_id = "implementation_accounting_events"
        try:
            cursor = self.store.get(cursor_id, "service_event_cursor")["position"]
        except KeyError:
            cursor = 0
        events = []
        if any(grant.get("job_id") for grant in grants) and hasattr(self.client, "events"):
            try:
                events = self.client.events(after=cursor, limit=1000)
            except ValueError as exc:
                self.connection_error = str(exc)
        changed = {event["data"].get("record_id") for event in events}
        clock = time.monotonic()
        sweep = clock >= self._job_sweep_at
        delivery_failed = False
        active = [trial for trial in self.store.list_trials_in_status({"running", "pausing", "stopping"})
                  if trial.get("implementation_version_id") or trial.get("evaluator_version_id")]
        if active:
            try:
                versions = {version["id"]: self.cache_version(version) for version in self.client.versions()}
                for trial in active:
                    for identity in [trial.get("implementation_version_id"), trial.get("evaluator_version_id")]:
                        if not identity:
                            continue
                        version = versions.get(identity)
                        if version is None or version["status"] not in ({"validated", "contract_validated"} if version.get("kind") == "evaluator" else {"validated"}):
                            self.workspace.memory.issue(trial["campaign_id"], "implementation_revoked",
                                "An executable used by this running experiment is no longer available for new work. Its pinned code is unchanged; decide whether to stop or retain the current attempt.",
                                affected=trial["id"], evidence=[identity])
                    if trial.get("evaluator_eligibility"):
                        try:
                            self.workspace.evaluators.check_eligibility(trial)
                        except ValueError as exc:
                            self.workspace.memory.issue(trial["campaign_id"], "evaluator_eligibility",
                                str(exc), affected=trial["id"], evidence=[trial["evaluator_eligibility"].get("waiver_id") or trial["evaluator_eligibility"]["validation_report_id"]])
            except ValueError as exc:
                self.connection_error = str(exc)
        for grant in grants:
            if settled(grant) and grant.get("cost_capture_digest") and grant.get("job_id") not in changed:
                if grant["request"].get("operation") == "revalidation":
                    if grant.get("evidence_reconciled"):
                        continue
                elif grant["status"] != "completed" or grant.get("attached"):
                    continue
            try:
                # Unsent grants belong to the durable outbox. Legacy reservations
                # are upgraded without submitting a second independent request.
                if not grant.get("job_id"):
                    effect_id = "submit_" + grant["id"]
                    try:
                        self.store.get(effect_id, "outbox")
                    except KeyError:
                        self.store.put("outbox", {"id": effect_id, "campaign_id": grant["campaign_id"],
                            "kind": "implementation_submit", "grant_id": grant["id"], "status": "pending", "created_at": now()}, "effect.queued")
                    continue
                if not sweep and grant["job_id"] in self._seen_jobs and grant["job_id"] not in changed:
                    continue
                job = self.client.job(grant["job_id"])
                self._apply_job(grant, job)
                self._seen_jobs.add(grant["job_id"])
                self.connection_error = None
            except ValueError as exc:
                delivery_failed = True
                self.connection_error = str(exc)
                self.workspace.memory.issue(grant["campaign_id"], "implementation_service", str(exc), affected=grant["id"])
        if sweep:
            self._job_sweep_at = clock + 60
        if events and not delivery_failed:
            self.store.put("service_event_cursor", {"id": cursor_id, "position": max(event["id"] for event in events)})

    def control(self, grant_id, action, *, idempotency_key=None):
        grant = self.store.get(grant_id, "implementation_grant")
        if not grant.get("job_id"):
            # Cancellation must be delivered even after an uncertain POST acknowledgement.
            self._apply_job(grant, self.client.submit(grant["request"]))
            grant = self.store.get(grant_id, "implementation_grant")
        return self._apply_job(grant, self.client.control(grant["job_id"], action,
            **({"idempotency_key": idempotency_key} if idempotency_key else {})))

    def deliver_control(self, effect):
        grant = self.store.get(effect["grant_id"], "implementation_grant")
        lookup = getattr(self.client, "control_receipt", None)
        receipt = lookup(grant["job_id"], effect["id"]) if lookup and grant.get("job_id") else None
        if receipt is not None:
            if receipt["job_id"] != grant["job_id"] or receipt["action"] != effect["action"]:
                raise ValueError("The implementation control receipt differs from its requested action")
            self._apply_job(grant, self.client.job(grant["job_id"]))
            effect["receipt_id"] = receipt["id"]
            return
        # Only previously unaccepted work is subject to the current direction.
        # Never resend an old resume merely to discover whether it was accepted.
        self.workspace.check_manager_context(effect["campaign_id"], tuple(effect["expected_context"]))
        self.control(grant["id"], effect["action"], idempotency_key=effect["id"])

    def prepare(self, version_id, task, parameters):
        bundle = self.client.artifact(version_id)
        version, artifact = bundle["version"], bundle["artifact"]
        if version["status"] != "validated":
            raise ValueError("Implementation is not available for new execution: " + version.get("revocation_reason", version["status"]))
        if version["id"] != version_id or digest(artifact) != version["artifact_digest"]:
            raise ValueError("Implementation service returned an inconsistent artifact")
        if version["spec"].get("evaluator_version_id"):
            self.cache_version(self.client.version(version["spec"]["evaluator_version_id"]))
        if not self.validation_current(version):
            raise ValueError("Implementation validation does not match this workspace's evaluator")
        self.compatible(version, task, parameters)
        verify_runtime(bundle_runtime_root(bundle), artifact["runtime"])
        self.cache_version(version)
        bundle["cost_asset_ids"] = [self.cost_asset(bundle)["id"]]
        return bundle

    def resolve_runtime(self, effect):
        from optimization_framework.implementations.runtime_resolution import ResolutionRequest
        request = ResolutionRequest(workspace_id=self.workspace_id, campaign_id=effect["campaign_id"],
            idempotency_key=effect["id"], runtime_digest=effect["runtime_digest"])
        # Recover an already accepted result before checking later guidance.
        # A lost acknowledgement must not turn completed work into new work.
        receipt = self.client.runtime_resolution(self.workspace_id, effect["id"])
        if receipt is None:
            self.workspace.check_manager_context(effect["campaign_id"], tuple(effect["expected_context"]))
            receipt = self.client.resolve_runtime(effect["version_id"], request.model_dump(mode="json"))
        expected = digest({"version_id": effect["version_id"], "request": request.model_dump(mode="json")})
        if (receipt.get("request_hash") != expected or receipt.get("campaign_id") != effect["campaign_id"]
                or receipt.get("version_id") != effect["version_id"] or receipt.get("runtime_digest") != effect["runtime_digest"]
                or receipt.get("status") not in {"available", "unavailable"}):
            raise ValueError("Runtime resolution receipt does not match the accepted command")
        with self.workspace.lock, self.store.transaction():
            self.store.put_immutable("runtime_resolution_receipt", receipt, "runtime.resolution_recorded")
            from optimization_framework.assets.service_costs import runtime_asset
            runtime_asset(self.workspace.assets, receipt)
            if receipt["status"] != "available":
                self.workspace.memory.issue(effect["campaign_id"], "runtime_unavailable", receipt["reason"], affected=effect["id"])
        return receipt

    def cost_asset(self, bundle):
        version = bundle["version"]
        from optimization_framework.assets.service_costs import implementation_asset, runtime_asset, record_implementation_job
        grants = [grant for grant in self.store.list("implementation_grant") if grant.get("version_id") == version["id"]]
        dependencies = sorted({asset for grant in grants for asset in grant.get("upstream_cost_asset_ids", [])})
        accounted_jobs = {grant["job_id"] for grant in grants if "upstream_cost_asset_ids" in grant}
        jobs = bundle.get("production_jobs", [])
        positions = {job["id"]: record_implementation_job(self.workspace.assets, job) for job in jobs}
        # Archived grants do not become runnable local grants. Their immutable
        # production assets still carry declared research inputs and exact costs.
        imported = {row["asset_id"] for row in self.store.list("imported_asset")}
        wanted = {job["id"] for job in jobs} - accounted_jobs
        for asset in sorted(self.store.list("asset"), key=lambda item: (item["cost_provenance"] != "complete", len(item["dependency_ids"]), item["id"])):
            if asset["id"] not in imported:
                continue
            if asset.get("payload", {}).get("implementation_version_id") != version["id"]:
                continue
            covered = set(asset["payload"].get("production_job_ids", [])) & wanted
            if covered:
                graph = self.workspace.assets.contributions([asset["id"]])
                # A matching producer name alone cannot prove that an older
                # imported asset covers the job's currently observed prefix.
                complete = {identity for identity in covered if any(start == 0 and stop >= positions[identity].stop
                    for start, stop in graph["intervals"].get(positions[identity].source_id, []))}
                dependencies.append(asset["id"])
                wanted -= covered
                if not graph["unknown_provenance_asset_ids"]:
                    accounted_jobs.update(complete)
        if bundle.get("runtime_resolution_receipt"):
            receipt = bundle["runtime_resolution_receipt"]
            self.store.put_immutable("runtime_resolution_receipt", receipt, "runtime.resolution_admitted")
            dependencies.append(runtime_asset(self.workspace.assets, receipt)["id"])
        return implementation_asset(self.workspace.assets, version, jobs, dependency_ids=dependencies,
            upstream_complete=bool(jobs) and not bundle.get("missing_production_job_ids") and not bundle.get("missing_runtime_resolution_receipt_id")
                and all(job["id"] in accounted_jobs for job in jobs))

    def check_launch(self, trial):
        """A pinned artifact preserves history; it does not bypass revocation."""
        version_id = trial.get("implementation_version_id")
        if not version_id:
            return
        bundle = self.client.artifact(version_id)
        version = self.cache_version(bundle["version"])
        if version["status"] != "validated":
            raise ValueError("The implementation was revoked before launch: " + version.get("revocation_reason", version["status"]))
        if version["artifact_digest"] != trial["implementation_artifact_digest"]:
            raise ValueError("The pinned implementation identity changed; no worker was started")
        if version["spec"].get("evaluator_version_id"):
            self.cache_version(self.client.version(version["spec"]["evaluator_version_id"]))
        if not self.validation_current(version):
            raise ValueError("Implementation correctness evidence is no longer current; revalidate before starting")
        pin_runtime(self.workspace.job_dir(trial["id"]) / "implementation", bundle)
        self.runtime_contribution(trial, bundle)

    def runtime_contribution(self, trial, bundle):
        receipt = bundle.get("runtime_resolution_receipt")
        if receipt:
            from optimization_framework.assets.service_costs import runtime_asset
            self.store.put_immutable("runtime_resolution_receipt", receipt, "runtime.resolution_admitted")
            asset = runtime_asset(self.workspace.assets, receipt)
            trial["operational_cost_asset_ids"] = sorted(set(trial.get("operational_cost_asset_ids", []) + [asset["id"]]))

    @staticmethod
    def pin(directory, bundle):
        root = directory / "implementation"
        write_package(root / "package", bundle["artifact"]["package"])
        if tree_hashes(root / "package") != bundle["version"]["package_hashes"]:
            raise ValueError("Implementation package snapshot is inconsistent")
        atomic_json(root / "bundle.json", bundle)
        pin_runtime(root, bundle)
        version = bundle["version"]
        return {"implementation_version_id": version["id"], "implementation_artifact_digest": version["artifact_digest"],
                "implementation_runtime_digest": version["runtime_digest"], "validation_report_id": version["validation_report"]["id"],
                "implementation_cost_asset_ids": bundle.get("cost_asset_ids", [])}
