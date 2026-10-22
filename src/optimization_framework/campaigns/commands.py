"""Idempotent command outcomes commit with allocations and domain records."""
from optimization_framework.contracts.base import content_hash
from optimization_framework.contracts.commands import Command, ReuseInput, WaiverInput, FindingInput, CommissionInput, AttachInput, SearchInput, ExecuteValidationInput, ConfirmationReleaseInput, ConfirmationScheduleInput
from optimization_framework.contracts.requests import CampaignInput, CampaignUpdate, TrialInput, ControlInput, StudyInput, RecipeInput, HypothesisInput
from optimization_framework.contracts.assets import ReuseDecision, CostReconcileInput
from optimization_framework.contracts.validation import Waiver, WaiverRevocation
from optimization_framework.storage.sqlite import now
from optimization_framework.contracts.study_rules import NominateInput
from optimization_framework.contracts.drafts import DraftSaveInput, DraftLaunchInput
from optimization_framework.contracts.reproduction import ReproductionDraftInput, ReproductionCompareInput
from optimization_framework.contracts.templates import TemplateFreezeInput, ExecutionInput
from optimization_framework.contracts.references import ReferenceImportInput
from optimization_framework.contracts.bundles import BundleExportInput, BundleInspectInput, BundlePublishInput
from optimization_framework.contracts.commands import EvaluatorCommissionInput, EvaluatorAttachInput, ImplementationControlInput, WaiverRevocationInput, RevalidationInput, ExecutableReuseInput, RuntimeResolutionInput
from optimization_framework.contracts.commands import CampaignUpdateInput, ContextEditInput, IssueResolveInput, TrialControlInput, TrialValidationInput
from optimization_framework.contracts.commands import TrialExtensionRequestInput, JournalCompactInput, JournalCompactionRequestInput
from optimization_framework.contracts.commands import ApprovalRequestInput, WorkerCeilingInput
from optimization_framework.contracts.commands import HypothesisReviewInput, HypothesisStatusInput, HypothesisNominateInput
from optimization_framework.contracts.commands import DecisionResolveInput, SourceRecordInput, SourceIngestInput
from optimization_framework.contracts.commands import InferenceRunInput, FinalistSetInput
from optimization_framework.contracts.commands import ComparisonReportInput, RuntimeProfileInput, EquivalenceInput, TrialContinueInput
from optimization_framework.contracts.commands import AssetSnapshotInput
from optimization_framework.contracts.racing import RaceCreateInput, RaceControlInput, RaceDecisionInput


DELEGATED = {"trial.create", "draft.save", "draft.launch", "reproduction.draft", "reproduction.compare", "study.nominate", "validation.run", "validation.require", "validation.execute", "validation.waive", "inference.run", "asset.reuse", "comparison.report", "finding.record",
             "bundle.export", "bundle.inspect", "bundle.publish", "cost.reconcile",
             "implementation.commission", "implementation.attach", "evaluator.commission", "evaluator.attach", "implementation.control", "implementation.revalidate", "implementation.reuse", "implementation.resolve_runtime", "literature.search", "source.ingest", "confirmation.schedule", "confirmation.validate", "confirmation.release", "asset.import_reference_set"}


DELEGATED.update({"fixed_mask.run", "hypothesis.create", "hypothesis.review", "hypothesis.status",
                  "trial.control", "study.create", "study.activate", "implementation.bind_builtin"})
DELEGATED.update({"study.race.control", "study.race.decide"})
DELEGATED.add("asset.snapshot")
DELEGATED.add("trial.extension_request")
DELEGATED.update({"runtime.profile", "evaluator.equivalence", "trial.continue"})
# The lead asks; removing recorded designs is the researcher's decision (journal.compact is not delegated).
DELEGATED.add("journal.compaction_request")
# Limits, waivers and the worker ceiling are the researcher's: an agent asks, and approval applies the change.
DELEGATED.add("approval.request")
# The experiment operator keeps the experiment rules and takes runs it superseded off the Experiments page.
DELEGATED.update({"experiments.rules", "trial.hide"})
# The resource planner's plan and estimates: they hold runs back or say what a kind is expected to use, inside the researcher's level.
DELEGATED.update({"resources.plan", "resources.estimate"})
# The lead works the evolution graph on request (docs/ui-workspace.md): allocations, batched interventions, new rounds, library reuse.
DELEGATED.update({"hypothesis.allocate", "intervention.batch", "research.generate", "library.apply"})


