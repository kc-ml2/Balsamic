"""Single-machine experiment supervisor, independent from LLM execution."""
from __future__ import annotations

from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time

from optimization_framework.contracts.requests import CampaignInput, CampaignUpdate, TrialInput, ControlInput, ValidationInput, RecipeInput, StudyInput, ResearchInput
from optimization_framework.storage.sqlite import Store, atomic_json, identifier, now, read_json
from optimization_framework.evaluation.registry import problems
from optimization_framework.contracts.problems import ProblemInstance
from optimization_framework.contracts.experiments import StudySpec, ExperimentSpec, ImplementationVersion, BudgetAmendment, CompletionCondition, study_for_tasks, task_in_study
from optimization_framework.contracts.experiments import DELIBERATE_STOPS
from optimization_framework.contracts.base import content_hash
from optimization_framework.optimizers.registry import capability_reason, methods as algorithms
from optimization_framework.execution.source import snapshot


ACTIVE = {"queued", "running", "pausing", "stopping"}
LIVE = {"running", "pausing", "stopping"}


def process_identity(pid):
    """Linux process start ticks prevent signals to a recycled PID."""
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text()
        fields = raw[raw.rfind(")") + 2:].split()
        return None if fields[0] == "Z" else fields[19]
    except (OSError, ValueError, IndexError, TypeError):
        return None


def alive(trial):
    pid, identity = trial.get("pid"), trial.get("process_identity")
    return bool(pid and identity and process_identity(pid) == identity)


