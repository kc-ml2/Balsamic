"""Persist research discussions and convert accepted proposals to concrete work."""
from __future__ import annotations

import threading
import json
from optimization_framework.contracts.base import content_hash
from optimization_framework.contracts.requests import ResearchInput, TrialInput, ControlInput
from optimization_framework.contracts.experiments import DELIBERATE_STOPS
from optimization_framework.storage.sqlite import identifier, now
from optimization_framework.research.providers import api_spend, provider_status


# These researcher-interface controls are not scientific action proposals.
# Explicitly scoped callers can still supply their schemas when needed.
INTERFACE_COMMANDS = {"campaign.create", "context.import", "context.edit", "models.configure", "issue.resolve",
    "decision.resolve", "decision.refresh", "research.control", "research.retry", "finalist.set", "implementation.reference", "implementation.bind_builtin"}


class ResearchCancelled(Exception):
    pass


class ResearchCoordinator:
    def __init__(self, workspace):
        self.workspace = workspace
        self.store = workspace.store
        workspace.on_trial_finished = self.reconsider_finished

    def context(self, campaign_id, question="", *, target_id=None, command_operations=None, decision_refresh_id=None):
        from optimization_framework.evaluation.registry import problems
        from optimization_framework.optimizers.registry import METHODS
        from optimization_framework.implementations.reuse import assess
        if decision_refresh_id and command_operations is None:
            from optimization_framework.campaigns.decisions import refresh_operations
            command_operations = refresh_operations(self.workspace, decision_refresh_id)
        library = self.workspace.implementations.catalog(refresh=True)
        campaign = self.store.get(campaign_id, "campaign")
        releases = self.store.list("confirmation_release", campaign_id)
        released_trials = {identity for release in releases for identity in release["trial_ids"]}
        released_tasks = {identity for release in releases for identity in release["task_ids"]}
        tasks = [{**t, "confirmation_released": t["id"] in released_tasks, "evaluator_readiness": self.workspace.evaluators.readiness(t)}
                 for t in self.workspace.current_tasks(campaign_id) if t["split"] != "test" or t["id"] in released_tasks]
        permitted = {t["id"] for t in tasks}
        trials = [{**t, "confirmation_released": t["id"] in released_trials} for t in self.store.list("trial", campaign_id)
                  if t["task_id"] in permitted and (t.get("task_split") != "test" or t["id"] in released_trials)]
        hypotheses = self.store.list("hypothesis", campaign_id)
        sources = self.store.list("source", campaign_id)
        for h in hypotheses:
            for source in h.get("sources", []):
                if isinstance(source, dict):
                    sources.append(source)
        memory = self.workspace.memory.assemble(campaign_id, question)
        visible_records = self.workspace.memory._records(campaign_id)
        visible_ids = {item["id"] for _, item in visible_records}
        retrieved = {r["id"] for r in memory["retrieved_records"]}
        if len(trials) > 30:
            trials = [t for t in trials if t["id"] in retrieved or t in trials[-20:]
                or campaign.get("active_study_id") and t.get("study_id") == campaign["active_study_id"]
                or t["status"] in {"queued", "running", "paused"}]
        def working_curve(trial_id):
            # Model prompts already select at most 40 observed points. Project
            # scalar measurements before compiling the context, so candidate
            # archives in every journal row cannot consume hundreds of MB.
            rows = self.workspace.metrics(trial_id, fields=("step", "evaluations", "solver_calls", "elapsed_seconds",
                "best_objective", "objective", "best_efficiency", "efficiency", "cache_hits", "budget_requests",
                "unknown_solver_cost", "unknown_worker_cost", "confirmed_observations", "interrupted_requests"))
            return [rows[round(i * (len(rows) - 1) / 39)] for i in range(40)] if len(rows) > 40 else rows

        trials = [{**{key: value for key, value in t.items() if key != "execution_manifest"},
            **({"execution_manifest_digest": content_hash(t["execution_manifest"])} if t.get("execution_manifest") else {}),
            "curve": working_curve(t["id"]),
            "curve_projection": "At most 40 evenly indexed observed scalar measurements, including endpoints; "
                "full journal, candidates and diagnostics remain in the saved experiment."} for t in trials]
        context = {"campaign": {**campaign, "charter": campaign}, "tasks": tasks, "trials": trials,
                "active_study": self.store.get(campaign["active_study_id"], "study") if campaign.get("active_study_id") else None,
                "problem_definitions": list({(t["problem"]["definition_id"], t["problem"]["evaluator_version"]):
                    self.workspace.evaluators.describe_task(t).model_dump(mode="json") for t in tasks if t.get("problem")}.values()),
                "available_methods": METHODS,
                "hypotheses": hypotheses, "decisions": [item for kind, item in visible_records if kind == "decision"],
                "evidence_library": [source for source in sources if source.get("id") in visible_ids],
                "history": [item for kind, item in visible_records if kind == "message"][-20:],
                "validation_requirements": [self.workspace.validations.assess(item["id"])
                    for kind, item in visible_records if kind == "validation_requirement"],
                "reuse_decisions": [item for kind, item in visible_records if kind == "reuse_decision"],
                "reference_evidence": self.workspace.assets.manager_references(campaign_id, campaign.get("active_study_id")),
                "applicable_assets": [json.loads(self.workspace.memory._text(kind, item))
                    for kind, item in visible_records if kind == "asset"],
                "application_commands": {key: value for key, value in self.workspace.commands.describe().items()
                    if key in command_operations} if command_operations is not None else {
                        key: value for key, value in self.workspace.commands.describe().items()
                        if not key.startswith("discovery.") and key not in INTERFACE_COMMANDS},
                "experiment_drafts": [{"draft": item, "readiness": self.workspace.drafts.readiness(item["id"])}
                    for kind, item in visible_records if kind == "experiment_draft"],
                "historical_reproduction_sources": [{key: item[key] for key in
                    ("reference", "key", "algorithm", "task_name", "seed", "compatible_task_ids", "unsupported_reason")}
                    for item in self.workspace.reproductions.sources(campaign_id)],
                "reproduction_comparisons": [item for kind, item in visible_records if kind == "reproduction_comparison"],
                "manager_context": memory,
                "implementation_readiness": {h["id"]: self.workspace.implementations.readiness(h) for h in hypotheses},
                "implementation_library_error": library["connection_error"],
                "available_implementations": [{"id": v["id"], "name": v["name"], "status": v["status"], "spec": v["spec"],
                    "validation": {key: v["validation_report"].get(key) for key in ("id", "kind", "passed", "scope", "limitations")},
                    "candidate_assessments": ({t["id"]: assess(self.workspace.implementations, campaign_id, v, task_id=t["id"]) for t in tasks}
                        if v.get("kind") == "evaluator" else {h["id"]: assess(self.workspace.implementations, campaign_id, v, hypothesis_id=h["id"]) for h in hypotheses})}
                    for v in library["versions"]]}
        if command_operations is None:
            context["application_command_scope"] = {"reason": "Scientific action proposal schemas are supplied. "
                "Campaign creation, model settings, memory editing, inbox decisions and research controls belong to the "
                "researcher interface; guide the user to those controls instead of inventing omitted payloads.",
                "omitted_operations": sorted(INTERFACE_COMMANDS)}
        if decision_refresh_id:
            from optimization_framework.campaigns.decisions import refresh_context
            context["decision_refresh"] = refresh_context(self.workspace, decision_refresh_id, campaign_id)
            context["application_command_scope"] = {
                "reason": "Reconsideration includes full schemas for selected historical actions and scientific follow-up. "
                    "Other operations require a separately scoped design turn; do not invent their payloads.",
                "omitted_operations": sorted(set(self.workspace.commands.describe()) - set(context["application_commands"]))}
        from .context import bounded
        return bounded(context, question=question, target_id=target_id)

    def validate_request(self, request):
        """Validate admission without allocating a turn or executing a provider."""
        self.store.get(request.campaign_id, "campaign")
        if request.proposal_operation:
            pi_owned = self.workspace.pi.owns(request.campaign_id)
            discovery = getattr(self.workspace, "discovery", None)
            session = discovery.active(request.campaign_id) if discovery else None
            if not session and not pi_owned:
                raise ValueError("Start an optimizer discovery session in Research notebook before requesting new proposals")
            for identity in request.parent_hypothesis_ids:
                parent = self.store.get(identity, "hypothesis")
                if parent["campaign_id"] != request.campaign_id or parent.get("status") == "archived":
                    raise ValueError("Select active parent proposals from this campaign")
                if parent.get("candidate_id") and not pi_owned:
                    discovery._evidence(session, parent["candidate_id"])
        target = self.store.get(request.hypothesis_id, "hypothesis") if request.hypothesis_id else None
        if target and target["campaign_id"] != request.campaign_id:
            raise ValueError("The selected idea must belong to this campaign")
        if request.feedback_review_ids and (request.mode != "evolve" or target is None):
            raise ValueError("Feedback comments require a revision of a selected idea")
        if target and request.mode == "evolve":
            if target.get("status") == "archived":
                raise ValueError("Revive this idea before requesting a revision")
            notes = [note for note in target.get("reviews", []) if note.get("author") == "researcher"]
            if set(request.feedback_review_ids) - {note["id"] for note in notes}:
                raise ValueError("Select saved researcher comments belonging to this idea")
            return [dict(note) for note in notes if not request.feedback_review_ids or note["id"] in request.feedback_review_ids]
        return None

    def start(self, request: ResearchInput, automatic=False, command_id=None, feedback_snapshot=None, input_ids=None, dispatch=True, decision_refresh_id=None):
        with self.workspace.lock:
            if command_id:
                previous = next((r for r in self.store.list("research_run", request.campaign_id) if r.get("manager_command_id") == command_id), None)
                if previous:
                    return self.public_run(previous)
            campaign = self.store.get(request.campaign_id, "campaign")
            target = None
            if request.hypothesis_id:
                target = self.store.get(request.hypothesis_id, "hypothesis")
                if target["campaign_id"] != request.campaign_id:
                    raise ValueError("The selected idea must belong to this campaign")
            if request.feedback_review_ids and (request.mode != "evolve" or target is None):
                raise ValueError("Feedback comments require a revision of a selected idea")
            selected_feedback = []
            if target is not None and request.mode == "evolve":
                if target.get("status") == "archived":
                    raise ValueError("Revive this idea before requesting a revision")
                notes = [note for note in target.get("reviews", []) if note.get("author") == "researcher"]
                requested_ids = set(request.feedback_review_ids)
                if requested_ids - {note["id"] for note in notes}:
                    raise ValueError("Select saved researcher comments belonging to this idea")
                selected_feedback = [note for note in notes if not requested_ids or note["id"] in requested_ids]
                if feedback_snapshot is not None:
                    selected_feedback = feedback_snapshot
            runs = self.store.list("research_run", request.campaign_id)
            if any(r["status"] in {"running", "stopping"} for r in runs):
                raise ValueError("This campaign already has an active research discussion; let it finish or stop it first")
            spent = sum(api_spend(r.get("usage")) for r in runs)
            remaining = max(0, campaign["llm_budget_usd"] - spent - self.workspace.implementations.api_committed(request.campaign_id))
            context = self.context(request.campaign_id, request.message, target_id=request.hypothesis_id,
                **({"decision_refresh_id": decision_refresh_id} if decision_refresh_id else {}))
            if target is not None and request.mode == "evolve":
                context["revision_context"] = {"hypothesis_id": target["id"], "reviews": selected_feedback}
            payload = request.model_dump()
            payload["llm_budget_usd"] = remaining
            payload["provider_snapshot"] = provider_status()
            payload["model_policy"] = self.workspace.models.snapshot(request.campaign_id)
            record = {"id": identifier("research"), "campaign_id": request.campaign_id,
                      "charter_version": campaign["version"], "request": payload, "context_snapshot": context,
                      "status": "running", "created_at": now(), "trace": [], "usage": {},
                      "checkpoint": None, "automatic": automatic, "control_revision": 0}
            record["manager_command_id"] = command_id
            if decision_refresh_id:
                record["decision_refresh_id"] = decision_refresh_id
            record["manager_input_ids"] = input_ids or []
            record["dispatch_phase"] = "queued"
            record["guidance_revision"] = self.workspace.memory.state(request.campaign_id)["guidance_revision"]
            entries = [("research_run", record, "research.started")]
            try:
                self.store.get("message_" + (command_id or record["id"]), "message")
            except KeyError:
                entries.append(("message", {"id": "message_" + (command_id or record["id"]), "campaign_id": request.campaign_id,
                    "role": "user", "content": request.message, "mode": request.mode, "created_at": now(),
                    "research_run_id": record["id"], "automatic": automatic}, "message.created"))
            self.store.put_many(entries)
            if decision_refresh_id:
                from .decision_review import prepare
                record = prepare(self, record)
            if dispatch:
                self._thread(record)
            return self.public_run(record)

    def _thread(self, record):
        if self.workspace.shutdown_event.is_set():
            return
        previous = self.workspace.research_threads.get(record["id"])
        if previous and previous.is_alive():
            return
        if not record.get("result_id"):
            campaign = self.store.get(record["campaign_id"], "campaign")
            if (campaign["version"] != record["charter_version"] or
                    self.workspace.memory.state(record["campaign_id"])["guidance_revision"] != record.get("guidance_revision", 0)):
                record.update(status="interrupted", error="Campaign guidance changed before this turn could dispatch; reconsider using current context.")
                if record.get("decision_review"):
                    record["decision_review"].update(phase="partial", budget_hold_usd=0.0)
                self.store.put("research_run", record, "research.stale_result")
                return
        if not record.get("result_id"):
            record["cost_work_revision"] = record.get("cost_work_revision", 0) + 1
        record["dispatch_phase"] = "dispatched"
        self.store.put("research_run", record, "research.attempt_started")
        thread = threading.Thread(target=self._run, args=(record["id"],), daemon=True,
                                  name=f"research-{record['id']}")
        self.workspace.research_threads[record["id"]] = thread
        thread.start()

    @staticmethod
    def public_run(record):
        return {k: v for k, v in record.items() if k not in {"context_snapshot", "checkpoint", "resume_fingerprint"}}

    def _emit(self, run_id, event):
        # Commit returned-call receipts and rejection reasons before propagating
        # cancellation. Raising inside the transaction would discard both.
        with self.workspace.lock, self.store.transaction():
            cancelled = self._emit_locked(run_id, event)
        if cancelled:
            raise ResearchCancelled()

    def _emit_locked(self, run_id, event):
        run = self.store.get(run_id, "research_run")
        if event["type"].startswith("provider_") or event["type"] in {"role_started", "role_completed", "role_failed"}:
            discovery_task = self.store.get(run["discovery_task_id"], "discovery_task") if run.get("discovery_task_id") else None
            logged = self.workspace.agent_log.capture(run["campaign_id"], run.get("discovery_task_id", run_id), event,
                guidance_revision=run.get("guidance_revision"), context_revision=run.get("charter_version"), research_run_id=run_id,
                discovery_session_id=run.get("discovery_session_id"), attempt_id=(discovery_task or {}).get("attempt_id"))
            if event["type"] == "role_completed" and logged:
                self.workspace.agent_log.record(run["campaign_id"], "message.handoff",
                    agent_id=f"{run_id}:{event['role']}", role=event["role"], task_id=run_id,
                    parent_event_id=logged["event_id"], from_agent=f"{run_id}:{event['role']}",
                    to_agent="campaign_manager", payload=event.get("result", {}), summary="Specialist result returned to the campaign manager")
        # A returned response is durable even when cancellation arrived during
        # inference. Cancellation is checked before the next dispatch instead.
        if event["type"] in {"provider_response", "provider_error", "role_completed", "role_failed"}:
            return
        if event["type"] == "provider_call_cancelled_before_send":
            run["usage"] = event["usage"]
            self.store.put("research_run", run, "research.reservation_released")
            return
        if event["type"] == "research_completed":
            from optimization_framework.research.lifecycle import save_result
            save_result(self.workspace, run_id, event["result"])
            return
        if run["status"] == "stopping" or self.workspace.shutdown_event.is_set():
            # Preserve completed-call usage even when interruption arrives in flight.
            if event["type"] == "research_checkpoint":
                run["usage"] = event["state"].get("usage", {})
                run["checkpoint"] = event["state"]
                run["resume_fingerprint"] = event["fingerprint"]
                self.store.put("research_run", run)
            return True
        if event["type"] == "provider_call_reserved":
            campaign = self.store.get(run["campaign_id"], "campaign")
            other_cost = sum(api_spend(other.get("usage"))
                for other in self.store.list("research_run", run["campaign_id"]) if other["id"] != run_id)
            other_cost += self.workspace.implementations.api_committed(run["campaign_id"])
            other_cost += sum((other.get("decision_review") or {}).get("budget_hold_usd", 0)
                for other in self.store.list("research_run", run["campaign_id"]) if other["id"] != run_id)
            if event["usage"].get("billing_mode") != "subscription" and other_cost + api_spend(event["usage"]) > campaign["llm_budget_usd"] + 1e-9:
                run["error"] = "The next call exceeds the current campaign funds after other discussions. No request was sent."
                self.store.put("research_run", run, "research.budget_blocked")
                return True
            run["usage"] = event["usage"]
            run["current_role"] = event["role"]
            self.store.put("research_run", run, "research.cost_reserved")
            return
        elif event["type"] == "provider_progress":
            return
        elif event["type"] == "provider_request":
            return
        elif event["type"] == "research_checkpoint":
            run.update(checkpoint=event["state"], resume_fingerprint=event["fingerprint"],
                       usage=event["state"].get("usage", {}), trace=event["state"].get("trace", []))
        elif event["type"] == "role_started":
            run["current_role"] = event["role"]
        self.store.put("research_run", run, "research.progress")

    def _run(self, run_id):
        from optimization_framework.research.engine import run_research
        from optimization_framework.research.lifecycle import save_result, finalize, reconciliation
        run = self.store.get(run_id, "research_run")
        if run.get("decision_review"):
            from .decision_review import run as run_decision_review
            return run_decision_review(self, run_id)
        request = dict(run["request"])
        if run.get("checkpoint"):
            request.update(resume_state=run["checkpoint"], resume_fingerprint=run["resume_fingerprint"])
        try:
            try:
                saved = self.store.get("research_result_" + run_id, "research_result")
            except KeyError:
                result = run_research(request, run["context_snapshot"], lambda e: self._emit(run_id, e))
                saved = save_result(self.workspace, run_id, result)
            finalize(self, run_id, saved)
            self.workspace.dispatch_outbox()
        except ResearchCancelled:
            with self.workspace.lock, self.store.transaction():
                run = self.store.get(run_id, "research_run")
                if (run.get("usage") or {}).get("pending_reservation"):
                    reconciliation(self.workspace, run)
                else:
                    run.update(status="interrupted" if self.workspace.shutdown_event.is_set() else "stopped", finished_at=now())
                    self.store.put("research_run", run, "research.stopped")
        except Exception as exc:
            with self.workspace.lock, self.store.transaction():
                run = self.store.get(run_id, "research_run")
                if (run.get("usage") or {}).get("pending_reservation"):
                    reconciliation(self.workspace, run)
                else:
                    run.update(status="interrupted" if run.get("result_id") else "failed",
                        error=f"Research workflow failed ({type(exc).__name__}); completed role evidence is retained.", finished_at=now())
                    self.store.put("research_run", run, "research.failed")
                self.workspace.memory.issue(run["campaign_id"], "manager_run", run["error"], affected=run_id)
        finally:
            try:
                self.capture_costs(self.store.get(run_id, "research_run"))
            except Exception as exc:
                self.workspace.memory.issue(run["campaign_id"], "model_cost_capture", str(exc), affected=run_id)

    def capture_costs(self, run):
        from optimization_framework.assets.service_costs import capture_research
        return capture_research(self.workspace, run)

    def _save_critiques(self, run, result):
        """Attach actual agent assessments to the idea that was reviewed."""
        request = run["request"]
        target_id = request.get("hypothesis_id")
        if not target_id or request.get("mode") not in {"review", "evolve"} or result.get("mode") != "llm":
            return
        target = self.store.get(target_id, "hypothesis")
        reviews = target.setdefault("reviews", [])
        existing = {review["id"] for review in reviews}
        role_results = (result.get("research_state") or {}).get("role_results", [])
        changed = False
        for index, assessment in enumerate(role_results):
            role = assessment.get("role")
            text = assessment.get("analysis", "").strip()
            review_id = f"{run['id']}_critique_{index}"
            if role not in {"assumption_reviewer", "comparative_reviewer"} or not text or review_id in existing:
                continue
            reviews.append({"id": review_id, "author": "agent", "role": role.replace("_", " ").capitalize(),
                            "text": text, "created_at": now(), "research_run_id": run["id"],
                            "model": result.get("provider", {}).get("model"), "origin": "llm"})
            changed = True
        if changed:
            self.store.put("hypothesis", target, "hypothesis.critiqued")

    def reconsider_finished(self):
        """One event-driven reconsideration per completed batch, bounded by delegation."""
        with self.workspace.lock:
            for campaign in self.store.list("campaign"):
                if campaign["autonomy"] != "delegated":
                    continue
                trials = self.store.list("trial", campaign["id"])
                pending = [t for t in trials if t.get("research_pending") and t["algorithm"] != "validate"]
                if not pending or any(t["status"] in {"running", "queued", "pausing", "stopping"} for t in trials):
                    continue
                if any(r["status"] in {"running", "stopping"} for r in self.store.list("research_run", campaign["id"])):
                    continue
                if any(d["status"] == "pending" and (not d.get("action_id") or d.get("blocking_scope") == "campaign")
                       for d in self.store.list("decision", campaign["id"])):
                    continue
                # A researcher's stop is a direction to leave that branch stopped.
                current_tasks = {t["id"] for t in self.workspace.current_tasks(campaign["id"])
                                 if t["split"] != "test"}
                eligible = [t for t in pending if t.get("stopped_by") not in DELIBERATE_STOPS
                            and t.get("task_split") != "test" and t["task_id"] in current_tasks
                            and t["charter_version"] == campaign["version"]]
                for trial in pending:
                    trial["research_pending"] = False
                    self.store.put("trial", trial)
                if eligible:
                    ids = ", ".join(t["id"] for t in eligible)
                    self.start(ResearchInput(campaign_id=campaign["id"], mode="plan", max_calls=3,
                        message=f"New development evidence from trials {ids}. Reconsider the most informative next action. Do not repeat an identical probe; ask the researcher if startup, numerical reliability, or budget prevents a meaningful decision."), automatic=True)

    def _save_decision(self, run, decision):
        for previous in self.store.list("decision", run["campaign_id"]):
            if (previous["charter_version"] == run["charter_version"] and
                    previous.get("title") == decision.get("title") and
                    previous.get("trial_id") == decision.get("trial_id") and
                    not decision.get("action_id")):
                if decision.get("decision_format") == "structured" and (
                        previous.get("guidance_revision") != run.get("guidance_revision", 0) or
                        any(previous.get(key) != decision.get(key) for key in
                            ("decision_format", "background", "proposal", "options", "recommendation", "recommendation_reason"))):
                    continue  # A clarified/current question is not the old opaque choice.
                return  # Keep the researcher's existing answer instead of asking again.
        options = []
        for index, choice in enumerate(decision.get("options", [])):
            options.append(choice if isinstance(choice, dict) else {"id": str(index), "label": choice, "description": ""})
        decision.update(campaign_id=run["campaign_id"], charter_version=run["charter_version"], guidance_revision=run.get("guidance_revision", 0),
                        research_run_id=run["id"], created_at=now(), options=options,
                        context=decision.get("context", decision.get("rationale", "")),
                        recommendation=decision.get("recommendation", str(decision.get("recommended_option", 0))))
        if run.get("decision_refresh_id"):
            decision["decision_refresh_id"] = run["decision_refresh_id"]
        self.store.put("decision", decision, "decision.created")

    def control(self, run_id, action):
        from optimization_framework.contracts.commands import Command
        run = self.store.get(run_id, "research_run")
        campaign = self.store.get(run["campaign_id"], "campaign")
        accepted = self.workspace.commands.execute(Command(id=identifier("research_control"),
            campaign_id=campaign["id"], operation="research.control", expected_revision=campaign["version"],
            payload={"run_id": run_id, "action": action,
                     "expected_control_revision": run.get("control_revision", 0)}))
        return accepted["outcome"]["research_run"]

    def resolve(self, decision_id, choice, comment):
        from optimization_framework.contracts.commands import Command
        decision = self.store.get(decision_id, "decision")
        campaign = self.store.get(decision["campaign_id"], "campaign")
        self.workspace.commands.execute(Command(id=identifier("decision_resolve"),
            campaign_id=campaign["id"], operation="decision.resolve", expected_revision=campaign["version"],
            payload={"decision_id": decision_id, "choice": choice, "comment": comment,
                     "expected_resolution_revision": decision.get("resolution_revision", 0)}))
        decision = self.store.get(decision_id, "decision")
        if decision["status"] == "pending" and decision.get("delivery_error"):
            raise ValueError(decision["delivery_error"])
        return decision

    def _finish_decision(self, decision, choice, comment, outcome):
        decision.update(status="resolved", choice=choice, comment=comment, resolved_at=now(), outcome=outcome)
        self.store.put("decision", decision, "decision.resolved")
        label = next((o["label"] for o in decision["options"] if o["id"] == choice), choice)
        self.store.put("message", {"id": identifier("message"), "campaign_id": decision["campaign_id"],
            "role": "user", "content": f"Decision: {decision['title']}\nChoice: {label}\n{comment}", "created_at": now()}, "message.created")
        return decision

    def execute_action(self, action, *, actor="researcher", approval=None):
        from optimization_framework.contracts.base import content_hash
        from optimization_framework.contracts.commands import Command
        proposal_digest = content_hash({key: value for key, value in action.items()
            if key not in {"status", "outcome", "allocation_issue"}})
        command_id = "action_" + action["id"]
        try:
            previous = self.store.get(command_id, "work_command")
        except KeyError:
            previous = None
        if previous:
            if previous["request"].get("proposal_digest") != proposal_digest or previous["actor"] != actor:
                raise ValueError("This action identity belongs to another proposal or authority")
            return previous["outcome"]
        if actor == "manager" and action.get("requires_researcher"):
            raise ValueError("This action explicitly requires a researcher decision before execution")
        campaign = self.store.get(action["campaign_id"], "campaign")
        if action.get("charter_version", campaign["version"]) != campaign["version"]:
            raise ValueError("This recommendation used an older charter; request a current proposal")
        if approval is not None and actor != "researcher":
            raise ValueError("Only a recorded researcher decision can refresh approval authority")
        expected_guidance = (approval or {}).get("guidance_revision",
            action.get("guidance_revision", self.workspace.memory.state(campaign["id"])["guidance_revision"]))
        if expected_guidance != self.workspace.memory.state(campaign["id"])["guidance_revision"]:
            raise ValueError("Researcher guidance changed; ask the manager to update this action")
        def submit(operation, payload):
            command = Command(id=command_id, campaign_id=campaign["id"], operation=operation,
                expected_revision=action.get("charter_version", campaign["version"]),
                expected_guidance_revision=expected_guidance,
                expected_authority_hash=(approval or {}).get("authority_hash", action.get("authority_hash", self.workspace.commands.authority_hash(campaign))),
                proposal_digest=proposal_digest, payload=payload)
            return self.workspace.commands.execute(command, actor=actor)["outcome"]

        if action["kind"] == "command":
            if not action.get("command_operation"):
                raise ValueError("The manager proposal needs a typed application command")
            return submit(action["command_operation"], action.get("command_payload") or {})
        hypothesis = self.store.get(action["hypothesis_id"], "hypothesis") if action.get("hypothesis_id") else None
        if action["kind"] == "probe":
            from optimization_framework.research.action_policy import probe_execution_issue
            issue = probe_execution_issue(action, autonomous=actor == "manager")
            if issue:
                raise ValueError(issue)
            task = self.store.get(action["task_id"], "task")
            if task["split"] == "test":
                raise ValueError("Exploratory proposals cannot access test tasks")
            algorithm = action.get("algorithm") or (hypothesis or {}).get("algorithm")
            if not algorithm:
                raise ValueError("The selected hypothesis has no executable algorithm; design or implement it before probing")
            if hypothesis and action.get("algorithm") and action["algorithm"] != hypothesis.get("algorithm"):
                raise ValueError("The explicit probe algorithm conflicts with its saved hypothesis")
            config = action.get("algorithm_config")
            if config is None:
                config = (hypothesis or {}).get("algorithm_config", {})
            proposed_steps = action["budget_calls"]
            seed = action.get("seed", 0)
            if any(t["task_id"] == task["id"] and t["algorithm"] == algorithm and t["seed"] == seed
                   and t["max_steps"] == proposed_steps and t["algorithm_config"] == config
                   for t in self.store.list("trial", campaign["id"])):
                raise ValueError("An identical probe already exists; choose another seed, task, budget, or strategy")
            return submit("trial.create", {"task_id": task["id"], "algorithm": algorithm, "algorithm_config": config,
                "hypothesis_id": hypothesis["id"] if hypothesis else None, "max_steps": proposed_steps,
                "seed": seed, "wall_seconds": action.get("wall_seconds") or campaign["delegated_trial_seconds"], "question": action["question"]})
        if action["kind"] == "nominate" and hypothesis:
            return submit("hypothesis.nominate", {"hypothesis_id": hypothesis["id"]})
        if action["kind"] == "search":
            return submit("literature.search", {"query": action["question"][:500]})
        if action["kind"] == "implement" and hypothesis:
            if action.get("implementation_version_id"):
                return submit("implementation.attach", {"hypothesis_id": hypothesis["id"], "version_id": action["implementation_version_id"]})
            if action.get("implementation_spec") and action.get("implementation_compute_seconds"):
                return submit("implementation.commission", {"hypothesis_id": hypothesis["id"], "spec": action["implementation_spec"],
                    "compute_seconds": action["implementation_compute_seconds"], "max_calls": action.get("implementation_max_calls", 12),
                    "api_budget_usd": action.get("implementation_api_budget_usd", 0)})
            issue = self.workspace.memory.issue(campaign["id"], "implementation_request",
                "Implementation needs a concrete specification and a bounded implementation allocation. Review the commissioning form with the campaign manager.",
                affected=hypothesis["id"], evidence=[action["id"]])
            return {"issue_id": issue["id"], "hypothesis_id": hypothesis["id"]}
        mode = {"review": "review", "compare": "compare", "evolve": "evolve"}.get(action["kind"])
        if mode:
            return submit("research.start", {"mode": mode,
                "hypothesis_id": (hypothesis or {}).get("id"), "message": action["question"] + "\n" + action["rationale"]})
        raise ValueError("This action needs an explicit experiment configuration or researcher direction; use the experiment controls")