class CommandService:
    def __init__(self, workspace):
        self.workspace = workspace
        self.store = workspace.store
        # Operations the service layer adds (threads, notes, interventions, autopilot, library): handler(command, actor) -> outcome.
        self.handlers, self.models = {}, {}
        self.delegated = set(DELEGATED)
        # Called with the receipt after a command completes (outside the transaction): lab events, placeholders.
        self.listeners = []
        # Checked before an agent's command runs: guard(command, actor, campaign) raises to refuse (autopilot's cap).
        self.guards = []
        # Set by the service layer: thread_revision(command) -> the current revision of the command's origin thread.
        self.thread_revision = None

    def register(self, operation, handler, *, model=None, delegable=False):
        """Add an operation to the bus. Its name must be a declared Operation (contracts/commands.py)."""
        from typing import get_args
        from optimization_framework.contracts.commands import Operation
        if operation not in get_args(Operation):
            raise ValueError(f"{operation} is not a declared command operation")
        self.handlers[operation] = handler
        if model is not None:
            self.models[operation] = model
        if delegable:
            self.delegated.add(operation)

    def authority_hash(self, campaign):
        return content_hash({key: campaign.get(key) for key in ("version", "active_study_id", "autonomy", "compute_budget_seconds",
            "delegated_trial_seconds", "llm_budget_usd", "implementation_compute_budget_seconds")})

    def delivery(self, identity):
        """Read current delivery without altering the immutable admission receipt."""
        command = self.store.get(identity, "work_command")
        pending = {"trial": {"queued", "running", "pausing", "stopping"},
            "study_execution": {"running", "closing"}, "outbox": {"pending"},
            "manager_command": {"queued", "waiting_provider", "dispatched"}, "research_run": {"running", "stopping"}, "action": {"proposed"},
            "implementation_grant": {"reserved", "queued", "submitted", "running", "building", "validating", "reviewing", "repairing", "stopping"},
            "bundle_operation": {"queued", "capturing", "inspecting", "publishing"}}
        targets = {"trial_id": "trial", "trial_ids": "trial", "execution_id": "study_execution", "effect_id": "outbox",
            "manager_command_id": "manager_command", "run_id": "research_run", "grant_id": "implementation_grant", "operation_id": "bundle_operation", "action_id": "action"}
        queue = [(targets[key], value) for key, values in command["outcome"].items() if key in targets
                 for value in (values if isinstance(values, list) else [values])]
        rows, seen = [], set()
        while queue:
            kind, key = queue.pop(0)
            if not key or key in seen:
                continue
            seen.add(key)
            record = self.store.get(key, kind)
            if kind == "manager_command" and record.get("research_run_id"):
                queue.append(("research_run", record["research_run_id"]))
            if kind == "outbox":
                queue.extend((targets[field], record[field]) for field in ("grant_id", "run_id", "action_id") if record.get(field))
            if kind == "research_run":
                queue.extend(("outbox", effect["id"]) for effect in self.store.list("outbox", record["campaign_id"])
                    if effect["kind"] == "manager_action" and effect["run_id"] == key)
            if kind == "action":
                queue.extend((targets[field], value) for field, values in (record.get("outcome") or {}).items() if field in targets
                    for value in (values if isinstance(values, list) else [values]))
            status = record["status"]
            is_pending = status in pending[kind] and not (kind == "manager_command" and record.get("research_run_id"))
            rows.append({"kind": kind, "id": key, "status": status, "pending": is_pending,
                "active": status in {"running", "pausing", "stopping", "building", "validating", "reviewing", "repairing", "capturing", "inspecting", "publishing"},
                **{field: record[field] for field in ("error", "reason") if record.get(field)}})
        issues = [issue["id"] for issue in self.store.list("manager_issue", command["campaign_id"])
                  if issue["status"] == "pending" and issue.get("affected") in seen]
        return {"command_id": identity, "resources": rows, "pending": any(row["pending"] for row in rows),
            "active": any(row["active"] for row in rows), "requires_attention": bool(issues) or
                any(row["status"] in {"failed", "blocked", "interrupted", "needs_reconciliation", "waiting_provider"} for row in rows),
            "issue_ids": issues}

    def describe(self):
        """Use the application's schemas in prompts instead of a second tool model."""
        from .experiment_rules import ExperimentRulesInput, TrialHideInput
        from optimization_framework.execution.admission import ResourcePlanInput, UsageEstimateInput
        from optimization_framework.agents.models import Activate, Configure, Message, Control, QuestionDismiss, Rollback
        from optimization_framework.agents.diagnostics import FixedMaskInput
        from optimization_framework.implementations.references import ReferenceInput
        from optimization_framework.implementations.builtin import BuiltinBindingInput
        models = {"agent.activate": Activate, "agent.message": Message, "agent.control": Control, "agent.rollback": Rollback,
            "agent.configure": Configure, "agent.question_dismiss": QuestionDismiss,
            "fixed_mask.run": FixedMaskInput, "comparison.report": ComparisonReportInput, "campaign.create": CampaignInput, "campaign.update": CampaignUpdateInput,
            "context.edit": ContextEditInput, "issue.resolve": IssueResolveInput,
            "hypothesis.create": HypothesisInput, "hypothesis.review": HypothesisReviewInput,
            "hypothesis.status": HypothesisStatusInput, "hypothesis.nominate": HypothesisNominateInput,
            "trial.create": TrialInput, "trial.control": TrialControlInput, "trial.extension_request": TrialExtensionRequestInput, "trial.validate": TrialValidationInput, "inference.run": InferenceRunInput,
            "draft.save": DraftSaveInput, "draft.launch": DraftLaunchInput,
            "reproduction.draft": ReproductionDraftInput, "reproduction.compare": ReproductionCompareInput,
            "bundle.export": BundleExportInput, "bundle.inspect": BundleInspectInput, "bundle.publish": BundlePublishInput,
            "cost.reconcile": CostReconcileInput,
            "study.create": StudyInput, "study.nominate": NominateInput, "finalist.set": FinalistSetInput, "validation.run": RecipeInput,
            "study.freeze_template": TemplateFreezeInput, "study.activate": ExecutionInput,
            "study.race.create": RaceCreateInput, "study.race.control": RaceControlInput,
            "study.race.decide": RaceDecisionInput,
            "validation.require": RecipeInput, "validation.execute": ExecuteValidationInput, "validation.waive": WaiverInput,
            "runtime.profile": RuntimeProfileInput, "evaluator.equivalence": EquivalenceInput, "trial.continue": TrialContinueInput,
            "journal.compact": JournalCompactInput, "journal.compaction_request": JournalCompactionRequestInput,
            "approval.request": ApprovalRequestInput, "workers.set": WorkerCeilingInput,
            "experiments.rules": ExperimentRulesInput, "trial.hide": TrialHideInput,
            "resources.plan": ResourcePlanInput, "resources.estimate": UsageEstimateInput,
            "validation.revoke_waiver": WaiverRevocationInput,
            "asset.reuse": ReuseInput, "finding.record": FindingInput, "implementation.commission": CommissionInput,
            "asset.snapshot": AssetSnapshotInput,
            "asset.import_reference_set": ReferenceImportInput,
            "implementation.reference": ReferenceInput, "implementation.bind_builtin": BuiltinBindingInput,
            "implementation.attach": AttachInput, "literature.search": SearchInput,
            "decision.resolve": DecisionResolveInput,
            "source.record": SourceRecordInput, "source.ingest": SourceIngestInput,
            "evaluator.commission": EvaluatorCommissionInput, "evaluator.attach": EvaluatorAttachInput, "implementation.control": ImplementationControlInput,
            "implementation.revalidate": RevalidationInput,
            "implementation.reuse": ExecutableReuseInput,
            "implementation.resolve_runtime": RuntimeResolutionInput,
            "confirmation.release": ConfirmationReleaseInput, "confirmation.schedule": ConfirmationScheduleInput,
            "confirmation.validate": ConfirmationScheduleInput}
        return {key: {"payload_schema": model.model_json_schema(), "delegable": key in self.delegated,
                      **({"additional_required_target": "trial_id"} if key in {"validation.run", "validation.require"} else {})}
                for key, model in {**models, **self.models}.items()}

    def execute(self, command, *, actor="researcher"):
        command = command if isinstance(command, Command) else Command(**command)
        try:
            prepared = None
            if command.operation == "hypothesis.create" and command.payload.get("implementation_version_id"):
                try:
                    self.store.get(command.id, "work_command")
                except KeyError:
                    values = HypothesisInput(**{**command.payload, "campaign_id": command.campaign_id})
                    # Fetch and verify the artifact before opening the command's
                    # transaction. Accepted replay requires no library access.
                    prepared = self.workspace.implementations.prepare(values.implementation_version_id, None, values.algorithm_config)
            if command.operation in {"implementation.commission", "evaluator.commission", "implementation.revalidate", "implementation.reuse", "implementation.resolve_runtime", "implementation.attach", "evaluator.attach"}:
                try:
                    self.store.get(command.id, "work_command")
                except KeyError:
                    # Read fresh library evidence before reserving or attaching.
                    # Accepted retries retain their original outcome and need no service call.
                    self.workspace.implementations.catalog(refresh=True)
            return self._execute(command, actor=actor, prepared=prepared)
        except ValueError as exc:
            # Rejected attempts remain reviewable without owning a successful
            # command identity or overwriting an earlier accepted outcome.
            identity = "rejection_" + content_hash([command.model_dump(mode="json"), actor, str(exc)])
            with self.workspace.lock, self.store.transaction():
                try:
                    self.store.get(identity, "command_rejection")
                except KeyError:
                    self.store.put_immutable("command_rejection", {"id": identity, "campaign_id": command.campaign_id,
                        "command_id": command.id, "request": command.model_dump(mode="json"), "actor": actor,
                        "reason": str(exc), "created_at": now()}, "command.rejected")
            raise

    def _execute(self, command, *, actor, prepared=None):
        if actor not in {"researcher", "manager", "autopilot"}:
            raise ValueError("Unknown command authority")
        # The fields the rebuild added are left out of the hash when unset, so receipts written before them still replay.
        request = command.model_dump(mode="json")
        for key in ("origin", "expected_thread_revision"):
            if request.get(key) is None:
                request.pop(key, None)
        request_hash = content_hash({"command": request, "actor": actor})
        # A receipt hashed with those fields present but unset (written between the two conventions) still matches.
        full_hash = content_hash({"command": command.model_dump(mode="json"), "actor": actor})
        with self.workspace.lock:
            with self.store.transaction():
                try:
                    previous = self.store.get(command.id, "work_command")
                except KeyError:
                    previous = None
                if previous:
                    if previous["request_hash"] not in {request_hash, full_hash}:
                        raise ValueError("Command identity already belongs to a different request or authority")
                    return previous
                if command.operation == "campaign.create":
                    if actor != "researcher":
                        raise ValueError("Only the researcher can authorize a new campaign and its resource limits")
                    command.campaign_precondition()
                    authority = content_hash({"kind": "campaign_creation", "campaign_id": command.campaign_id,
                        "researcher_request": command.payload})
                else:
                    campaign = self.store.get(command.campaign_id, "campaign")
                    if campaign["version"] != command.expected_revision:
                        raise ValueError("Campaign revision changed; refresh the command against current authority")
                    guidance = self.workspace.memory.state(command.campaign_id)["guidance_revision"]
                    if command.expected_guidance_revision is not None and guidance != command.expected_guidance_revision:
                        raise ValueError("Researcher guidance changed since this command was prepared")
                    if command.expected_thread_revision is not None and self.thread_revision is not None:
                        if self.thread_revision(command) != command.expected_thread_revision:
                            raise ValueError("The researcher wrote in this conversation since this command was prepared")
                    authority = self.authority_hash(campaign)
                    if command.expected_authority_hash is not None and command.expected_authority_hash != authority:
                        raise ValueError("The command's resource grant changed")
                if actor in {"manager", "autopilot"}:
                    if command.expected_guidance_revision is None or command.expected_authority_hash is None:
                        raise ValueError("Manager commands must pin guidance and delegated authority")
                    if campaign["autonomy"] != "delegated" or command.operation not in self.delegated:
                        raise ValueError("This action needs researcher authorization at the campaign manager interface")
                    from .inbox import check_issues
                    check_issues(self.workspace, command)
                    for guard in self.guards:
                        guard(command, actor, campaign)
                    default_wall = 60 if command.operation == "trial.create" else 120
                    if (command.operation in {"trial.create", "validation.run", "validation.execute", "inference.run", "evaluator.equivalence", "trial.continue"}
                            and command.payload.get("wall_seconds", default_wall) > campaign["delegated_trial_seconds"]):
                        raise ValueError("Command exceeds the manager's delegated per-experiment allowance")
                    if command.operation == "trial.continue" and command.payload.get("accept_unverified"):
                        raise ValueError("Only the researcher continues a run under an evaluator that was not shown to be equivalent")
                    # The manager may pause, stop or resume within an allocation; only the researcher enlarges one.
                    if command.operation == "trial.control" and (command.payload.get("action") == "extend"
                            or any(command.payload.get(key) is not None for key in ("max_steps", "wall_seconds"))):
                        raise ValueError("More time or evaluations for a trial need researcher approval; "
                            "file trial.extension_request explaining how the extra budget would change a decision")
                    if command.operation == "trial.create":
                        request = TrialInput(**{**command.payload, "campaign_id": command.campaign_id})
                        for schedule in request.diagnostics:
                            jobs = [*schedule.recipes, *schedule.rollouts, *(recipe for rollout in schedule.rollouts for recipe in rollout.recipes)]
                            if any(job.wall_seconds > campaign["delegated_trial_seconds"] for job in jobs):
                                raise ValueError("A diagnostic exceeds the manager's delegated per-experiment allowance")
                outcome = self._apply(command, actor, prepared=prepared) if prepared is not None else self._apply(command, actor)
                record = {"id": command.id, "campaign_id": command.campaign_id, "request_hash": request_hash,
                    "request": command.model_dump(mode="json"), "actor": actor, "authority_hash": authority,
                    "origin": command.origin.model_dump(mode="json") if command.origin else None,
                    "status": "completed", "outcome": outcome, "created_at": now(), "finished_at": now()}
                self.store.put("work_command", record, "command.completed")
        for listener in self.listeners:
            try:
                listener(record)
            except Exception as exc:   # a listener never undoes a committed command
                self.store.event(command.campaign_id, "command.listener_failed", {"record_id": record["id"], "error_type": type(exc).__name__})
        self.workspace.dispatch_outbox()
        if command.operation == "study.race.create" and self.workspace.thread is not None:
            from optimization_framework.execution.race_guard import arm
            race = self.store.get(record["outcome"]["race_id"], "adaptive_race")
            arm(self.workspace, race["id"], race["deadline_at"])
        return record

    def _hold_race_configurations(self, campaign_id, hypothesis_id, reason, actor):
        from optimization_framework.contracts.racing import RaceDecisionInput
        for race in self.store.list("adaptive_race", campaign_id):
            if race.get("status") not in {"preflight", "running", "paused"}:
                continue
            protocol = self.workspace.racing._protocol(race)
            bound = [item["id"] for item in protocol["configurations"] if item.get("hypothesis_id") == hypothesis_id
                     and race["configurations"].get(item["id"], {}).get("status") not in {"held", "eliminated", "stopped"}]
            if bound:
                try:
                    self.workspace.racing.decide(campaign_id, RaceDecisionInput(race_id=race["id"], action="hold", configuration_ids=bound,
                                                 rationale=f"Strategy {hypothesis_id} was stopped: {reason}"[:5000]), authority=actor)
                except ValueError as exc:
                    self.workspace.memory.issue(campaign_id, "race_hold_failed", str(exc), affected=race["id"])

    def _target(self, command, key, kind):
        identity = command.payload[key]
        record = self.store.get(identity, kind)
        if record.get("campaign_id") != command.campaign_id:
            raise ValueError(f"The {kind} belongs to another campaign")
        return record

    def _trial_outcome(self, trial, *, snapshot=True):
        # Compatibility responses retain the state accepted by this command,
        # even if execution or another control has advanced by the next retry.
        trial = self.store.get(trial["id"], "trial")
        return {"trial_id": trial["id"], "control_revision": trial["control_revision"],
            **({"trial": {key: value for key, value in trial.items() if key not in {"pid", "process_identity"}}} if snapshot else {})}

    def _apply(self, command, actor, *, prepared=None):
        payload = dict(command.payload)
        if payload.get("campaign_id", command.campaign_id) != command.campaign_id:
            raise ValueError("Command payload refers to another campaign")
        if command.operation in self.handlers:
            return self.handlers[command.operation](command, actor)
        if command.operation == "agent.activate":
            return self.workspace.pi.activate(command.campaign_id, payload, command.id)
        if command.operation == "agent.message":
            return self.workspace.pi.message(command.campaign_id, payload, command.id)
        if command.operation == "agent.control":
            return self.workspace.pi.control(command.campaign_id, payload, command.id)
        if command.operation == "agent.rollback":
            return self.workspace.pi.rollback(command.campaign_id, payload)
        if command.operation == "agent.configure":
            return self.workspace.pi.configure(command.campaign_id, payload, command.id)
        if command.operation == "agent.question_dismiss":
            return self.workspace.pi.dismiss_question(command.campaign_id, payload)
        if command.operation == "fixed_mask.run":
            from optimization_framework.agents.diagnostics import reserve
            return reserve(self.workspace, command.campaign_id, payload, command.id)
        if command.operation in {"hypothesis.create", "hypothesis.review", "hypothesis.status", "hypothesis.nominate"}:
            from optimization_framework.campaigns import hypotheses
            if command.operation == "hypothesis.create":
                values = HypothesisInput(**{**payload, "campaign_id": command.campaign_id})
                hypothesis = hypotheses.create(self.workspace, command.campaign_id, values,
                    identity="hypothesis_" + command.id, bundle=prepared)
                if actor == "manager":
                    hypothesis.update(origin="pi" if self.workspace.pi.owns(command.campaign_id) else "manager", requires_concept_review=True)
                    self.store.put("hypothesis", hypothesis)
                    if self.workspace.pi.owns(command.campaign_id):
                        self.store.put_immutable("agent_alias", {"id": "pi_alias_" + hypothesis["id"], "campaign_id": command.campaign_id,
                            "label": f"H{len(self.store.list('hypothesis', command.campaign_id)):02d}", "record_id": hypothesis["id"], "candidate_id": None})
            else:
                hypothesis = self._target(command, "hypothesis_id", "hypothesis")
                if command.operation == "hypothesis.review":
                    values = HypothesisReviewInput(**payload)
                    hypothesis = hypotheses.review(self.workspace, hypothesis, values.text, identity="review_" + command.id, author=actor)
                else:
                    model = HypothesisStatusInput if command.operation == "hypothesis.status" else HypothesisNominateInput
                    values = model(**payload)
                    hypothesis = hypotheses.set_status(self.workspace, hypothesis,
                        values.status if command.operation == "hypothesis.status" else "finalist", values.expected_status_revision,
                        stop_reason=getattr(values, "stop_reason", None))
                    if command.operation == "hypothesis.status" and values.status == "archived":
                        # A stopped strategy's race configuration is held, so the race spends nothing more on it.
                        self._hold_race_configurations(command.campaign_id, hypothesis["id"], hypothesis.get("stop_reason") or "stopped", actor)
            if actor == "researcher" and not getattr(self, "_batch_depth", 0):
                self.workspace.memory.bump(command.campaign_id)
            return {"hypothesis_id": hypothesis["id"], "hypothesis": self.store.get(hypothesis["id"], "hypothesis")}
        if command.operation in {"campaign.create", "campaign.update", "context.edit", "issue.resolve"}:
            if command.operation == "campaign.create":
                campaign = self.workspace.create_campaign(CampaignInput(**payload), campaign_id=command.campaign_id)
                outcome = {"campaign_id": campaign["id"], "revision": campaign["version"], "campaign": campaign}
            elif command.operation == "campaign.update":
                values = CampaignUpdateInput(**payload)
                previous = self.store.get(command.campaign_id, "campaign")
                campaign = self.workspace.update_campaign(command.campaign_id,
                    CampaignUpdate(**values.model_dump(exclude={"rationale"}, exclude_none=True)))
                limits = ("compute_budget_seconds", "llm_budget_usd", "delegated_trial_seconds",
                    "validation_reserve_seconds", "implementation_compute_budget_seconds")
                changed = [key for key in limits if previous.get(key) != campaign.get(key)]
                if changed:
                    self.store.put_immutable("campaign_budget_amendment", {
                        "id": "campaign_amendment_" + command.id, "schema_version": 1,
                        "campaign_id": command.campaign_id, "command_id": command.id,
                        "previous_revision": previous["version"], "revision": campaign["version"],
                        "previous_limits": {key: previous.get(key) for key in changed},
                        "limits": {key: campaign.get(key) for key in changed},
                        "authority": actor, "rationale": values.rationale, "created_at": now()}, "campaign.budget_amended")
                outcome = {"campaign_id": campaign["id"], "revision": campaign["version"], "campaign": campaign}
            elif command.operation == "context.edit":
                values = ContextEditInput(**payload)
                revision = self.workspace.memory.edit(command.campaign_id, values.content, values.expected_revision, values.reason)
                outcome = {"context_id": revision["id"], "revision": revision["revision"],
                    "guidance_revision": revision["guidance_revision"]}
            else:
                values = IssueResolveInput(**payload)
                self._target(command, "issue_id", "manager_issue")
                issue = self.workspace.memory.resolve_issue(values.issue_id, values.choice, values.comment,
                    expected_revision=values.expected_revision, action=values.action)
                outcome = {"issue_id": issue["id"], "revision": issue["revision"], "issue": issue}
            return outcome
        if command.operation == "cost.reconcile":
            from optimization_framework.assets.accounting import reconcile
            values = CostReconcileInput(**payload)
            if values.asset_id not in {asset["id"] for asset in self.workspace.assets.visible(command.campaign_id)}:
                raise ValueError("Import or declare this asset in the campaign before reconciling its costs")
            graph = self.workspace.assets.contributions([values.asset_id])
            if values.source_id not in graph["intervals"]:
                raise ValueError("The cost source is not a declared contribution to this asset")
            for identity in values.evidence_ids:
                try:
                    self.store.get_entry(identity)
                except KeyError:
                    raise ValueError("Supporting cost evidence is unavailable: " + identity) from None
            receipt = reconcile(self.workspace.assets, values.source_id, values.stop, values.quantities,
                evidence_ids=values.evidence_ids, authority=actor, rationale=values.rationale,
                identity="cost_reconciliation_" + command.id)
            return {"receipt_id": receipt["id"], "asset_id": values.asset_id}
        if command.operation == "asset.snapshot":
            values = AssetSnapshotInput(**payload)
            trial = self._target(command, "trial_id", "trial")
            from optimization_framework.execution.metrics import full_row, scalar_rows
            journal = self.workspace.job_dir(trial["id"]) / "observations.jsonl"
            observation = next((full_row(journal, offset) for offset, _, row in scalar_rows(journal)
                                if row.get("id") == values.observation_id and row.get("status") == "ok"), None)
            if observation is None:
                raise ValueError("Select an exact successful observation from this experiment's journal")
            from optimization_framework.execution.compaction import has_design
            if not has_design(observation):
                raise ValueError(f"That observation's design was removed when this experiment's journals were compacted on {trial.get('journals_compacted_at')}; "
                                 "its archive and a spread of its designs remain")
            from optimization_framework.evaluation.jobs import snapshot_solutions
            asset = snapshot_solutions(self.workspace, trial, [observation["candidate"]])[0]
            return {"asset_id": asset["id"], "observation_id": values.observation_id}
        if command.operation.startswith("bundle."):
            model = {"bundle.export": BundleExportInput, "bundle.inspect": BundleInspectInput, "bundle.publish": BundlePublishInput}[command.operation]
            values = model(**payload).model_dump(mode="json", exclude={"schema_version"})
            if command.operation == "bundle.export":
                for identity in values["asset_ids"]:
                    self.store.get(identity, "asset")
            elif command.operation == "bundle.inspect":
                self.workspace.bundles._upload(values["upload_id"])
            else:
                self._target(command, "inspection_id", "bundle_inspection")
            operation = {"id": "bundle_op_" + command.id, "schema_version": 1, "campaign_id": command.campaign_id,
                "command_id": command.id,
                "action": command.operation.partition(".")[2], "payload": values,
                "status": "queued", "authority": actor, "created_at": now()}
            self.store.put("bundle_operation", operation, "bundle.operation_queued")
            effect = {"id": "bundle_effect_" + command.id, "kind": "bundle_operation", "operation_id": operation["id"],
                "campaign_id": command.campaign_id, "status": "pending", "created_at": now(),
                "expected_context": [command.expected_revision, self.workspace.memory.state(command.campaign_id)["guidance_revision"]]}
            self.store.put("outbox", effect, "effect.queued")
            return {"operation_id": operation["id"], "effect_id": effect["id"]}
        if command.operation == "trial.create":
            trial = self.workspace.create_trial(TrialInput(**{**payload, "campaign_id": command.campaign_id}))
            return self._trial_outcome(trial, snapshot=actor == "researcher")
        if command.operation == "inference.run":
            values = InferenceRunInput(**payload)
            parent = self._target(command, "trial_id", "trial")
            from optimization_framework.evaluation.inference import run_job
            return self._trial_outcome(run_job(self.workspace, parent, values), snapshot=actor == "researcher")
        if command.operation == "asset.import_reference_set":
            from optimization_framework.assets.references import import_set
            imported = import_set(self.workspace, command.campaign_id, ReferenceImportInput(**payload), authority=actor)
            return {"import_id": imported["id"], "asset_bindings": imported["asset_bindings"]}
        if command.operation == "draft.save":
            draft = self.workspace.drafts.save(command.campaign_id, DraftSaveInput(**payload), authority=actor)
            return {"draft_id": draft["id"], "revision": draft["revision"]}
        if command.operation == "reproduction.draft":
            draft = self.workspace.reproductions.save(command.campaign_id, ReproductionDraftInput(**payload), authority=actor)
            return {"draft_id": draft["id"], "revision": draft["revision"]}
        if command.operation == "reproduction.compare":
            values = ReproductionCompareInput(**payload)
            trial = self._target(command, "trial_id", "trial")
            comparison = self.workspace.reproductions.compare(trial)
            return {"trial_id": values.trial_id, "comparison_id": comparison["id"]}
        if command.operation == "draft.launch":
            self._target(command, "draft_id", "experiment_draft")
            launched = self.workspace.drafts.launch(command.campaign_id, DraftLaunchInput(**payload), authority=actor)
            return {"draft_id": launched["draft_id"], "trial_id": launched["trial_id"]}
        if command.operation == "implementation.bind_builtin":
            from optimization_framework.implementations.builtin import bind
            return bind(self.workspace, command.campaign_id, payload, command.id)
        if command.operation == "implementation.reference":
            self._target(command, "hypothesis_id", "hypothesis")
            from optimization_framework.implementations.references import capture
            return capture(self.workspace, command.campaign_id, payload, command.id)
        if command.operation == "implementation.commission":
            self._target(command, "hypothesis_id", "hypothesis")
            values = CommissionInput(**payload).model_dump(exclude={"schema_version"})
            service_key = values.pop("service_idempotency_key") or command.id
            grant = self.workspace.implementations.reserve_commission(**values, idempotency_key=service_key)
            return {"grant_id": grant["id"]}
        if command.operation == "implementation.revalidate":
            values = RevalidationInput(**payload).model_dump(mode="json", exclude={"schema_version"})
            grant = self.workspace.implementations.reserve_revalidation(command.campaign_id, **values, idempotency_key=command.id)
            return {"grant_id": grant["id"], "version_id": values["version_id"]}
        if command.operation == "implementation.resolve_runtime":
            values = RuntimeResolutionInput(**payload)
            version = self.store.get(values.version_id, "implementation_cache")
            effect = {"id": "runtime_effect_" + command.id, "campaign_id": command.campaign_id,
                "kind": "implementation_resolve_runtime", "version_id": values.version_id,
                "runtime_digest": version["runtime_digest"], "authority": actor,
                "expected_context": [command.expected_revision, self.workspace.memory.state(command.campaign_id)["guidance_revision"]],
                "status": "pending", "created_at": now()}
            self.store.put("outbox", effect, "effect.queued")
            return {"effect_id": effect["id"], "version_id": values.version_id}
        if command.operation == "implementation.reuse":
            values = ExecutableReuseInput(**payload).model_dump(mode="json", exclude={"schema_version"})
            self._target(command, "study_id", "study")
            self._target(command, "task_id" if values["task_id"] else "hypothesis_id", "task" if values["task_id"] else "hypothesis")
            if values["decision"] == "reuse":
                from optimization_framework.implementations.reuse import assess
                candidate = assess(self.workspace.implementations, command.campaign_id,
                    self.store.get(values["version_id"], "implementation_cache"), study_id=values["study_id"],
                    hypothesis_id=values["hypothesis_id"], task_id=values["task_id"])
                if not candidate["eligible"]:
                    raise ValueError(candidate["reason"])
            effect = {"id": "reuse_effect_" + command.id, "campaign_id": command.campaign_id, "kind": "implementation_reuse",
                **values, "authority": actor, "identity": "reuse_" + command.id,
                "expected_context": [command.expected_revision, self.workspace.memory.state(command.campaign_id)["guidance_revision"]],
                "status": "pending", "created_at": now()}
            self.store.put("outbox", effect, "effect.queued")
            return {"effect_id": effect["id"], "reuse_decision_id": effect["identity"], "version_id": values["version_id"]}
        if command.operation == "evaluator.commission":
            self._target(command, "task_id", "task")
            values = EvaluatorCommissionInput(**payload).model_dump(exclude={"schema_version"})
            grant = self.workspace.implementations.reserve_commission(hypothesis_id=None, **values, idempotency_key=command.id)
            return {"grant_id": grant["id"]}
        if command.operation in {"evaluator.attach", "implementation.control"}:
            model = EvaluatorAttachInput if command.operation == "evaluator.attach" else ImplementationControlInput
            target, kind = ("task_id", "task") if command.operation == "evaluator.attach" else ("grant_id", "implementation_grant")
            self._target(command, target, kind)
            values = model(**payload).model_dump(exclude={"schema_version"})
            effect = {"id": "effect_" + command.id, "campaign_id": command.campaign_id,
                "kind": command.operation.replace(".", "_"), **values, "authority": actor,
                "expected_context": [command.expected_revision, self.workspace.memory.state(command.campaign_id)["guidance_revision"]],
                "status": "pending", "created_at": now()}
            self.store.put("outbox", effect, "effect.queued")
            return {"effect_id": effect["id"], target: values[target]}
        if command.operation == "confirmation.release":
            self._target(command, "protocol_id", "confirmation_protocol")
            values = ConfirmationReleaseInput(**payload)
            return {"release_id": self.workspace.confirmations.release(values.protocol_id, authority=actor,
                allow_incomplete=values.allow_incomplete, rationale=values.rationale)["id"]}
        if command.operation == "confirmation.schedule":
            self._target(command, "protocol_id", "confirmation_protocol")
            values = ConfirmationScheduleInput(**payload)
            return self.workspace.confirmations.schedule(self.workspace, values.protocol_id,
                reuse_decision_ids=values.reuse_decision_ids, authority=actor)
        if command.operation == "confirmation.validate":
            self._target(command, "protocol_id", "confirmation_protocol")
            values = ConfirmationScheduleInput(**payload)
            return self.workspace.confirmations.schedule_checks(self.workspace, values.protocol_id, authority=actor)
        if command.operation == "implementation.attach":
            self._target(command, "hypothesis_id", "hypothesis")
            values = AttachInput(**payload).model_dump(exclude={"schema_version"})
            effect = {"id": "attach_" + command.id, "campaign_id": command.campaign_id, "kind": "implementation_attach",
                **values, "authority": actor, "expected_context": [command.expected_revision, self.workspace.memory.state(command.campaign_id)["guidance_revision"]],
                "status": "pending", "created_at": now()}
            self.store.put("outbox", effect, "effect.queued")
            return {"effect_id": effect["id"], "hypothesis_id": values["hypothesis_id"], "implementation_version_id": values["version_id"]}
        if command.operation == "decision.resolve":
            from .approvals import resolve
            return resolve(self.workspace, command)
        if command.operation == "source.record":
            values = SourceRecordInput(**payload).model_dump(exclude={"schema_version"})
            source = {**values, "id": "source_" + command.id, "campaign_id": command.campaign_id,
                      "created_at": now(), "verification": "researcher_supplied_unverified"}
            self.store.put_immutable("source", source, "source.created")
            return {"source_id": source["id"], "source": source}
        if command.operation in {"literature.search", "source.ingest"}:
            model = SearchInput if command.operation == "literature.search" else SourceIngestInput
            values = model(**payload).model_dump(exclude={"schema_version"})
            effect = {"id": "search_" + command.id, "campaign_id": command.campaign_id,
                "kind": "literature_search" if command.operation == "literature.search" else "source_ingest",
                **values, "status": "pending", "created_at": now()}
            self.store.put("outbox", effect, "effect.queued")
            return {"effect_id": effect["id"]}
        if command.operation == "trial.control":
            values = TrialControlInput(**payload)
            trial = self._target(command, "trial_id", "trial")
            if trial["control_revision"] != values.expected_control_revision:
                raise ValueError("Experiment controls changed; refresh before submitting another control")
            request = ControlInput(**values.model_dump(exclude={"trial_id", "expected_control_revision"}))
            return self._trial_outcome(self.workspace.control(trial["id"], request, authority=actor))
        if command.operation == "trial.extension_request":
            # The researcher approves it in the decision inbox, which runs the
            # extension under researcher authority (approvals.resolve).
            values = TrialExtensionRequestInput(**payload)
            trial = self._target(command, "trial_id", "trial")
            if trial.get("confirmation_protocol_hash") or trial.get("execution_grant_id") or trial.get("race_phase") == "confirmation":
                raise ValueError("A confirmatory allocation is frozen; propose a new exploratory trial instead of extending it")
            campaign = self.store.get(command.campaign_id, "campaign")
            lead = self.workspace.pi.owns(command.campaign_id)
            budget = f"{values.additional_seconds:g} s" + (f" and {values.additional_evaluations} evaluations" if values.additional_evaluations else "")
            title = f"Extend trial {trial['id']} by {budget}?"
            decision = {"id": "extension_" + command.id, "campaign_id": command.campaign_id, "charter_version": campaign["version"],
                "guidance_revision": self.workspace.memory.state(command.campaign_id)["guidance_revision"],
                "question": title, "title": title, "rationale": values.rationale, "context": values.rationale,
                "proposal": f"{'The lead agent' if lead else 'The campaign manager'} asks for {budget} more on trial {trial['id']} "
                    f"(now {trial['wall_seconds']:g} s, {trial['max_steps']} evaluations).",
                "options": [{"id": "0", "label": f"Extend by {budget}", "description": "Runs under your authority, subject to current campaign limits."},
                            {"id": "1", "label": "Keep the current allocation", "description": "The requester is told and the trial is unchanged."}],
                "recommendation": "0", "trial_id": trial["id"], "incremental_solver_calls": values.additional_evaluations,
                "estimated_seconds": values.additional_seconds, "estimate_basis": "requested budget",
                "requested_by": "lead" if lead else "manager", "audience": "researcher", "status": "pending", "created_at": now()}
            self.store.put("decision", decision, "decision.created")
            return {"decision_id": decision["id"]}
        if command.operation == "trial.validate":
            values = TrialValidationInput(**payload)
            trial = self._target(command, "trial_id", "trial")
            from optimization_framework.contracts.requests import ValidationInput
            request = ValidationInput(**values.model_dump(exclude={"trial_id"}))
            return self._trial_outcome(self.workspace.validate_trial(trial["id"], request))
        if command.operation == "study.create":
            return {"study_id": self.workspace.create_study(command.campaign_id, StudyInput(**payload), authority=actor)["id"]}
        if command.operation == "study.race.create":
            race = self.workspace.racing.create(command.campaign_id, RaceCreateInput(**payload), authority=actor)
            return {"race_id": race["id"], "study_id": race["study_id"], "race": self.workspace.racing.view(race["id"])}
        if command.operation in {"study.race.control", "study.race.decide"}:
            self._target(command, "race_id", "adaptive_race")
            if command.operation == "study.race.control":
                race = self.workspace.racing.control(command.campaign_id, RaceControlInput(**payload), authority=actor)
            else:
                race = self.workspace.racing.decide(command.campaign_id, RaceDecisionInput(**payload), authority=actor)
            return {"race_id": race["id"], "race": self.workspace.racing.view(race["id"])}
        if command.operation == "study.freeze_template":
            return {"execution_id": self.workspace.study_executions.freeze(command.campaign_id, TemplateFreezeInput(**payload), authority=actor)["id"]}
        if command.operation == "study.activate":
            self._target(command, "execution_id", "study_execution")
            return {"execution_id": self.workspace.study_executions.activate(ExecutionInput(**payload).execution_id, authority=actor)["id"]}
        if command.operation == "study.nominate":
            from optimization_framework.analysis.studies import nominate
            values = NominateInput(**payload)
            self._target(command, "study_id", "study")
            return {"nomination_id": nominate(self.workspace, values.study_id, authority=actor, expected_evidence_hash=values.expected_evidence_hash)["id"]}
        if command.operation == "finalist.set":
            from optimization_framework.analysis.finalists import set_selection
            record = set_selection(self.workspace, command.campaign_id, FinalistSetInput(**payload), command_id=command.id)
            return {"finalist_selection_id": record["id"], "revision": record["revision"],
                "trial_ids": record["trial_ids"], "prototype_trial_ids": record["prototype_trial_ids"]}
        if command.operation in {"validation.run", "validation.require"}:
            trial = self._target(command, "trial_id", "trial")
            payload.pop("trial_id")
            result = self.workspace.run_recipe(trial["id"], RecipeInput(**payload), authority=actor,
                require_only=command.operation == "validation.require")
            return self._trial_outcome(result, snapshot=actor == "researcher") if command.operation == "validation.run" else result
        if command.operation == "validation.execute":
            self._target(command, "requirement_id", "validation_requirement")
            values = ExecuteValidationInput(**payload)
            return {"trial_id": self.workspace.run_requirement(values.requirement_id, wall_seconds=values.wall_seconds)["id"]}
        if command.operation == "asset.reuse":
            values = ReuseInput(**payload).model_dump(exclude={"schema_version"})
            decision = ReuseDecision(id="reuse_" + command.id, campaign_id=command.campaign_id,
                **values, authority=actor, created_at=now())
            return {"reuse_decision_id": self.workspace.assets.decide(decision)["id"]}
        if command.operation == "evaluator.equivalence":
            record = self.workspace.equivalence.start(command.campaign_id, EquivalenceInput(**payload))
            return {"equivalence_id": record["id"], "replay_trial_id": record["replay_trial_id"], "candidates": len(record["expected"])}
        if command.operation == "trial.continue":
            self._target(command, "trial_id", "trial")
            return self._trial_outcome(self.workspace.continue_trial(TrialContinueInput(**payload)), snapshot=actor == "researcher")
        if command.operation == "experiments.rules":
            from .experiment_rules import update
            return update(self.store, command.campaign_id, payload, actor)
        if command.operation == "trial.hide":
            from .experiment_rules import hide
            return hide(self.store, command.campaign_id, payload, actor)
        if command.operation == "workers.set":
            if actor != "researcher":
                raise ValueError("Only the researcher changes the worker ceiling; file approval.request")
            values = WorkerCeilingInput(**payload)
            previous = self.workspace.max_workers
            # The scheduler reads the ceiling on every pass, so the change holds from the next one. A runtime
            # profile measured under the old ceiling is no longer current, and a new one measures up to this one.
            self.workspace.max_workers = values.workers
            self.store.put_immutable("worker_ceiling_change", {"id": "worker_ceiling_" + command.id, "campaign_id": command.campaign_id,
                "previous": previous, "workers": values.workers, "rationale": values.rationale, "authority": actor, "created_at": now(),
                "lasts": "Until the services restart; the launcher configuration's workers setting applies then."}, "workers.changed")
            return {"previous": previous, "workers": values.workers}
        if command.operation == "approval.request":
            # The researcher approves it in the decision inbox, which issues the change
            # under researcher authority (approvals.resolve).
            values = ApprovalRequestInput(**payload)
            campaign = self.store.get(command.campaign_id, "campaign")
            asker = "The lead agent" if self.workspace.pi.owns(command.campaign_id) else "The campaign manager"
            request = {"kind": values.kind}
            if values.kind == "campaign_limits":
                CampaignUpdate(**values.limits)
                changes = {key: value for key, value in values.limits.items() if campaign.get(key) != value}
                if not changes:
                    raise ValueError("The campaign already has these limits")
                request["limits"] = changes
                shown = "; ".join(f"{key} {campaign.get(key):g} to {value:g}" for key, value in changes.items())
                title, approve = f"Change campaign limits ({shown})?", "Change the limits"
                proposal = f"{asker} asks to change this campaign's limits: {shown}. The change is recorded as a budget amendment under your authority."
            elif values.kind == "waiver":
                requirement = self._target(command, "requirement_id", "validation_requirement")
                for identity in values.evidence_ids:
                    if self.store.get(identity).get("campaign_id") != command.campaign_id:
                        raise ValueError("A waiver request cites this campaign's own measurements")
                results = [row for row in self.store.list("validation_result", command.campaign_id) if row["requirement_id"] == requirement["id"]]
                request.update(requirement_id=requirement["id"], evidence_ids=values.evidence_ids)
                title, approve = f"Waive the {requirement['kind'].replace('_', ' ')} check ({requirement['id']})?", "Waive the check"
                proposal = (f"{asker} asks you to waive a check that now blocks work. " +
                    (f"Its last result: {results[-1]['verdict']}. {results[-1].get('rationale', '')} " if results else "It has no result yet. ") +
                    "A waiver is yours alone, is recorded with this reason, and can be revoked.")
            else:
                if values.workers == self.workspace.max_workers:
                    raise ValueError(f"The worker ceiling is already {values.workers}")
                request["workers"] = values.workers
                title, approve = f"Change the worker ceiling from {self.workspace.max_workers} to {values.workers}?", "Change the ceiling"
                proposal = (f"{asker} asks for {values.workers} experiments to run at once instead of {self.workspace.max_workers}. It applies at once, to every "
                    "campaign on this server, until the services restart; to keep it, set workers in the launcher configuration. "
                    "A runtime profile measured under the old ceiling is no longer current.")
            decision = {"id": "approval_" + command.id, "campaign_id": command.campaign_id, "charter_version": campaign["version"],
                "guidance_revision": self.workspace.memory.state(command.campaign_id)["guidance_revision"],
                "question": title, "title": title, "rationale": values.rationale, "context": values.rationale, "proposal": proposal,
                "options": [{"id": "0", "label": approve, "description": "Applied at once as your own command."},
                            {"id": "1", "label": "Leave it as it is", "description": "The requester is told and nothing changes."}],
                "recommendation": "0", "approval_request": request,
                "requested_by": "lead" if self.workspace.pi.owns(command.campaign_id) else "manager", "audience": "researcher",
                "status": "pending", "created_at": now()}
            self.store.put("decision", decision, "decision.created")
            return {"decision_id": decision["id"], "request": request}
        if command.operation == "journal.compact":
            if actor != "researcher":
                raise ValueError("Only the researcher removes recorded designs; file journal.compaction_request")
            values = JournalCompactInput(**payload)
            plan = self.workspace.journals.plan(command.campaign_id, trial_ids=values.trial_ids, study_id=values.study_id)
            job = self.workspace.journals.start(command.campaign_id, plan["trial_ids"], authority=actor, identity="journal_compaction_" + command.id)
            return {"compaction_id": job["id"], "trial_ids": job["trial_ids"], "bytes_before": job["bytes_before"], "kept": job["kept"]}
        if command.operation == "journal.compaction_request":
            # The researcher approves it in the decision inbox, which runs the
            # compaction under researcher authority (approvals.resolve).
            values = JournalCompactionRequestInput(**payload)
            plan = self.workspace.journals.plan(command.campaign_id, trial_ids=values.trial_ids, study_id=values.study_id)
            if not plan["trial_ids"]:
                raise ValueError("None of these experiments has journals left to compact")
            campaign = self.store.get(command.campaign_id, "campaign")
            lead = self.workspace.pi.owns(command.campaign_id)
            count, size = len(plan["trial_ids"]), plan["bytes"] / 1e9
            uncited = plan["not_cited_by_a_finding"]
            title = f"Remove the evaluated designs from the journals of {count} finished experiment{'s' if count != 1 else ''} ({size:.2f} GB)?"
            decision = {"id": "compaction_" + command.id, "campaign_id": command.campaign_id, "charter_version": campaign["version"],
                "guidance_revision": self.workspace.memory.state(command.campaign_id)["guidance_revision"],
                "question": title, "title": title, "rationale": values.rationale, "context": values.rationale,
                "proposal": f"{'The lead agent' if lead else 'The campaign manager'} asks to compact the request and observation journals of {count} "
                    f"finished experiment{'s' if count != 1 else ''}, which hold {size:.2f} GB. Every row and every number stays, with the designs of each "
                    "experiment's archive, of its 32 best evaluations and of 32 spread over the run; results, metrics and saved states are untouched. "
                    "The other evaluated designs are removed and cannot be brought back: they can no longer be snapshotted or replayed. "
                    + (f"{len(uncited)} of these experiments {'is' if len(uncited) == 1 else 'are'} not cited by any recorded finding yet: " + ", ".join(uncited[:20])
                       + (" …" if len(uncited) > 20 else "") + "." if uncited else "Every one of them is cited by a recorded finding."),
                "options": [{"id": "0", "label": "Compact these journals", "description": "Runs under your authority; the removed designs are gone for good."},
                            {"id": "1", "label": "Keep the journals", "description": "The requester is told and nothing changes."}],
                "recommendation": "0" if not uncited else "1", "journal_compaction": {"trial_ids": plan["trial_ids"], "bytes": plan["bytes"]},
                "requested_by": "lead" if lead else "manager", "audience": "researcher", "status": "pending", "created_at": now()}
            self.store.put("decision", decision, "decision.created")
            return {"decision_id": decision["id"], "trial_ids": plan["trial_ids"], "bytes": plan["bytes"], "not_cited_by_a_finding": uncited}
        if command.operation in {"resources.plan", "resources.estimate"}:
            from optimization_framework.execution.admission import ResourcePlanInput, UsageEstimateInput
            if command.operation == "resources.plan":
                return self.workspace.admission.plan(command.campaign_id, ResourcePlanInput(**payload), authority=actor)
            return self.workspace.admission.estimate(command.campaign_id, UsageEstimateInput(**payload), authority=actor)
        if command.operation == "runtime.profile":
            profile = self.workspace.runtime.start(command.campaign_id, RuntimeProfileInput(**payload))
            return {"runtime_profile_id": profile["id"], "levels": [level["workers"] for level in profile["levels"]]}
        if command.operation == "validation.waive":
            values = WaiverInput(**payload)
            requirement = self._target(command, "requirement_id", "validation_requirement")
            waiver = Waiver(id="waiver_" + command.id, campaign_id=command.campaign_id, study_id=requirement["study_id"],
                requirement_id=requirement["id"], subject_digest=requirement["subject_digest"], recipe_id=requirement["recipe_id"],
                rationale=values.rationale, evidence_ids=values.evidence_ids, authority=actor, authority_kind=actor, created_at=now())
            return {"waiver_id": self.workspace.validations.waive(waiver)["id"]}
        if command.operation == "validation.revoke_waiver":
            values = WaiverRevocationInput(**payload)
            waiver = self._target(command, "waiver_id", "waiver")
            revocation = WaiverRevocation(id="revoke_" + command.id, campaign_id=command.campaign_id, waiver_id=waiver["id"],
                rationale=values.rationale, authority=actor, created_at=now())
            return {"revocation_id": self.workspace.validations.revoke(revocation)["id"]}
        if command.operation == "comparison.report":
            from optimization_framework.analysis.general import report
            return report(self.workspace, command.campaign_id, **ComparisonReportInput.model_validate(payload).model_dump(exclude={"schema_version"}))
        if command.operation == "finding.record":
            from .findings import record
            return record(self.workspace, command, actor)
        raise ValueError("Unsupported application command")