class Workspace:
    def __init__(self, directory, max_workers=2, stop_grace_seconds=5, implementation_client=None):
        self.store = Store(directory)
        self.directory = self.store.directory
        from optimization_framework.research.log import AgentLog
        self.agent_log = AgentLog(self.store)
        self.store.event_observer = self.agent_log.observe
        self.agent_log_thread = None
        self.max_workers = max(1, min(int(max_workers), 16))
        self.stop_grace_seconds = stop_grace_seconds
        self.lock = threading.RLock()
        self.outbox_lock = threading.Lock()
        self.shutdown_event = threading.Event()
        self.thread = None
        self.processes = {}
        self.last_progress = {}
        from optimization_framework.execution.metrics import MetricProjectionCache
        self._metric_projections = MetricProjectionCache()
        self.research_threads = {}
        self.source_threads = {}
        self.on_trial_finished = None
        self._lease = None
        from optimization_framework.research.model_policy import CampaignModels
        self.models = CampaignModels(self)
        from optimization_framework.campaigns.memory import CampaignMemory
        from optimization_framework.implementations.bridge import ImplementationBridge
        self.memory = CampaignMemory(self)
        self.implementations = ImplementationBridge(self, implementation_client)
        from optimization_framework.evaluation.commissioning import EvaluatorService
        self.evaluators = EvaluatorService(self)
        from optimization_framework.assets.catalog import AssetCatalog
        self.assets = AssetCatalog(self.store)
        from optimization_framework.assets.bundles import BundleService
        self.bundles = BundleService(self)
        from optimization_framework.evaluation.policy import ValidationService
        from optimization_framework.evaluation.confirmation import ConfirmationService
        self.validations = ValidationService(self.store)
        self.confirmations = ConfirmationService(self.store)
        from optimization_framework.campaigns.commands import CommandService
        self.commands = CommandService(self)
        from optimization_framework.campaigns.drafts import DraftService
        self.drafts = DraftService(self)
        from optimization_framework.campaigns.reproduction import ReproductionService
        self.reproductions = ReproductionService(self)
        from optimization_framework.execution.resources import ResourceLedger
        self.resources = ResourceLedger(self.store)
        from optimization_framework.execution.studies import StudyExecutionService
        self.study_executions = StudyExecutionService(self)
        from optimization_framework.execution.racing import AdaptiveRacing
        self.racing = AdaptiveRacing(self)
        self.maintenance_thread = None
        self.on_manager_tick = None
        from optimization_framework.research.discovery.controller import DiscoveryController
        self.discovery = DiscoveryController(self)
        from optimization_framework.agents.controller import PiController
        self.pi = PiController(self)

    def start(self):
        if self._lease:
            raise RuntimeError("This workspace still owns its service lease")
        self._lease = (self.directory / "service.lock").open("a+")
        try:
            fcntl.flock(self._lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lease.close()
            raise RuntimeError("Another service owns this workspace directory") from exc
        self.shutdown_event.clear()
        self.agent_log.project_pending()
        self.reconcile()
        from optimization_framework.execution.race_guard import arm
        for race in self.store.list("adaptive_race"):
            if race["status"] in {"preflight", "running", "paused"}:
                arm(self, race["id"], race["deadline_at"])
        from optimization_framework.research.lifecycle import recover
        recover(self)
        self.discovery.recover()
        from optimization_framework.assets.service_costs import reconcile_workspace
        reconcile_workspace(self)
        self.thread = threading.Thread(target=self._loop, name="experiment-supervisor", daemon=True)
        self.thread.start()
        self.maintenance_thread = threading.Thread(target=self._maintenance, name="campaign-manager", daemon=True)
        self.maintenance_thread.start()
        self.agent_log_thread = threading.Thread(target=self._project_agent_logs, name="agent-log-projector", daemon=True)
        self.agent_log_thread.start()

    def _project_agent_logs(self):
        while not self.shutdown_event.wait(.5):
            self.agent_log.project_pending()
            self.discovery.project_pending()

    def _maintenance(self):
        while not self.shutdown_event.wait(2):
            try:
                self.implementations.reconcile()
                self.dispatch_outbox()
                from optimization_framework.assets.service_costs import reconcile_workspace
                reconcile_workspace(self)
                self.reconcile_assets()
                from optimization_framework.evaluation.diagnostics import reconcile as reconcile_diagnostics
                reconcile_diagnostics(self)
                self.study_executions.reconcile()
                self.racing.tick()
                from optimization_framework.agents.diagnostics import reconcile as reconcile_fixed_masks
                reconcile_fixed_masks(self)
                self.confirmations.reconcile_reports(on_error=self.memory.issue)
                if self.on_manager_tick:
                    self.on_manager_tick()
            except Exception as exc:
                self.store.event(None, "manager.maintenance_failed", {"error_type": type(exc).__name__})

    def close(self):
        self.shutdown_event.set()
        if self.thread:
            self.thread.join(timeout=3)
        if self.maintenance_thread:
            self.maintenance_thread.join(timeout=6)
        if self.agent_log_thread:
            self.agent_log_thread.join(timeout=3)
        # A returning provider/source call still writes its receipt. Do not let a
        # second local owner start until all of this owner's writers are done.
        writers = [thread for thread in [self.thread, self.maintenance_thread, self.agent_log_thread,
            *self.research_threads.values(), *self.source_threads.values()] if thread and thread.is_alive()]
        lease = self._lease
        def release():
            for thread in writers:
                thread.join()
            self.agent_log.project_pending()
            self.discovery.project_pending()
            if lease:
                fcntl.flock(lease, fcntl.LOCK_UN)
                lease.close()
                if self._lease is lease:
                    self._lease = None
        if writers:
            threading.Thread(target=release, name="workspace-shutdown", daemon=True).start()
        else:
            release()

    def job_dir(self, trial_id):
        # Identifiers always come from records or our UUID generator.
        if "/" in trial_id or ".." in trial_id:
            raise ValueError("Invalid trial identifier")
        return self.directory / "trials" / trial_id

    def capture_evidence(self, trial):
        from optimization_framework.assets.execution import ingest_costs, ingest_outputs
        if trial.get("execution_contract") != 1 or alive(trial):
            return trial
        self.execution_policy(trial)
        with self.lock, self.store.transaction():
            directory = self.job_dir(trial["id"])
            costs = ingest_costs(self.assets, trial, directory)
            assets = ingest_outputs(self.assets, trial, directory, costs)
            trial["output_asset_ids"] = sorted(set(trial.get("output_asset_ids", [])) | set(assets))
            trial["latest_output_asset_ids"] = assets
            from optimization_framework.evaluation.jobs import finish
            finish(self, trial)
            trial["asset_capture_attempt"] = trial["attempt"]
            trial["evidence_committed_at"] = now()
            if trial.get("reproduction"):
                comparison = self.reproductions.compare(trial)
                trial["reproduction_comparison_ids"] = sorted(set(trial.get("reproduction_comparison_ids", [])) | {comparison["id"]})
                trial["reproduction_comparison_id"] = comparison["id"]
            self.store.put("trial", trial, "trial.evidence_cataloged")
        return trial

    def reconcile_assets(self):
        with self.lock:
            for trial in self.store.trials_requiring_capture():
                try:
                    self.capture_evidence(trial)
                except Exception as exc:
                    self.memory.issue(trial["campaign_id"], "evidence_capture_failed", str(exc), affected=trial["id"])

    def create_study(self, campaign_id, request: StudyInput, *, authority="researcher"):
        from optimization_framework.contracts.confirmation import ConfirmationProtocol
        from optimization_framework.evaluation.confirmation import method_definition
        from optimization_framework.evaluation.confirmation_allocations import binding_id, derive
        with self.lock, self.store.transaction():
            campaign = self.store.get(campaign_id, "campaign")
            tasks = self.current_tasks(campaign_id)
            if request.task_ids:
                selected = set(request.task_ids)
                if selected - {task["id"] for task in tasks}:
                    raise ValueError("Study instances must be selected from this campaign's current problem definitions")
                tasks = [task for task in tasks if task["id"] in selected]
            if not tasks:
                raise ValueError("A study needs at least one problem instance")
            from optimization_framework.evaluation.recipes import validation_policy
            frozen_validation = validation_policy(tasks, request.validation_policy, registry_resolver=self.evaluators.registry_for)
            from optimization_framework.analysis.rules import freeze as freeze_rule, check_design
            from optimization_framework.analysis.studies import experiment_evidence
            instances = [ProblemInstance(**task["problem"]) for task in tasks]
            selection = freeze_rule(request.selection, "selection", instances, store=self.store) if request.selection else {}
            analysis = freeze_rule(request.analysis, "verdict", instances, store=self.store) if request.analysis else {}
            if selection:
                check_design(selection, {"instances": [instance.model_dump(mode="json") for instance in instances]}, store=self.store)
            if request.scope != "confirmation" and (analysis or request.nomination_id or request.reference_trial_ids):
                raise ValueError("Nomination, reference evidence and verdict rules belong to a confirmation study")
            if request.scope == "confirmation" and selection:
                raise ValueError("Development selection must finish before the confirmation study is frozen")
            nomination = self.store.get(request.nomination_id, "nomination") if request.nomination_id else None
            if nomination and nomination["campaign_id"] != campaign_id:
                raise ValueError("Nomination belongs to another campaign")
            from optimization_framework.analysis.finalists import confirmation_selection
            finalist_selection = confirmation_selection(self.store, campaign_id, request)
            methods, prototypes, allocation_bindings, source_allocations = {}, {}, {}, {}
            prototype_ids = [*request.prototype_trial_ids, *((nomination or {}).get("prototypes", {}).values())]
            if set(request.prototype_allocations) - set(prototype_ids):
                raise ValueError("Final allocations must refer to selected prototype trials")
            for trial_id in dict.fromkeys(prototype_ids):
                prototype = self.store.get(trial_id, "trial")
                if prototype.get("method_contract") == 3:
                    raise ValueError("This procedure has cell-specific bindings; use its frozen template execution to confirm it")
                if prototype["campaign_id"] != campaign_id or prototype.get("recipe") or prototype.get("diagnostic_grant_id") or prototype.get("execution_contract") != 1:
                    raise ValueError("Select a versioned optimization prototype from this campaign")
                source_method = method_definition(prototype)
                source_id = content_hash(source_method)
                method, allocation_binding = source_method, None
                if trial_id in request.prototype_allocations:
                    method, allocation_binding = derive(prototype, source_method, request.prototype_allocations[trial_id])
                    if nomination and source_id in nomination["methods"] and method != source_method:
                        raise ValueError("A frozen nomination fixes its method allocation. Use a manual finalist shortlist to define a different final allocation")
                method_id = content_hash(method)
                if source_id in source_allocations and source_allocations[source_id] != method_id:
                    raise ValueError("Selected seed replicas of the same prototype procedure have conflicting final allocations; choose one representative")
                source_allocations[source_id] = method_id
                if method_id not in methods:
                    methods[method_id] = method
                    prototypes[method_id] = trial_id
                    if allocation_binding:
                        allocation_bindings[method_id] = allocation_binding
            if nomination and any(methods.get(key) != value for key, value in nomination["methods"].items()):
                raise ValueError("The nominated source prototype changed; preserve the nomination and define a new study")
            references = []
            for trial_id in dict.fromkeys(request.reference_trial_ids):
                reference = self.store.get(trial_id, "trial")
                if reference["campaign_id"] != campaign_id or reference.get("task_split") in {"test", "heldout", "confirmation"} or reference.get("locked"):
                    raise ValueError("Reference evidence must be visible in this campaign before confirmation")
                if reference["status"] != "completed" or not (reference.get("result") or {}).get("scientific_complete"):
                    raise ValueError("Reference experiments must have completed before their results are frozen")
                references.append(experiment_evidence(self.store, reference))
            study_id = identifier("study")
            protocol = None
            if request.scope == "confirmation":
                for task in tasks:
                    readiness = self.evaluators.readiness(task, allow_waived=False)
                    if not readiness["runnable"]:
                        raise ValueError(readiness["reason"])
                if request.confirmation_kind is None:
                    raise ValueError("Choose seed replication, unseen-instance confirmation, or policy transfer")
                protocol = ConfirmationProtocol(id=identifier("protocol"), campaign_id=campaign_id, study_id=study_id,
                    kind=request.confirmation_kind, methods=methods, prototypes=prototypes, instances=[ProblemInstance(**task["problem"]) for task in tasks],
                    seeds=request.seeds, selection_rule=request.selection_rule, policy_asset_id=request.policy_asset_id,
                    analysis=analysis, nomination_id=request.nomination_id, reference_evidence=references,
                    adaptation=request.adaptation, adaptation_procedure=request.adaptation_procedure, authority=authority, created_at=now())
                if analysis:
                    check_design(analysis, {**protocol.model_dump(mode="json"), "nomination": nomination}, store=self.store)
                if protocol.policy_asset_id:
                    asset = self.store.get(protocol.policy_asset_id, "asset")
                    if asset["kind"] != "policy" or self.assets.availability(asset)["status"] == "unavailable":
                        raise ValueError("Policy transfer requires an available policy artifact")
            elif request.confirmation_kind:
                raise ValueError("Confirmation protocols require a confirmation study")
            study = study_for_tasks(tasks, id=study_id, campaign_id=campaign_id, parent_study_id=campaign.get("active_study_id"),
                goal=request.goal, scope=request.scope,
                method_roster=list(methods), assumptions=request.assumptions, comparison=request.comparison,
                selection=selection,
                validation_policy=frozen_validation, confirmation=protocol.model_dump(mode="json") if protocol else {},
                created_at=now(), authority=authority)
            record = study.model_dump(mode="json")
            entries = [("study", {**record, "content_hash": content_hash(record)}, "study.created")]
            if protocol:
                entries.append(("confirmation_protocol", {**protocol.model_dump(mode="json"), "content_hash": protocol.digest()}, "confirmation.frozen"))
                if allocation_bindings:
                    binding = {"schema_version": 1, "id": binding_id(protocol.id), "campaign_id": campaign_id,
                        "study_id": study_id, "protocol_id": protocol.id, "methods": allocation_bindings,
                        "created_at": now()}
                    self.store.put_immutable("confirmation_allocation_binding", binding, "confirmation.allocations_frozen")
            if finalist_selection:
                binding = {"id": "finalists_for_" + study_id, "campaign_id": campaign_id, "study_id": study_id,
                    "protocol_id": protocol.id, **finalist_selection, "created_at": now()}
                entries.append(("finalist_confirmation_binding", {**binding, "content_hash": content_hash(binding)}, "finalist.confirmation_bound"))
            campaign.update(active_study_id=study_id, version=campaign["version"] + 1, updated_at=now())
            charter = {**campaign, "id": identifier("charter"), "campaign_id": campaign_id, "tasks": self.current_tasks(campaign_id)}
            self.store.put_many([*entries, ("campaign", campaign, "campaign.study_changed"), ("charter", charter, None)])
            for task in tasks:
                self.evaluators.require_numerical_evidence(task, study_id)
            return self.store.get(study_id, "study")

    def create_campaign(self, request: CampaignInput, *, campaign_id=None):
        with self.lock, self.store.transaction():
            if campaign_id is not None:
                import re
                if not re.fullmatch(r"[a-zA-Z0-9_-]{1,160}", campaign_id):
                    raise ValueError("Invalid campaign identity")
                try:
                    self.store.get_entry(campaign_id)
                except KeyError:
                    pass
                else:
                    raise ValueError("A record already owns this campaign identity")
            data = request.model_dump(exclude={"tasks"})
            campaign = dict(data, id=campaign_id or identifier("campaign"), version=1, created_at=now(), updated_at=now())
            self.store.put("campaign", campaign, "campaign.created")
            self._add_tasks(campaign, request.tasks)
            # Installed algorithms are resources for research and manual
            # experiments. Creating a campaign is not scientific generation.
            campaign["research_protocol"] = "optimizer-discovery-v1"
            self.store.put("charter", {**campaign, "id": identifier("charter"), "campaign_id": campaign["id"],
                                        "tasks": [t.model_dump() for t in request.tasks]})
            study = study_for_tasks(self.current_tasks(campaign["id"]), id=identifier("study"), campaign_id=campaign["id"], goal=campaign["objective"],
                created_at=now(), authority="researcher")
            self.store.put_immutable("study", study.model_dump(mode="json"), "study.created")
            campaign["active_study_id"] = study.id
            campaign["schema_version"] = 1
            self.store.put("campaign", campaign)
            return campaign

    def _add_tasks(self, campaign, tasks):
        for task in tasks:
            record = task.model_dump(exclude={"id"})
            adapter = problems.get(task.problem_id) if task.evaluator_manifest is None else None
            exposure = adapter.exposure_fields(self.store, campaign["id"], task.problem) if hasattr(adapter, "exposure_fields") else {"exposed": False}
            record.update(id=identifier("task"), campaign_id=campaign["id"], charter_version=campaign["version"],
                          problem_instance_id="instance_" + task.problem.digest(), archived=False, created_at=now(), **exposure)
            if task.evaluator_manifest is not None:
                self.evaluators.declare(record)
            else:
                self.store.put_immutable("problem_instance", {"id": record["problem_instance_id"],
                    **task.problem.model_dump(mode="json")})
            self.store.put("task", record)

    def update_campaign(self, campaign_id, request: CampaignUpdate):
        with self.lock, self.store.transaction():
            campaign = self.store.get(campaign_id, "campaign")
            changes = request.model_dump(exclude_none=True, exclude={"tasks"})
            objective_changed = "objective" in changes and changes["objective"] != campaign["objective"]
            tasks_changed = request.tasks is not None
            if request.tasks is not None:
                existing_tasks = {task["id"]: task for task in self.store.list("task", campaign_id) if not task.get("archived")}
                if len(existing_tasks) == len(request.tasks) and {task.id for task in request.tasks} == set(existing_tasks):
                    tasks_changed = any(any(existing_tasks[task.id].get(key) != value
                        for key, value in task.model_dump(mode="json", exclude={"id"}).items()) for task in request.tasks)
            proposed = {**campaign, **changes}
            if proposed["validation_reserve_seconds"] > proposed["compute_budget_seconds"]:
                raise ValueError("Validation reserve exceeds compute budget")
            used_reserved = self.allocated_seconds(campaign_id)
            if proposed["compute_budget_seconds"] < used_reserved:
                raise ValueError(f"Existing spent/reserved compute is {used_reserved:.1f}s; stop or finish work before lowering this cap")
            if proposed.get("implementation_compute_budget_seconds", 0) < self.implementations.compute_committed(campaign_id):
                raise ValueError("Existing implementation allocations exceed the proposed implementation compute cap")
            from optimization_framework.research.providers import api_spend
            api_committed = self.implementations.api_committed(campaign_id) + sum(api_spend(r.get("usage")) for r in self.store.list("research_run", campaign_id))
            if proposed["llm_budget_usd"] + 1e-9 < api_committed:
                raise ValueError("Existing model spending and implementation grants exceed the proposed API cap")
            campaign.update(changes)
            campaign.update(version=campaign["version"] + 1, updated_at=now())
            if tasks_changed:
                for task in self.store.list("task", campaign_id):
                    if not task.get("archived"):
                        self.store.put("task", {**task, "archived": True})
                self._add_tasks(campaign, request.tasks)
            if objective_changed or tasks_changed:
                study = study_for_tasks(self.current_tasks(campaign_id), id=identifier("study"), campaign_id=campaign_id,
                    parent_study_id=campaign.get("active_study_id"), goal=campaign["objective"],
                    created_at=now(), authority="researcher")
                self.store.put_immutable("study", study.model_dump(mode="json"), "study.created")
                campaign["active_study_id"] = study.id
                for task in self.current_tasks(campaign_id):
                    self.evaluators.require_numerical_evidence(task, study.id)
            self.store.put("campaign", campaign, "campaign.revised")
            self.store.put("charter", {**campaign, "id": identifier("charter"), "campaign_id": campaign_id,
                                        "tasks": self.current_tasks(campaign_id)})
            for trial in self.store.list("trial", campaign_id):
                trial["charter_superseded"] = True
                self.store.put("trial", trial)
            return campaign

    def current_tasks(self, campaign_id):
        return [self.evaluators.task_view(t) for t in self.store.list("task", campaign_id) if not t.get("archived")]

    def allocated_seconds(self, campaign_id, exclude=None):
        return self.resources.assessment(campaign_id, exclude=exclude)["allocated_seconds"]

    def _check_allocation(self, campaign, seconds, exclude=None, validation=False, *, grant_id=None, deadline_at=None):
        if grant_id:
            return self.resources.check(grant_id, campaign["id"], seconds, exclude=exclude, deadline_at=deadline_at)
        ceiling = campaign["compute_budget_seconds"]
        if not validation:
            ceiling -= campaign.get("validation_reserve_seconds", 0)
        remaining = ceiling - self.allocated_seconds(campaign["id"], exclude)
        if seconds > remaining + 1e-6:
            raise ValueError(f"Requested allocation exceeds remaining campaign budget ({max(0, remaining):.1f}s available)")

    def check_manager_context(self, campaign_id, expected):
        if expected is not None:
            current = (self.store.get(campaign_id, "campaign")["version"], self.memory.state(campaign_id)["guidance_revision"])
            if tuple(expected) != current:
                raise ValueError("Campaign direction changed while this action was being prepared; ask the manager for a current proposal")

    def create_trial(self, request: TrialInput, validation=None, *, expected_context=None, frozen_prototype_id=None, execution=None,
                     isolation_policy=None, captured_source=None, reproduction=None):
        from optimization_framework.contracts.isolation import IsolationPolicy
        if captured_source is not None and isolation_policy is None:
            isolation_policy = IsolationPolicy()
        selected_isolation = (IsolationPolicy.model_validate(isolation_policy) if isolation_policy is not None else None)
        if reproduction is not None:
            from optimization_framework.contracts.reproduction import ReproductionIntent
            if captured_source is None or validation is not None or frozen_prototype_id is not None or execution:
                raise ValueError("Historical reproduction requires its captured source and a new exploratory execution")
            self.reproductions.check_procedure(request.campaign_id,
                ReproductionIntent(**{key: reproduction[key] for key in ("reference", "comparison")}), request)
            if content_hash(captured_source[1]) != reproduction["execution_manifest_digest"]:
                raise ValueError("Reproduction source differs from its frozen binding")
        bundle = None
        selected_task = self.store.get(request.task_id, "task")
        if selected_task["campaign_id"] != request.campaign_id:
            raise ValueError("Select a task belonging to this campaign")
        protocol = self.store.get(request.confirmation_protocol_id, "confirmation_protocol") if request.confirmation_protocol_id else None
        evaluator_study_id = ((validation or {}).get("study_id") or (protocol or {}).get("study_id")
            or (execution or {}).get("study_id") or self.store.get(request.campaign_id, "campaign").get("active_study_id"))
        evaluator_bundle = self.evaluators.prepare(selected_task, study_id=evaluator_study_id)
        selected_task = self.evaluators.task_view(selected_task)
        selected_hypothesis = self.store.get(request.hypothesis_id, "hypothesis") if request.hypothesis_id else None
        version_id = request.implementation_version_id or (selected_hypothesis or {}).get("implementation_version_id")
        if version_id and validation is None:
            if request.algorithm not in {"", "package"}:
                raise ValueError("Algorithm alias conflicts with the selected implementation version")
            if selected_hypothesis and selected_hypothesis.get("implementation_version_id") not in {None, version_id}:
                raise ValueError("Experiment implementation conflicts with the selected hypothesis")
            bundle = self.implementations.prepare(version_id, selected_task, request.algorithm_config)
            parameters = {**bundle["version"]["spec"]["parameters"], **request.algorithm_config}
            request = request.model_copy(update={"algorithm": "package", "algorithm_config": parameters, "implementation_version_id": version_id})
        with self.lock, self.store.transaction():
            self.check_manager_context(request.campaign_id, expected_context)
            campaign = self.store.get(request.campaign_id, "campaign")
            if request.race_id:
                race = self.store.get(request.race_id, "adaptive_race")
                if race["campaign_id"] != request.campaign_id or race["status"] not in {"preflight", "running", "paused"}:
                    raise ValueError("Select an open adaptive race belonging to this campaign")
                if not request.race_phase:
                    raise ValueError("Declare the adaptive race execution phase")
            if request.hypothesis_id and validation is None:
                from optimization_framework.research.discovery.proposals import readiness as proposal_readiness
                review = proposal_readiness(self.store, self.store.get(request.hypothesis_id, "hypothesis"))
                if not review["eligible"]:
                    raise ValueError(review["reason"])
            task = self.evaluators.task_view(self.store.get(request.task_id, "task"))
            execution = dict(execution or {})
            if validation and validation.get("parent_trial_id"):
                parent = self.store.get(validation["parent_trial_id"], "trial")
                inherited_keys = ("protected_cohort_id",) if validation.get("independent_countercheck") else (
                    "execution_grant_id", "absolute_deadline", "study_execution_id", "stop_grace_seconds", "protected_cohort_id")
                inherited = {key: parent[key] for key in inherited_keys if key in parent}
                if any(execution.get(key, value) != value for key, value in inherited.items()):
                    raise ValueError("A child job must remain within its parent's execution grant and cutoff")
                execution.update(inherited)
                if parent.get("study_execution_id") and not validation.get("independent_countercheck"):
                    owner = self.store.get(parent["study_execution_id"], "study_execution")
                    design = self.store.get(owner["design_id"], "confirmation_design")
                    key = "diagnostic_priority" if validation.get("diagnostic_grant_id") else "validation_priority"
                    request = request.model_copy(update={"priority": design["template"][key]})
            if execution.get("execution_grant_id"):
                grant = self.resources.check(execution["execution_grant_id"], campaign["id"], 0,
                    deadline_at=execution.get("absolute_deadline"))
                execution["absolute_deadline"] = min(grant["deadline_at"], execution.get("absolute_deadline", grant["deadline_at"]))
                execution["stop_grace_seconds"] = grant["stop_grace_seconds"]
            elif set(execution) - {"protected_cohort_id"}:
                raise ValueError("Study execution admission requires a recorded resource grant")
            frozen_source = captured_source
            if execution.get("execution_source_id"):
                if captured_source is not None:
                    raise ValueError("Select one frozen execution source")
                from optimization_framework.execution.provenance import resolve
                frozen_source = resolve(self.store, execution["execution_source_id"])
            if task["campaign_id"] != campaign["id"] or (task.get("archived") and validation is None and not request.confirmation_protocol_id and not execution):
                raise ValueError("Select a current task from this campaign")
            protocol = self.store.get(request.confirmation_protocol_id, "confirmation_protocol") if request.confirmation_protocol_id else None
            prototype = None
            source_trial_id = frozen_prototype_id or (validation or {}).get("source_trial_id")
            if source_trial_id:
                if frozen_prototype_id and (not protocol or frozen_prototype_id not in protocol.get("prototypes", {}).values()):
                    raise ValueError("Source prototype is outside the frozen confirmation protocol")
                if not frozen_prototype_id and source_trial_id != (validation or {}).get("parent_trial_id"):
                    raise ValueError("A diagnostic must use the source snapshot of its recorded parent")
                prototype = self.store.get(source_trial_id, "trial")
                if prototype["campaign_id"] != campaign["id"]:
                    raise ValueError("The source prototype belongs to another campaign")
                inherited_isolation = self.execution_policy(prototype)
                if inherited_isolation:
                    if selected_isolation and selected_isolation != inherited_isolation:
                        raise ValueError("A descendant must retain its parent's frozen isolation policy")
                    selected_isolation = inherited_isolation
            selected_study_id = (validation or {}).get("study_id") or (protocol or {}).get("study_id") or execution.get("study_id") or campaign.get("active_study_id")
            if evaluator_bundle:
                self.evaluators.check_eligibility({"task_id": task["id"], "study_id": selected_study_id,
                    "evaluator_eligibility": evaluator_bundle.get("eligibility")})
            study = self.store.get(selected_study_id, "study") if selected_study_id else None
            if study and study["campaign_id"] != campaign["id"]:
                raise ValueError("Study belongs to another campaign")
            if study and validation is None and not execution.get("protocol_cell_id") and any(study["id"] in design["study_ids"].values()
                    for design in self.store.list("confirmation_design", campaign["id"])):
                raise ValueError("Use the frozen template cells or define a linked study for additional experiments")
            if study and not task_in_study(task, study):
                raise ValueError("Problem instance is outside the active frozen study")
            if study and study["scope"] == "confirmation" and validation is None and not request.confirmation_protocol_id and not execution.get("protocol_cell_id"):
                raise ValueError("Experiments in this study must use its frozen confirmation protocol")
            if not prototype and reproduction is None and request.algorithm not in {a["id"] for a in algorithms()} | {"validate", "recipe", "custom", "package"}:
                raise ValueError("Unknown executable algorithm; proposed code must be verified before execution")
            if request.algorithm == "package" and bundle is None:
                raise ValueError("Select a validated implementation version")
            if request.algorithm in {"validate", "recipe"} and validation is None:
                raise ValueError("Use the recipe/validation endpoint for independent scientific diagnostics")
            hypothesis = None
            if request.hypothesis_id:
                hypothesis = self.store.get(request.hypothesis_id, "hypothesis")
                if hypothesis["campaign_id"] != campaign["id"]:
                    raise ValueError("Hypothesis belongs to another campaign")
                if bundle and hypothesis.get("implementation_version_id") not in {None, version_id}:
                    raise ValueError("The hypothesis implementation changed while the artifact was being prepared")
            if request.algorithm == "custom":
                raise ValueError("Custom source requires independent implementation validation; commission or import a package through the campaign manager")
            problem = ProblemInstance(**task["problem"]) if task.get("problem") else problems.resolve("meent_grating", task["physics"])
            if validation is None:
                self.study_executions.check_external_exposure(campaign["id"], problem.model_dump(mode="json"), request.seed,
                    cohort_id=execution.get("protected_cohort_id") or request.confirmation_protocol_id)
            declared_assets = self.assets.inputs(campaign["id"], selected_study_id,
                request.initial_assets, request.reuse_decision_ids, cohort_id=execution.get("protected_cohort_id"))
            if len(set(request.dependencies)) != len(request.dependencies):
                raise ValueError("Declare each experiment dependency once")
            for dependency_id in request.dependencies:
                dependency = self.store.get(dependency_id, "trial")
                if dependency["campaign_id"] != campaign["id"]:
                    raise ValueError("Execution dependencies must belong to the same campaign")
            if task["split"] == "test" and validation is None and not request.confirmation_protocol_id and not execution.get("protocol_cell_id"):
                if not request.confirmatory or not hypothesis or hypothesis.get("status") != "finalist":
                    raise ValueError("Test tasks require an explicitly confirmatory run of a nominated finalist")
            allocation = {"grant_id": execution.get("execution_grant_id"), "deadline_at": execution.get("absolute_deadline")}
            self._check_allocation(campaign, request.wall_seconds, validation=validation is not None, **allocation)
            preparation = {"request": request.model_dump(mode="json"), "problem": problem.model_dump(mode="json"),
                "assets": declared_assets, "implementation": bundle["version"]["spec"] if bundle else None,
                "evaluator_manifest": task.get("evaluator_manifest")}
            if frozen_source:
                from optimization_framework.execution.provenance import invoke
                prepared = invoke(*frozen_source, "trial.prepare", preparation, isolated=selected_isolation is not None)
            elif prototype and prototype.get("execution_manifest"):
                from optimization_framework.execution.provenance import invoke
                prepared = invoke(self.job_dir(prototype["id"]), prototype["execution_manifest"], "trial.prepare",
                    preparation, isolated=selected_isolation is not None)
            else:
                from optimization_framework.execution.preparation import prepare
                prepared = prepare(request, problem, declared_assets, preparation["implementation"],
                    evaluator_manifest=preparation["evaluator_manifest"])
            if reproduction is not None:
                original = self.reproductions.check_procedure(request.campaign_id,
                    ReproductionIntent(**{key: reproduction[key] for key in ("reference", "comparison")}),
                    {**request.model_dump(mode="json"), **{key: prepared[key] for key in
                        ("algorithm_config", "training", "completion", "diagnostics")}})
                if problem.model_dump(mode="json") != original["experiment_spec"]["problem"]:
                    raise ValueError("Reproduction problem or evaluator changed before freezing")
            request = request.model_copy(update={"algorithm_config": prepared["algorithm_config"]})
            training = prepared["training"]
            completion = CompletionCondition(**prepared["completion"])
            from optimization_framework.contracts.diagnostics import DiagnosticSchedule
            schedules = [DiagnosticSchedule(**raw) for raw in prepared["diagnostics"]]
            diagnostic_allocation = sum(len(schedule.at_counts) * schedule.allocation() for schedule in schedules)
            if diagnostic_allocation:
                self._check_allocation(campaign, request.wall_seconds + diagnostic_allocation, validation=True, **allocation)
            record = request.model_dump()
            if prepared.get("inference_adapter"):
                record["inference_adapter"] = prepared["inference_adapter"]
            record["diagnostics"] = [schedule.model_dump(mode="json") for schedule in schedules]
            from optimization_framework.evaluation.legacy_confirmation import scientific_environment
            record["scientific_environment"] = scientific_environment()
            from optimization_framework.execution.source import scientific_hash
            record["scientific_source_hash"] = scientific_hash()
            record.update(id=identifier("trial"), charter_version=campaign["version"],
                          task_name=task["name"], task_split=task["split"], physics=task["physics"], training=training,
                          problem=problem.model_dump(mode="json"), execution_contract=1,
                          study_id=selected_study_id,
                          declared_assets=declared_assets,
                          research_cost_asset_ids=(hypothesis or {}).get("research_cost_asset_ids", []),
                          recovery=request.recovery.model_dump(mode="json"),
                          completion=completion.model_dump(exclude={"schema_version"}),
                          schedule_steps=request.schedule_steps or request.max_steps,
                          status="queued", created_at=now(), updated_at=now(),
                          control_revision=0, attempt=0, execution_seconds=0.0,
                          progress={}, result=None, validation=None, parent_trial_id=None,
                          validation_orders=[], validation_tolerance=0.005, archive_size=10)
            if validation:
                record.update(validation)
            record.update(execution)
            if selected_isolation:
                record["isolation_policy"] = selected_isolation.model_dump(mode="json")
            if reproduction is not None:
                record["reproduction"] = reproduction
            if prototype:
                if not prototype.get("execution_manifest") and prototype["scientific_environment"] != record["scientific_environment"]:
                    raise ValueError("The prototype's frozen runtime is unavailable; no confirmation work was allocated")
                record["scientific_environment"] = prototype["scientific_environment"]
                record["scientific_source_hash"] = prototype["scientific_source_hash"]
                record["research_cost_asset_ids"] = prototype.get("research_cost_asset_ids", [])
                record["source_prototype_id"] = source_trial_id
            confirmation = None
            if task["split"] == "test" and validation is None and not request.confirmation_protocol_id and not execution.get("protocol_cell_id"):
                from optimization_framework.evaluation.legacy_confirmation import prepare_confirmation
                confirmation = prepare_confirmation(self.store, campaign, task, hypothesis, record)
                record.update(confirmation["trial_fields"])
            directory = self.job_dir(record["id"])
            directory.mkdir(parents=True, exist_ok=False)
            if declared_assets:
                from optimization_framework.storage.artifacts import LocalArtifactStore
                from optimization_framework.contracts.experiments import ArtifactReference
                inputs = LocalArtifactStore(directory / "inputs")
                for asset in declared_assets:
                    for raw in asset["artifacts"]:
                        reference = ArtifactReference(**raw)
                        with self.assets.artifacts.open(reference) as stream:
                            copied = inputs.put_stream(stream, media_type=reference.media_type)
                        if copied.sha256 != reference.sha256:
                            raise ValueError("Input artifact changed while pinning the experiment")
            if bundle:
                record.update(self.implementations.pin(directory, bundle))
                self.implementations._exposure(campaign["id"], bundle["version"])
                if prototype:
                    if record["implementation_artifact_digest"] != prototype.get("implementation_artifact_digest"):
                        raise ValueError("The implementation no longer matches its frozen prototype")
                    record["implementation_cost_asset_ids"] = prototype.get("implementation_cost_asset_ids", [])
            if evaluator_bundle:
                record.update(self.evaluators.pin(directory, evaluator_bundle))
                if prototype and record["evaluator_artifact_digest"] != prototype.get("evaluator_artifact_digest"):
                    raise ValueError("The evaluator no longer matches its frozen prototype")
            from optimization_framework.implementations.runtime import bundle_runtime_root, verify_runtime
            conversions = {}
            for prefix, executable_bundle in (("implementation", bundle), ("evaluator", evaluator_bundle)):
                if executable_bundle:
                    converted = verify_runtime(bundle_runtime_root(executable_bundle), executable_bundle["artifact"]["runtime"]).get("conversion")
                    if converted:
                        conversions[prefix] = converted
            if conversions:
                record["runtime_conversions"] = conversions
            if prototype and conversions != prototype.get("runtime_conversions", {}):
                raise ValueError("A descendant must retain its parent's frozen runtime conversion")
            if reproduction is not None:
                for prefix, binding in reproduction["executable_bindings"].items():
                    if any(record.get(prefix + "_" + key) != value for key, value in binding.items()):
                        raise ValueError("Reproduction executable changed before freezing")
                if sorted(conversions.values(), key=lambda row: row["id"]) != sorted(reproduction["runtime_conversions"], key=lambda row: row["id"]):
                    raise ValueError("Reproduction runtime conversion changed before freezing")
            record["contribution_asset_ids"] = sorted(set(record.get("implementation_cost_asset_ids", []) + record.get("research_cost_asset_ids", []) + record.get("evaluator_cost_asset_ids", [])))
            if frozen_source:
                from optimization_framework.execution.source import copy_snapshot
                from optimization_framework.execution.provenance import copy_manifest
                source_directory, manifest = frozen_source
                record["source_hash"] = copy_snapshot(source_directory, directory, snapshot(source_directory))
                record["execution_manifest"] = copy_manifest(source_directory, directory, manifest)
                record["scientific_source_hash"] = manifest["scientific_digest"]
                record["scientific_environment"] = manifest["runtime"]
                record["method_contract"] = 3
            elif prototype:
                from optimization_framework.execution.source import copy_snapshot
                record["source_hash"] = copy_snapshot(self.job_dir(prototype["id"]), directory, prototype["source_hash"])
                if prototype.get("execution_manifest"):
                    from optimization_framework.execution.provenance import copy_manifest
                    record["execution_manifest"] = copy_manifest(self.job_dir(prototype["id"]), directory, prototype["execution_manifest"])
                    if prototype.get("method_contract"):
                        record["method_contract"] = prototype["method_contract"]
                elif scientific_hash(directory / "code") != record["scientific_source_hash"]:
                    raise ValueError("The prototype's scientific source identity changed")
            elif confirmation:
                from optimization_framework.evaluation.legacy_confirmation import commit_confirmation, snapshot_confirmation_code
                record["source_hash"] = snapshot_confirmation_code(directory, record["scientific_source_hash"])
                commit_confirmation(self.store, confirmation)
            else:
                record["source_hash"] = self._snapshot_code(directory)
                from optimization_framework.execution.provenance import capture
                record["execution_manifest"] = capture(directory, problem_ids=[] if evaluator_bundle else [problem.definition_id],
                    recipe_ids=evaluator_bundle["version"]["spec"]["manifest"].get("recipe_ids", []) if evaluator_bundle else ())
                record["scientific_source_hash"] = record["execution_manifest"]["scientific_digest"]
                record["scientific_environment"] = record["execution_manifest"]["runtime"]
                record["method_contract"] = 2
            if request.confirmation_protocol_id:
                record.update(self.confirmations.prepare(request.confirmation_protocol_id, record))
            if record["algorithm"] in {item["id"] for item in algorithms()} | {"validate", "recipe"} or (reproduction and not bundle):
                record["builtin_implementation_id"] = f"builtin:{record['algorithm']}:{record['source_hash']}"
            entries = []
            if record.get("execution_contract") == 1:
                if not record["study_id"]:
                    # Existing campaigns retain an explicitly retrospective
                    # scope until the researcher defines their next study.
                    study = StudySpec(id=identifier("study"), campaign_id=campaign["id"], goal=campaign["objective"],
                        scope="historical", instance_ids=[task.get("problem_instance_id", task["id"])],
                        created_at=now(), authority="legacy_mapping")
                    study_record = study.model_dump(mode="json")
                    entries.append(("study", {**study_record, "content_hash": content_hash(study_record)}, "study.mapped"))
                    campaign["active_study_id"] = record["study_id"] = study.id
                    entries.append(("campaign", campaign, None))
                method = ImplementationVersion(id=record.get("implementation_version_id") or record["builtin_implementation_id"],
                    name=record["algorithm"], source_digest=record.get("implementation_artifact_digest") or record["source_hash"],
                    runtime_digest=record.get("implementation_runtime_digest") or content_hash(record["scientific_environment"]),
                    representations=[problem.candidate_schema.representation],
                    validation_ids=[record["validation_report_id"]] if record.get("validation_report_id") else [])
                frozen = ExperimentSpec(id=record["id"] + "_spec", campaign_id=campaign["id"], study_id=record["study_id"],
                    problem=problem, implementation=method, parameters={key: record[key] for key in
                        ("algorithm", "algorithm_config", "training", "recipe") if key in record}, seed=record["seed"],
                    schedule={"steps": record["schedule_steps"], "numerical_threads": record["numerical_threads"], **({"evaluator": {key: record[key] for key in
                        ("evaluator_version_id", "evaluator_artifact_digest", "evaluator_runtime_digest")}} if evaluator_bundle else {}),
                        **({"isolation_policy": record["isolation_policy"]} if selected_isolation else {}),
                        **({"reproduction": reproduction} if reproduction is not None else {}),
                        **({"runtime_conversions": conversions} if conversions else {}),
                        **({"evaluator_eligibility": record["evaluator_eligibility"]} if record.get("evaluator_eligibility") else {}),
                        **({"absolute_deadline": record["absolute_deadline"],
                        "execution_grant_id": record["execution_grant_id"], "stop_grace_seconds": record["stop_grace_seconds"]}
                        if record.get("execution_grant_id") else {}),
                        **({"logical_method": record["logical_method"], "protocol_cell_id": record["protocol_cell_id"]}
                           if record.get("logical_method") else {}),
                        **({"execution_manifest_digest": content_hash(record["execution_manifest"]),
                        "method_contract": record.get("method_contract", 1)} if record.get("execution_manifest") else {})},
                    recovery=record["recovery"], completion=record["completion"],
                    initial_assets=record["initial_assets"],
                    contribution_asset_ids=record["contribution_asset_ids"],
                    dependencies=record["dependencies"],
                    diagnostics=record["diagnostics"],
                    asset_digests={item["id"]: item["content_hash"] for item in declared_assets},
                    initial_wall_seconds=record["wall_seconds"], created_at=record["created_at"],
                    extension_policy="forbidden" if record.get("confirmation_protocol_hash") or record.get("execution_grant_id") else "explicit_amendment")
                frozen_record = frozen.model_dump(mode="json")
                record["experiment_spec_id"] = frozen.id
                record["experiment_spec_hash"] = content_hash(frozen_record)
                record["experiment_spec"] = frozen_record
                entries.append(("experiment_spec", {**frozen_record, "content_hash": record["experiment_spec_hash"]}, "experiment.frozen"))
            atomic_json(directory / "spec.json", record)
            self.store.put_many([*entries, ("trial", record, "trial.queued")])
            self.resources.reserve_trial(record)
            from optimization_framework.evaluation.diagnostics import reserve as reserve_diagnostics
            reserve_diagnostics(self.store, record)
            return record

    def validate_trial(self, trial_id, request: ValidationInput):
        trial = self.store.get(trial_id, "trial")
        if trial.get("recipe") or trial["algorithm"] in {"validate", "recipe"}:
            raise ValueError("Select an optimization trial")
        archive = trial.get("progress", {}).get("archive") or (trial.get("result") or {}).get("archive") or read_json(self.job_dir(trial_id) / "archive.json", [])
        if isinstance(archive, dict):
            archive = archive.get("designs", archive.get("archive", []))
        if not archive:
            design = trial.get("progress", {}).get("best_design")
            if not design:
                raise ValueError("No completed design is available to validate")
            archive = [design]
        chosen = archive[:request.max_designs]
        problem = trial.get("problem") or problems.resolve("meent_grating", trial["physics"]).model_dump(mode="json")
        recipe = self.compile_recipe({**trial, "problem": problem}, "fourier_convergence:v1", {"orders": request.orders, "tolerance": request.tolerance},
            [item.get("candidate", item.get("design")) if isinstance(item, dict) else item for item in chosen])
        return self._queue_recipe(trial, recipe, TrialInput(campaign_id=trial["campaign_id"], task_id=trial["task_id"],
            algorithm="validate", algorithm_config={"designs": chosen}, seed=trial["seed"],
            max_steps=len(chosen)*len(request.orders), wall_seconds=request.wall_seconds,
            question=f"Validate the top {len(chosen)} archived designs across Fourier orders."),
            extra={"validation_orders": request.orders, "validation_tolerance": request.tolerance})

    def run_recipe(self, trial_id, request: RecipeInput, *, authority="researcher", require_only=False):
        trial = self.store.get(trial_id, "trial")
        if trial.get("recipe") or trial["algorithm"] in {"recipe", "validate"}:
            raise ValueError("Select an optimization experiment as the source")
        evidence = trial.get("progress") or trial.get("result") or {}
        archive = evidence.get("archive", [])
        subjects = [item.get("candidate", item.get("design")) for item in archive[:request.subject_limit]]
        problem = trial.get("problem") or problems.resolve("meent_grating", trial["physics"]).model_dump(mode="json")
        recipe = self.compile_recipe(trial, request.recipe_id, request.parameters, subjects)
        return self._queue_recipe(trial, recipe, TrialInput(campaign_id=trial["campaign_id"], task_id=trial["task_id"],
            algorithm="recipe", seed=trial["seed"], max_steps=len(recipe["cases"]), wall_seconds=request.wall_seconds,
            question=f"Run {request.recipe_id} on {len(subjects)} declared observed candidates."),
            authority=authority, require_only=require_only)

    def describe_trial_problem(self, trial_id):
        trial = self.store.get(trial_id, "trial")
        manifest = trial.get("execution_manifest", {})
        if "problem.describe" in manifest.get("operations", []):
            from optimization_framework.execution.provenance import invoke
            return {"definition": invoke(self.job_dir(trial_id), manifest, "problem.describe", {"problem": trial["problem"]},
                    isolated=self.execution_policy(trial) is not None),
                "basis": "captured_source"}
        # Older captures have no catalog operation. Expose a clearly identified
        # compatibility view only for the exact recorded instance. Execution
        # continues through that experiment's compiler and own eligibility gates.
        task = self.evaluators.task_view(self.store.get(trial["task_id"], "task"))
        registry = self.evaluators.registry_for(task)
        problem = ProblemInstance.model_validate(trial["problem"])
        adapter = registry.get(problem.definition_id)
        if adapter.resolve(problem.configuration, problem.fidelity) != problem:
            raise ValueError("The historical problem version has no compatible catalog; inspect its captured evidence")
        definition = adapter.describe().model_dump(mode="json")
        if trial.get("evaluator_version_id") and "recipe_entry_points" not in manifest:
            for schema in definition["recipe_schemas"].values():
                schema.update(available=False, unavailable_reason="This historical evaluator experiment did not capture registered recipes; create a new experiment")
        return {"definition": definition, "basis": "compatible_current_definition"}

    def compile_recipe(self, trial, recipe_id, parameters, subjects):
        from optimization_framework.evaluation.recipes import compile_recipe
        if trial.get("execution_manifest"):
            from optimization_framework.execution.provenance import invoke
            if trial.get("evaluator_version_id") and "recipe_entry_points" not in trial["execution_manifest"]:
                raise ValueError("This historical evaluator experiment did not capture registered recipes; create a new experiment")
            return invoke(self.job_dir(trial["id"]), trial["execution_manifest"], "recipe.compile",
                {"problem": trial["problem"], "recipe_id": recipe_id, "parameters": parameters, "subjects": subjects},
                isolated=self.execution_policy(trial) is not None)
        # Legacy procedures did not capture a compiler manifest. Preserve their
        # explicit compatibility behavior rather than inventing an archived one.
        return compile_recipe(trial["problem"], recipe_id, parameters, subjects)

    def compile_inference(self, trial, rollout, assets):
        manifest = trial.get("execution_manifest")
        if manifest and "inference.compile" in manifest["operations"]:
            from optimization_framework.execution.provenance import invoke
            return invoke(self.job_dir(trial["id"]), manifest, "inference.compile",
                {"problem": trial["problem"], "rollout": rollout, "assets": assets},
                isolated=self.execution_policy(trial) is not None)
        if rollout.get("kind") == "artifact_inference:v1":
            raise ValueError("This historical procedure did not capture an inference compiler")
        # The pre-registry contract admitted only the versioned DQN rollout.
        from optimization_framework.evaluation.inference import compile_inference
        return compile_inference(trial["problem"], rollout, assets)

    def _queue_recipe(self, trial, recipe, request, *, extra=None, authority="researcher", require_only=False, frozen_subjects=None):
        from optimization_framework.evaluation.jobs import snapshot_solutions, requirements
        if trial.get("race_id"):
            request = request.model_copy(update={"race_id": trial["race_id"], "race_phase": "validation" if trial.get("race_phase") == "confirmation" else trial.get("race_phase", "preflight"),
                "numerical_threads": trial.get("numerical_threads", 1)})
        with self.lock, self.store.transaction():
            candidates = recipe.get("subjects", [])
            subjects = (frozen_subjects if frozen_subjects is not None else snapshot_solutions(self, trial, candidates)) if candidates and recipe.get("validation_rule", {}).get("subject") != "evaluator" else []
            recipe["subject_asset_ids"] = [item["id"] for item in subjects]
            required = requirements(self, trial, recipe, subjects, authority)
            if require_only:
                if not required:
                    raise ValueError("This diagnostic has no validation assertion to require or waive")
                return {"requirement_ids": required, "subject_asset_ids": recipe["subject_asset_ids"]}
            return self.create_trial(request, validation={"parent_trial_id": trial["id"], "recipe": recipe,
                "study_id": trial.get("study_id"), "validation_requirement_ids": required,
                "recipe_subject_asset_ids": recipe["subject_asset_ids"],
                **({"independent_countercheck": True} if trial.get("study_execution_id") and authority not in
                    {"frozen_study_template", "frozen_diagnostic_procedure"} else {}),
                **({"source_trial_id": trial["id"]} if trial.get("execution_manifest") else {}), **(extra or {})})

    def run_requirement(self, requirement_id, *, wall_seconds=120):
        requirement = self.store.get(requirement_id, "validation_requirement")
        recipe = requirement["scope"].get("recipe")
        if not recipe:
            raise ValueError("This requirement has no executable recipe; its owning validation service must supply evidence")
        trial = self.store.get(requirement["scope"]["parent_trial_id"], "trial")
        required = [item["id"] for item in self.store.list("validation_requirement", trial["campaign_id"])
                    if item["study_id"] == requirement["study_id"] and item["scope"].get("recipe") == recipe]
        required.sort(key=lambda identity: self.store.get(identity, "validation_requirement")["scope"]["subject_index"])
        return self.create_trial(TrialInput(campaign_id=trial["campaign_id"], task_id=trial["task_id"], algorithm="recipe",
            seed=trial["seed"], max_steps=len(recipe["cases"]), wall_seconds=wall_seconds,
            question="Execute the frozen validation requirement " + requirement_id),
            validation={"parent_trial_id": trial["id"], "recipe": recipe, "study_id": requirement["study_id"],
                "validation_requirement_ids": required, "recipe_subject_asset_ids": recipe.get("subject_asset_ids", []),
                **({"independent_countercheck": True} if trial.get("study_execution_id") else {}),
                **({"source_trial_id": trial["id"]} if trial.get("execution_manifest") else {})})

    def control(self, trial_id, command: ControlInput, *, authority="researcher", reason=None):
        """authority is who acted: researcher, manager, or a budget/deadline rule that stopped the trial."""
        with self.lock, self.store.transaction():
            trial = self.store.get(trial_id, "trial")
            self.racing.guard_control(trial, command)
            if trial.get("confirmation_protocol_id") and command.action in {"resume", "extend"} and any(
                    item["protocol_id"] == trial["confirmation_protocol_id"] for item in self.store.list("confirmation_release", trial["campaign_id"])):
                raise ValueError("This confirmation protocol is closed; preserve its result and create a new study")
            previous_allocation = (trial["max_steps"], trial["wall_seconds"])
            status = trial["status"]
            if (trial.get("confirmation_protocol_hash") or trial.get("execution_grant_id")) and (
                (command.max_steps is not None and command.max_steps != trial["max_steps"]) or
                (command.wall_seconds is not None and command.wall_seconds != trial["wall_seconds"])
            ):
                raise ValueError("A confirmatory allocation is frozen; fork a new exploratory trial instead of extending it")
            if command.action == "prioritize":
                if status != "queued":
                    raise ValueError("Only queued trials can be reprioritized")
                trial["priority"] = command.priority if command.priority is not None else trial["priority"] + 1
            elif command.action == "stop":
                if status not in ACTIVE | {"paused", "interrupted"}:
                    return trial
                if reason is None:
                    reason = ("Stopped by researcher" if authority == "researcher" else
                        ("Stopped by the lead agent" if self.pi.owns(trial["campaign_id"]) else "Stopped by the campaign manager")
                        if authority == "manager" else f"Stopped by {authority}")
                trial.update(status="stopping" if alive(trial) else "stopped" if authority in DELIBERATE_STOPS else "budget_exhausted",
                             stop_requested_at=time.time(), stopped_by=authority, reason=reason)
            elif command.action == "pause":
                if status not in {"queued", "running"}:
                    raise ValueError("Only queued or running trials can be paused")
                trial.update(status="pausing" if alive(trial) else "paused", pause_requested_at=time.time())
            elif command.action in {"resume", "extend"}:
                if status in {"stopping", "pausing"}:
                    raise ValueError("Wait for the pending control command to finish")
                if command.action == "resume" and status not in {"paused", "interrupted", "stopped", "failed"}:
                    raise ValueError("This trial is not resumable in its current state")
                if status not in ACTIVE and trial["attempt"] and not trial.get("progress", {}).get("checkpoint_available"):
                    raise ValueError("No compatible checkpoint exists; create a new trial")
                if trial["attempt"] and trial.get("progress", {}).get("resume_supported") is False:
                    raise ValueError("This worker failure has no consistent resumable state; fork a new trial")
                if command.max_steps is not None:
                    if command.max_steps < trial["max_steps"]:
                        raise ValueError("An extension cannot decrease the original request budget")
                    trial["max_steps"] = command.max_steps
                if command.wall_seconds is not None:
                    if command.wall_seconds < trial["wall_seconds"]:
                        raise ValueError("An extension cannot decrease the original time budget")
                    trial["wall_seconds"] = command.wall_seconds
                if trial["max_steps"] <= trial.get("progress", {}).get("budget_requests", trial.get("progress", {}).get("step", 0)):
                    raise ValueError("Increase evaluation budget before resuming a finished trial")
                from optimization_framework.execution.resources import budget_spent
                if trial["wall_seconds"] <= budget_spent(trial):
                    raise ValueError("Increase time budget before resuming")
                campaign = self.store.get(trial["campaign_id"], "campaign")
                pending_diagnostics = [grant for grant in self.store.list("diagnostic_grant", trial["campaign_id"])
                    if grant["parent_trial_id"] == trial_id and grant["status"] in {"reserved", "not_reached"}]
                allocation = {"grant_id": trial.get("execution_grant_id"), "deadline_at": trial.get("absolute_deadline")}
                self._check_allocation(campaign, trial["wall_seconds"], exclude=trial_id,
                                       validation=trial["algorithm"] in {"validate", "recipe"} or bool(trial.get("diagnostic_grant_id")), **allocation)
                self._check_allocation(campaign, trial["wall_seconds"] + sum(grant["reserved_seconds"] for grant in pending_diagnostics),
                    exclude=trial_id, validation=True, **allocation)
                if status not in LIVE:
                    trial.update(status="queued", stopped_by=None, reason=None)
                # Original schedule_steps never changes when budget is extended.
            trial["control_revision"] += 1
            trial["updated_at"] = now()
            entries = []
            if command.action in {"resume", "extend"}:
                for grant in pending_diagnostics:
                    if grant["status"] == "not_reached":
                        grant.update(status="reserved")
                        grant.pop("finished_at", None)
                        entries.append(("diagnostic_grant", grant, "diagnostic.reserved"))
            if (trial["max_steps"], trial["wall_seconds"]) != previous_allocation:
                amendment = BudgetAmendment(id=identifier("amendment"), experiment_id=trial_id,
                    campaign_id=trial["campaign_id"], previous_count=previous_allocation[0], previous_wall_seconds=previous_allocation[1],
                    count=trial["max_steps"], wall_seconds=trial["wall_seconds"], authority=authority,
                    rationale=command.rationale, created_at=now()).model_dump(mode="json")
                entries.append(("budget_amendment", {**amendment, "content_hash": content_hash(amendment)}, "budget.amended"))
            effect = {"id": f"control_{trial['id']}_{trial['control_revision']}", "campaign_id": trial["campaign_id"],
                "kind": "worker_control", "trial_id": trial["id"], "revision": trial["control_revision"],
                "status": "pending", "created_at": now()}
            self.store.put_many([*entries, ("trial", trial, "trial.control"), ("outbox", effect, "effect.queued")])
        if not self.store.in_transaction:
            self.dispatch_outbox()
        return trial

    def dispatch_outbox(self):
        if self.shutdown_event.is_set() or self.store.in_transaction or not self.outbox_lock.acquire(blocking=False):
            return
        try:
            for effect in self.store.list("outbox"):
                if effect["status"] != "pending":
                    continue
                try:
                    if effect["kind"] == "worker_control":
                        with self.lock:
                            trial = self.store.get(effect["trial_id"], "trial")
                            # Delayed controls always project the latest intent.
                            self._write_control(trial)
                            effect["applied_revision"] = trial["control_revision"]
                    elif effect["kind"] == "manager_context_projection":
                        # Text is a projection of committed context, never a
                        # second authority or an uncommitted command effect.
                        revision = self.memory.sync(effect["campaign_id"])
                        effect["context_id"] = revision["id"]
                    elif effect["kind"] == "implementation_submit":
                        self.implementations.deliver_submission(effect["grant_id"])
                    elif effect["kind"] == "implementation_attach":
                        from optimization_framework.implementations.reuse import decide
                        decide(self.implementations, identity="reuse_" + effect["id"], campaign_id=effect["campaign_id"],
                            study_id=self.store.get(effect["campaign_id"], "campaign")["active_study_id"],
                            hypothesis_id=effect["hypothesis_id"], version_id=effect["version_id"], decision="reuse",
                            rationale="Selected this exact library version for the idea", authority=effect.get("authority", "researcher"),
                            expected_context=tuple(effect["expected_context"]))
                    elif effect["kind"] == "evaluator_attach":
                        from optimization_framework.implementations.reuse import decide
                        decide(self.implementations, identity="reuse_" + effect["id"], campaign_id=effect["campaign_id"],
                            study_id=self.store.get(effect["campaign_id"], "campaign")["active_study_id"],
                            task_id=effect["task_id"], version_id=effect["version_id"], decision="reuse", rationale=effect["rationale"],
                            authority=effect["authority"], expected_context=tuple(effect["expected_context"]))
                    elif effect["kind"] == "implementation_reuse":
                        from optimization_framework.implementations.reuse import decide
                        decide(self.implementations, **{key: effect[key] for key in ("identity", "campaign_id", "study_id", "version_id",
                            "decision", "rationale", "authority", "hypothesis_id", "task_id")}, expected_context=tuple(effect["expected_context"]))
                    elif effect["kind"] == "implementation_control":
                        self.implementations.deliver_control(effect)
                    elif effect["kind"] == "implementation_resolve_runtime":
                        receipt = self.implementations.resolve_runtime(effect)
                        effect["receipt_id"] = receipt["id"]
                    elif effect["kind"] == "bundle_operation":
                        operation = self.store.get(effect["operation_id"], "bundle_operation")
                        if operation["status"] == "queued":
                            try:
                                self.check_manager_context(effect["campaign_id"], tuple(effect["expected_context"]))
                            except ValueError as exc:
                                with self.lock, self.store.transaction():
                                    self.bundles.update(operation, status="failed", error=str(exc), finished_at=now())
                                    effect.update(status="failed", error=str(exc), finished_at=now())
                                    self.store.put("outbox", effect, "effect.failed")
                                    self.memory.issue(effect["campaign_id"], "bundle_operation", str(exc), affected=operation["id"])
                                continue
                        self.bundles.dispatch(effect)
                    elif effect["kind"] == "manager_start":
                        from optimization_framework.campaigns.manager import CampaignManager
                        manager = getattr(self, "manager", None) or CampaignManager(self)
                        manager.start(ResearchInput(**effect["request"]), automatic=effect["automatic"],
                                      command_id=effect["manager_command_id"], guidance_recorded=effect.get("guidance_recorded", False),
                                      feedback_snapshot=effect.get("feedback_snapshot"))
                    elif effect["kind"] in {"research_resume", "decision_resolution"}:
                        from optimization_framework.campaigns.research_commands import deliver_resume, deliver_resolution
                        (deliver_resume if effect["kind"] == "research_resume" else deliver_resolution)(self, effect)
                    elif effect["kind"] == "manager_action":
                        from optimization_framework.research.lifecycle import deliver_action
                        deliver_action(self, effect)
                        continue
                    elif effect["kind"] in {"literature_search", "source_ingest"}:
                        from optimization_framework.research.sources import schedule
                        schedule(self, effect)
                        continue
                    else:
                        raise ValueError("Unknown durable effect: " + effect["kind"])
                    effect.update(status="completed", finished_at=now())
                    self.store.put("outbox", effect, "effect.applied")
                except Exception as exc:
                    if effect["kind"] == "decision_resolution" and isinstance(exc, (ValueError, KeyError)):
                        from optimization_framework.campaigns.research_commands import failed_resolution
                        failed_resolution(self, effect, exc)
                    elif effect["kind"] in {"literature_search", "source_ingest"} and isinstance(exc, ValueError):
                        effect.update(status="failed", error=str(exc), finished_at=now())
                        self.store.put("outbox", effect, "effect.failed")
                    self.memory.issue(effect["campaign_id"], "control_delivery" if effect["kind"] == "worker_control" else "command_delivery",
                        str(exc), affected=effect.get("trial_id", effect.get("grant_id", effect["id"])))
        finally:
            self.outbox_lock.release()

    def dependencies_ready(self, trial):
        waiting = []
        failed = []
        for dependency_id in trial.get("dependencies", []):
            parent = self.store.get(dependency_id, "trial")
            if parent["status"] in {"queued", "running", "pausing", "stopping", "paused", "interrupted"}:
                waiting.append(dependency_id)
            elif parent["status"] != "completed" or not (parent.get("result") or parent.get("progress", {})).get("scientific_complete"):
                failed.append(dependency_id)
        if failed:
            self.memory.issue(trial["campaign_id"], "dependency_incomplete",
                "An experiment dependency did not satisfy its scientific completion condition. Resolve or replace it before launching dependent work.",
                affected=trial["id"], evidence=failed)
        return not waiting and not failed

    def _write_control(self, trial):
        command = "stop" if trial["status"] in {"stopping", "stopped"} else "pause" if trial["status"] in {"pausing", "paused"} else "run"
        atomic_json(self.job_dir(trial["id"]) / "control.json", {
            "command": command, "max_steps": trial["max_steps"], "wall_seconds": trial["wall_seconds"],
            "revision": trial["control_revision"]})

    @staticmethod
    def _snapshot_code(directory):
        return snapshot(directory)

    def execution_policy(self, trial):
        from optimization_framework.execution.supervision import policy
        return policy(self, trial)

    def _start_trial(self, trial):
        directory = self.job_dir(trial["id"])
        selected_isolation = self.execution_policy(trial)
        if trial.get("absolute_deadline") and time.time() >= trial["absolute_deadline"]:
            raise ValueError("The study's fixed execution deadline has passed")
        self.implementations.check_launch(trial)
        self.evaluators.check_launch(trial)
        from optimization_framework.implementations.runtime_conversion import check_frozen
        check_frozen(self, trial)
        from optimization_framework.evaluation.legacy_confirmation import scientific_environment
        if trial.get("execution_manifest"):
            from optimization_framework.execution.provenance import verify
            verify(directory, trial["execution_manifest"])
        elif trial.get("scientific_environment") and scientific_environment() != trial["scientific_environment"]:
            label = "the confirmation" if trial.get("confirmation_protocol_hash") else "this experiment"
            raise ValueError(f"Numerical dependencies changed after {label} was queued; no worker was started")
        if trial.get("confirmation_protocol_hash") and not trial.get("execution_manifest"):
            from optimization_framework.evaluation.legacy_confirmation import scientific_source_hash
            if scientific_source_hash(directory / "code" / "dqn_meent") != trial.get("scientific_source_hash"):
                raise ValueError("The frozen confirmation source snapshot changed; no worker was started")
        if (directory / "result.json").exists():
            (directory / "result.json").rename(directory / f"result.attempt-{trial['attempt']}.json")
        trial["attempt"] += 1
        from optimization_framework.execution.resources import budget_spent
        trial.update(status="running", attempt_started_at=time.time(),
                     prior_execution_seconds=trial.get("execution_seconds", 0),
                     prior_budget_execution_seconds=budget_spent(trial), updated_at=now())
        self._write_control(trial)
        # New trials pin at enqueue. Historical trials without a snapshot pin once.
        code = directory / "code"
        if not code.exists():
            trial["source_hash"] = self._snapshot_code(directory)
        elif not trial.get("confirmation_protocol_hash") and trial.get("source_hash") != self._snapshot_code(directory):
            raise ValueError("The queued implementation source snapshot changed; no worker was started")
        worker_spec = ({**trial, "execution_seconds": trial["prior_budget_execution_seconds"]}
                       if selected_isolation else trial)
        atomic_json(directory / "spec.json", worker_spec)
        trial["lease_deadline"] = time.time() + 10
        self.store.put("trial", trial, "trial.launch_prepared")
        if selected_isolation:
            from optimization_framework.execution.supervision import launch
            process = launch(self, trial, selected_isolation)
        else:
            env = os.environ.copy()
            # Numerical jobs have no need to inherit LLM credentials.
            for key in list(env):
                if key.startswith("GRATING_LLM_") or key in {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY"}:
                    env.pop(key)
            threads = str(trial.get("numerical_threads", 1))
            env.update(PYTHONPATH=str(code), PYTHONUNBUFFERED="1", OPENBLAS_NUM_THREADS=threads,
                       OMP_NUM_THREADS=threads, MKL_NUM_THREADS=threads, MPLBACKEND="Agg")
            with (directory / "worker.log").open("ab") as log:
                module = "optimization_framework.execution.worker" if trial.get("execution_contract") == 1 else "dqn_meent.workspace.worker"
                process = subprocess.Popen([sys.executable, "-m", module, "--directory", str(directory)],
                    cwd=directory, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        trial.update(pid=process.pid, process_identity=process_identity(process.pid))
        trial.pop("lease_deadline", None)
        self.processes[trial["id"]] = process
        self.store.put("trial", trial, "trial.started")

    def _terminate(self, trial):
        if alive(trial):
            try:
                if os.getpgid(trial["pid"]) == trial["pid"]:
                    os.killpg(trial["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _deadline_enforcement(self, trial, directory):
        receipt = read_json(directory / "deadline-enforcement.json")
        if not receipt:
            return None
        from optimization_framework.execution.worker import fingerprint
        lease = read_json(directory / "worker-lease.json", {})
        expected = {"experiment_id": trial["id"], "attempt": trial["attempt"], "fingerprint": fingerprint(trial),
            "pid": trial.get("pid", lease.get("pid")), "process_identity": trial.get("process_identity", lease.get("process_identity")),
            "absolute_deadline": trial.get("absolute_deadline"), "grace_seconds": trial.get("stop_grace_seconds", 5)}
        if (any(receipt.get(key) != value for key, value in expected.items())
                or not receipt.get("attempt_id") or receipt.get("enforced_at", 0) < (trial.get("absolute_deadline") or float("inf"))
                or receipt.get("elapsed_seconds", -1) < 0 or receipt.get("signal") != int(signal.SIGKILL)):
            self.memory.issue(trial["campaign_id"], "deadline_receipt_invalid", "A termination receipt does not match this frozen attempt.", affected=trial["id"])
            return None
        record = {**receipt, "id": "deadline_" + content_hash([trial["id"], trial["attempt"]]), "campaign_id": trial["campaign_id"]}
        return self.store.put_immutable("deadline_enforcement", record, "trial.deadline_enforced")

    def reconcile(self):
        with self.lock:
            for trial in self.store.list_trials_in_status(LIVE | {"queued", "paused", "interrupted"}):
                if trial.get("absolute_deadline") and time.time() >= trial["absolute_deadline"] and trial["status"] in {"queued", "paused", "interrupted"}:
                    trial.update(status="budget_exhausted", stopped_by="deadline", reason="Study execution deadline reached",
                        allocation_stop="study_deadline_reached", finished_at=now())
                    self.store.put("trial", trial, "trial.deadline_reached")
                if trial["status"] not in LIVE:
                    continue
                directory = self.job_dir(trial["id"])
                try:
                    selected_isolation = self.execution_policy(trial)
                except (ValueError, KeyError) as exc:
                    self.memory.issue(trial["campaign_id"], "execution_policy_invalid", str(exc), affected=trial["id"])
                    continue
                if trial.get("execution_contract") == 1 and not alive(trial):
                    if selected_isolation:
                        from optimization_framework.execution import supervision
                        try:
                            lease = supervision.lease(self, trial, selected_isolation)
                        except ValueError as exc:
                            self.memory.issue(trial["campaign_id"], "execution_host_invalid", str(exc), affected=trial["id"])
                            continue
                    else:
                        lease = read_json(directory / "worker-lease.json", {})
                    if lease and lease.get("experiment_id") == trial["id"] and process_identity(lease.get("pid")) == lease.get("process_identity"):
                        from optimization_framework.execution.worker import fingerprint
                        if lease.get("fingerprint") == fingerprint(trial):
                            trial.update(pid=lease["pid"], process_identity=lease["process_identity"],
                                attempt=max(trial["attempt"], lease["attempt"]), attempt_started_at=lease["started_at"])
                            trial.pop("lease_deadline", None)
                            self.store.put("trial", trial, "trial.adopted")
                    if not alive(trial) and time.time() < trial.get("lease_deadline", 0) and not (directory / "result.json").exists():
                        continue
                progress = read_json(directory / "progress.json")
                changed = False
                if progress and progress != trial.get("progress"):
                    trial["progress"] = progress
                    changed = True
                result = read_json(directory / "result.json")
                process = self.processes.get(trial["id"])
                if process:
                    process.poll()
                is_alive = alive(trial)
                host_receipt = None
                if selected_isolation and not is_alive:
                    from optimization_framework.execution import supervision
                    try:
                        host_receipt = supervision.reconcile(self, trial, selected_isolation)
                    except (ValueError, BlockingIOError) as exc:
                        self.memory.issue(trial["campaign_id"], "execution_host_invalid", str(exc), affected=trial["id"])
                        continue
                    stopped = host_receipt.get("stopped_by")
                    if stopped in {"budget", "deadline"}:
                        trial.update(stopped_by=stopped)
                    if stopped or host_receipt.get("process_exit") not in {0, None}:
                        status = ("budget_exhausted" if stopped in {"budget", "deadline"} else "interrupted"
                                  if stopped == "interrupted_host" else "paused" if stopped == "control_pause" else "stopped"
                                  if stopped == "control_stop" else "failed")
                        result = {**(result or progress or {}), "status": status,
                            "reason": host_receipt.get("error") or stopped or "isolated_worker_failed",
                            "elapsed_seconds": trial.get("execution_seconds", 0),
                            "execution_host_receipt_id": host_receipt["id"],
                            "unknown_worker_cost": bool(trial.get("unknown_execution_seconds")),
                            "scientific_complete": bool(result and result.get("scientific_complete"))}
                        if status in {"failed", "interrupted"}:
                            self.memory.issue(trial["campaign_id"], "isolated_execution_failed",
                                result["reason"], affected=trial["id"], evidence=[host_receipt["id"]])
                enforcement = self._deadline_enforcement(trial, directory) if not is_alive and not selected_isolation else None
                if enforcement:
                    trial.update(stopped_by="deadline", reason="Study execution deadline enforced after the frozen shutdown grace",
                        deadline_enforcement_id=enforcement["id"], execution_seconds=max(trial.get("execution_seconds", 0), enforcement["elapsed_seconds"]),
                        execution_seconds_basis="observed_to_termination_request")
                    result = {**(result or progress or {}), "attempt_id": enforcement["attempt_id"], "status": "budget_exhausted",
                        "reason": "study_deadline_reached", "allocation_stop": "study_deadline_reached",
                        "scientific_complete": bool(result and result.get("scientific_complete")),
                        "elapsed_seconds": trial["execution_seconds"], "unknown_worker_cost": True,
                        "uncertain_final_call": not bool(result), "worker_terminal_record": bool(result),
                        "process_exit": process.returncode if process else None, "resume_supported": False,
                        "deadline_enforcement_id": enforcement["id"], "absolute_deadline": trial["absolute_deadline"],
                        "deadline_overshoot_seconds": max(0, enforcement["enforced_at"] - trial["absolute_deadline"])}
                if is_alive:
                    trial["execution_seconds"] = max(trial.get("execution_seconds", 0),
                        trial.get("prior_execution_seconds", 0) + time.time() - trial.get("attempt_started_at", time.time()))
                    if selected_isolation:
                        trial["execution_seconds_upper_bound"] = max(trial.get("execution_seconds_upper_bound", 0),
                            trial["prior_budget_execution_seconds"] + time.time() - trial["attempt_started_at"])
                    if trial.get("absolute_deadline") and time.time() >= trial["absolute_deadline"] and trial["status"] != "stopping":
                        trial.update(status="stopping", stopped_by="deadline", reason="Study execution deadline reached", stop_requested_at=time.time())
                        self._write_control(trial)
                        changed = True
                    elif max(trial["execution_seconds"], trial.get("execution_seconds_upper_bound", 0)) >= trial["wall_seconds"] and trial["status"] == "running":
                        trial.update(status="stopping", stopped_by="budget", reason="Wall-time budget reached", stop_requested_at=time.time())
                        self._write_control(trial)
                        changed = True
                    if trial["status"] == "stopping" and time.time() - trial.get("stop_requested_at", time.time()) >= trial.get("stop_grace_seconds", self.stop_grace_seconds) + (2 if selected_isolation else 0):
                        self._terminate(trial)
                if result and not is_alive:
                    trial["result"] = result
                    if not trial.get("reason"):
                        trial["reason"] = result.get("reason")
                    if trial.get("stopped_by") in DELIBERATE_STOPS:
                        trial["status"] = "stopped"
                    elif trial.get("stopped_by") in {"budget", "deadline"}:
                        trial["status"] = "budget_exhausted"
                    else:
                        trial["status"] = result.get("status", "completed")
                    if trial["status"] not in {"completed", "paused", "stopped", "failed", "budget_exhausted", "interrupted"}:
                        trial["status"] = "failed"
                        trial["reason"] = "Worker returned an invalid terminal status"
                    trial["finished_at"] = now()
                    if not selected_isolation:
                        trial["execution_seconds"] = max(trial.get("execution_seconds", 0), result.get("elapsed_seconds", 0))
                    if trial["algorithm"] == "validate" and trial.get("parent_trial_id"):
                        parent = self.store.get(trial["parent_trial_id"], "trial")
                        parent["validation"] = {"trial_id": trial["id"], "status": trial["status"], **result}
                        self.store.put("trial", parent, "trial.validated")
                    self.store.event(trial["campaign_id"], "trial.finished", {"trial_id": trial["id"], "status": trial["status"]})
                    trial["research_pending"] = True
                    changed = True
                elif not is_alive:
                    trial["status"] = "stopped" if trial.get("stopped_by") in DELIBERATE_STOPS else "budget_exhausted" if trial.get("stopped_by") in {"budget", "deadline"} else "interrupted"
                    trial["reason"] = trial.get("reason") or "Worker exited without a terminal record; inspect logs or resume its checkpoint"
                    trial["finished_at"] = now()
                    self.store.event(trial["campaign_id"], "trial.interrupted", {"trial_id": trial["id"]})
                    changed = True
                if trial.get("absolute_deadline") and trial["status"] not in LIVE:
                    # Reconciliation may run hours after a worker exits. Its
                    # observation time is not the numerical job's overshoot.
                    trial["deadline_overshoot_seconds"] = (result or {}).get("deadline_overshoot_seconds")
                    trial["deadline_overshoot_basis"] = "termination_request" if enforcement else "worker_report" if result else "unknown"
                    if trial.get("stopped_by") == "deadline":
                        trial["allocation_stop"] = "study_deadline_reached"
                trial["updated_at"] = now()
                self.store.put("trial", trial, "trial.progress" if changed else None)

    def _loop(self):
        while not self.shutdown_event.wait(0.4):
            try:
                self.reconcile()
                with self.lock:
                    trials = self.store.list_trials_in_status(LIVE | {"queued"})
                    free = self.max_workers - sum(t["status"] in LIVE for t in trials) - sum(j["status"] in {"starting", "running"} for j in self.store.list("fixed_mask_job"))
                    queued = sorted((t for t in trials if t["status"] == "queued" and self.dependencies_ready(t)), key=lambda t: (-t["priority"], t["created_at"]))
                    for trial in queued:
                        if free <= 0:
                            break
                        if trial.get("race_id") and not self.racing.admission(trial, [row for row in trials if row["status"] in LIVE]):
                            continue
                        if trial.get("execution_grant_id"):
                            grant = self.store.get(trial["execution_grant_id"], "execution_grant")
                            occupied = sum(t["status"] in LIVE and t.get("execution_grant_id") == grant["id"] for t in self.store.list_trials_in_status(LIVE))
                            if occupied >= grant["max_workers"]:
                                continue
                        try:
                            self._start_trial(trial)
                            # Include this launch in resource admission for the next
                            # queued member of the same scheduler pass.
                            trial["status"] = "running"
                            free -= 1
                        except Exception as exc:
                            trial.update(status="failed", reason=str(exc), finished_at=now())
                            self.store.put("trial", trial, "trial.failed")
                if self.on_trial_finished:
                    self.on_trial_finished()
            except Exception as exc:
                self.store.event(None, "service.error", {"message": str(exc)})

    def metrics(self, trial_id, *, fields=None, limit=None):
        self.store.get(trial_id, "trial")
        path = self.job_dir(trial_id) / "metrics.jsonl"
        if limit is not None:
            if type(limit) is not int or not 1 <= limit <= 1000:
                raise ValueError("Recent metrics limit must be an integer from 1 through 1000")
            from optimization_framework.execution.metrics import recent_metrics
            rows = recent_metrics(path, limit)
            return [{field: row[field] for field in fields if field in row} for row in rows] if fields is not None else rows
        if fields is not None:
            return self._metric_projections.read(path, fields)
        if not path.exists():
            return []
        rows = []
        for line in path.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue  # An active writer can leave one incomplete final line.
        return rows
