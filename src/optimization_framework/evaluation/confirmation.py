"""Seed replication, unseen instances and policy transfer have different rules."""
from optimization_framework.contracts.base import content_hash
from optimization_framework.contracts.confirmation import ConfirmationProtocol
from optimization_framework.contracts.experiments import RecoveryPolicy
from optimization_framework.contracts.problems import ProblemInstance
from optimization_framework.execution.source import scientific_hash, runtime_identity
from optimization_framework.storage.sqlite import now


def resolved_method_definition(trial):
    """Pin behavior and execution runtime, independently of labels and seed."""
    if not trial.get("scientific_source_hash") or not trial.get("scientific_environment"):
        raise ValueError("This historical prototype needs verified source/runtime identities before confirmation")
    return {**({"diagnostics": trial["diagnostics"]} if trial.get("diagnostics") else {}),
        **({"numerical_threads": trial["numerical_threads"]} if trial.get("numerical_threads", 1) != 1 else {}),
        **({"method_contract": trial["method_contract"], "recovery": RecoveryPolicy(**trial.get("recovery", {})).model_dump(mode="json")} if trial.get("method_contract") in {2, 3} else {}),
        "algorithm": trial["algorithm"], "algorithm_config": trial.get("algorithm_config", {}),
        "training": {key: value for key, value in trial.get("training", {}).items() if key != "seed"},
        "schedule_steps": trial["schedule_steps"], "max_steps": trial["max_steps"], "wall_seconds": trial["wall_seconds"],
        "completion": trial.get("completion", {"unit": "evaluation_requests", "count": trial["max_steps"]}),
        "implementation_version_id": trial.get("implementation_version_id"),
        "implementation_digest": trial.get("implementation_artifact_digest"),
        "scientific_source_hash": trial["scientific_source_hash"], "runtime": trial["scientific_environment"],
        "initial_assets": trial.get("initial_assets", []),
        "contribution_asset_ids": trial.get("contribution_asset_ids", []),
        "asset_digests": trial.get("experiment_spec", {}).get("asset_digests") or
            {item["id"]: item["content_hash"] for item in trial.get("declared_assets", [])}}


def method_definition(trial):
    if trial.get("method_contract") == 3 and trial.get("logical_method"):
        method = trial["logical_method"]
        if trial.get("experiment_spec", {}).get("schedule", {}).get("logical_method") != method:
            raise ValueError("Logical method differs from the frozen resolved cell")
        return method
    return resolved_method_definition(trial)


class ConfirmationService:
    def __init__(self, store):
        self.store = store
        self._report_cursors = {}

    def schedule(self, workspace, protocol_id, *, reuse_decision_ids=(), authority="researcher"):
        """Expand a frozen roster into the workspace's ordinary scheduler jobs.

        Allocation is atomic; retrying the roster only fills missing cells.
        It never replaces a failed cell or changes its source, seed, or budget.
        """
        from optimization_framework.contracts.requests import TrialInput
        from .confirmation_allocations import source_matches
        with workspace.lock, self.store.transaction():
            protocol = self.store.get(protocol_id, "confirmation_protocol")
            if protocol.get("contract_version") == 2:
                if any(item["protocol_id"] == protocol_id for item in self.store.list("confirmation_release", protocol["campaign_id"])):
                    raise ValueError("This confirmation protocol is closed")
                design = self.store.get(protocol["design_id"], "confirmation_design")
                execution = self.store.get(design["execution_id"], "study_execution")
                if execution["status"] == "frozen":
                    raise ValueError("Activate the frozen study grant before scheduling its cells")
                previous = {row["id"] for row in self.store.list("trial", protocol["campaign_id"])}
                workspace.study_executions._reconcile(design["execution_id"])
                trials = [row["id"] for row in self.store.list("trial", protocol["campaign_id"]) if row.get("study_execution_id") == design["execution_id"]]
                return {"protocol_id": protocol_id, "created_trial_ids": [key for key in trials if key not in previous],
                    "existing_trial_ids": [key for key in trials if key in previous]}
            campaign = self.store.get(protocol["campaign_id"], "campaign")
            if any(item["protocol_id"] == protocol_id for item in self.store.list("confirmation_release", campaign["id"])):
                raise ValueError("This confirmation protocol is closed; a new study is required for further work")
            if not protocol.get("prototypes"):
                raise ValueError("This historical protocol has no pinned source prototypes; define a linked study")
            if authority == "manager" and (campaign["autonomy"] != "delegated" or any(
                    method["wall_seconds"] > campaign["delegated_trial_seconds"] for method in protocol["methods"].values())):
                raise ValueError("The frozen roster exceeds the manager's delegated per-experiment allowance")
            study = self.store.get(protocol["study_id"], "study")
            tasks = {ProblemInstance(**task["problem"]).digest(): task for task in
                     (workspace.evaluators.task_view(item) for item in self.store.list("task", campaign["id"]))
                     if task.get("problem_instance_id") in study["instance_ids"] and task.get("problem")}
            decisions = list(reuse_decision_ids) or [item["id"] for item in self.store.list("reuse_decision", campaign["id"])
                if item["study_id"] == protocol["study_id"] and item["decision"] == "reuse" and item["intended_use"] == "optimizer_input"]
            cells = self.assess(protocol_id)["cells"]
            created, existing = [], []
            for cell in cells:
                if cell["trial_id"]:
                    existing.append(cell["trial_id"])
                    continue
                task = tasks.get(cell["instance_digest"])
                if not task:
                    raise ValueError("A frozen confirmation instance is unavailable in this campaign")
                method = protocol["methods"][cell["method_id"]]
                prototype_id = protocol["prototypes"][cell["method_id"]]
                prototype = self.store.get(prototype_id, "trial")
                if not source_matches(self.store, protocol, cell["method_id"], prototype):
                    raise ValueError("The frozen source prototype changed; confirmation cannot substitute its current procedure")
                request = TrialInput(campaign_id=campaign["id"], task_id=task["id"], seed=cell["seed"],
                    numerical_threads=prototype.get("numerical_threads", 1),
                    algorithm=method["algorithm"], implementation_version_id=method.get("implementation_version_id"),
                    algorithm_config=method["algorithm_config"], training=method["training"], initial_assets=method["initial_assets"],
                    reuse_decision_ids=decisions, max_steps=method["max_steps"], schedule_steps=method["schedule_steps"],
                    wall_seconds=method["wall_seconds"], completion=method["completion"], confirmation_protocol_id=protocol_id,
                    diagnostics=method.get("diagnostics", []),
                    recovery=method.get("recovery", prototype.get("recovery", {})),
                    question="Execute one method/instance/seed cell of the frozen confirmation roster")
                trial = workspace.create_trial(request, frozen_prototype_id=prototype_id)
                created.append(trial["id"])
            return {"protocol_id": protocol_id, "created_trial_ids": created, "existing_trial_ids": existing}

    def schedule_checks(self, workspace, protocol_id, *, authority="researcher"):
        from optimization_framework.contracts.requests import RecipeInput
        if self.store.get(protocol_id, "confirmation_protocol").get("contract_version") == 2:
            return self.schedule(workspace, protocol_id, authority=authority)
        with workspace.lock, self.store.transaction():
            protocol = self.store.get(protocol_id, "confirmation_protocol")
            study = self.store.get(protocol["study_id"], "study")
            campaign = self.store.get(protocol["campaign_id"], "campaign")
            policy = study.get("validation_policy", {})
            wall = policy.get("validation_wall_seconds", 120)
            if authority == "manager" and (campaign["autonomy"] != "delegated" or wall > campaign["delegated_trial_seconds"]):
                raise ValueError("Required validation exceeds the manager's delegated per-experiment allowance")
            assessment = self.assess(protocol_id)
            if assessment["release"]:
                raise ValueError("This confirmation protocol is closed; later counterchecks must remain separately identified")
            trials = self.store.list("trial", campaign["id"])
            created, existing = [], []
            for cell in assessment["cells"]:
                if not cell["scientific_complete"]:
                    continue
                for check in cell["required_validation"]:
                    if check["passed"]:
                        continue
                    previous = [trial for trial in trials if trial.get("parent_trial_id") == cell["trial_id"]
                        and set(trial.get("validation_requirement_ids", [])) & set(check["requirement_ids"])]
                    if previous:
                        existing.extend(trial["id"] for trial in previous)
                        continue  # Failed checks require an explicit recheck, never an automatic hidden retry.
                    trial = workspace.run_recipe(cell["trial_id"], RecipeInput(recipe_id=check["recipe_id"],
                        parameters=policy.get("required_recipe_parameters", {}).get(check["recipe_id"], {}),
                        subject_limit=1, wall_seconds=wall), authority=authority)
                    created.append(trial["id"])
            return {"protocol_id": protocol_id, "created_trial_ids": created, "existing_trial_ids": sorted(set(existing))}

    def freeze(self, protocol):
        protocol = protocol if isinstance(protocol, ConfirmationProtocol) else ConfirmationProtocol(**protocol)
        study = self.store.get(protocol.study_id, "study")
        if study["campaign_id"] != protocol.campaign_id or study["scope"] != "confirmation":
            raise ValueError("Confirmation needs a frozen confirmation study in this campaign")
        if study.get("confirmation") != protocol.model_dump(mode="json"):
            raise ValueError("Protocol differs from the study's frozen confirmation design")
        if any(content_hash(method) != identity for identity, method in protocol.methods.items()):
            raise ValueError("Confirmation method identity differs from its resolved behavior")
        if set(study["method_roster"]) != set(protocol.methods):
            raise ValueError("The confirmation method roster changed after study freeze")
        if protocol.kind == "policy_transfer":
            asset = self.store.get(protocol.policy_asset_id, "asset")
            from optimization_framework.assets.catalog import AssetCatalog
            if asset["kind"] != "policy" or AssetCatalog(self.store).availability(asset)["status"] == "unavailable":
                raise ValueError("Policy transfer requires an available policy artifact")
        return self.store.put_immutable("confirmation_protocol", protocol.model_dump(mode="json"), "confirmation.frozen")

    def prepare(self, protocol_id, trial):
        if any(item["protocol_id"] == protocol_id for item in self.store.list("confirmation_release")):
            raise ValueError("This confirmation protocol is closed; a new study is required for further work")
        raw = self.store.get(protocol_id, "confirmation_protocol")
        if raw.get("contract_version") == 2:
            raise ValueError("Conditional confirmation cells must be admitted through their frozen study execution")
        protocol = ConfirmationProtocol(**{key: value for key, value in raw.items() if key != "content_hash"})
        if trial["campaign_id"] != protocol.campaign_id or trial["study_id"] != protocol.study_id:
            raise ValueError("Experiment belongs to another confirmation study")
        if trial["seed"] not in protocol.seeds:
            raise ValueError("Seed is outside the frozen confirmation roster")
        instance = ProblemInstance(**trial["problem"])
        if instance.digest() not in {item.digest() for item in protocol.instances}:
            raise ValueError("Problem instance or fidelity differs from the frozen confirmation roster")
        method = method_definition(trial)
        method_id = content_hash(method)
        if method_id not in protocol.methods:
            raise ValueError("Experiment changes the frozen method, inputs, allocation or schedule")
        trials = self.store.list("trial", trial["campaign_id"])
        identity = instance.scientific_identity
        same = [item for item in trials if item.get("problem", {}).get("scientific_identity") == identity and not item.get("recipe")]
        outside = [item for item in same if item.get("confirmation_protocol_id") != protocol.id]
        within = [item for item in same if item.get("confirmation_protocol_id") == protocol.id
                  and ProblemInstance(**item["problem"]).digest() == instance.digest()]
        if any(item["seed"] == trial["seed"] and item.get("confirmation_method_id") == method_id for item in within):
            raise ValueError("This method/instance/seed cell is already allocated in the confirmation protocol")
        if protocol.kind == "seed_replication":
            if any(item["seed"] == trial["seed"] for item in outside):
                raise ValueError("Seed replication requires a fresh seed on this known instance")
        elif protocol.kind == "unseen_instance":
            if outside:
                raise ValueError("This instance was already evaluated outside the frozen confirmation cohort")
            for decision in self.store.list("reuse_decision", trial["campaign_id"]):
                if decision["decision"] == "decline":
                    continue
                asset = self.store.get(decision["asset_id"], "asset")
                if asset["exposure_status"] == "unknown":
                    raise ValueError("Referenced asset exposure is unknown; this cannot establish an unseen-instance claim")
                if identity in asset["exposed_instance_ids"]:
                    raise ValueError("Prior asset or reference evidence exposed this instance")
            from optimization_framework.evaluation.registry import problems
            adapter = problems.get(instance.definition_id)
            if hasattr(adapter, "check_unseen_instance"):
                adapter.check_unseen_instance(self.store, trial["campaign_id"], instance)
        else:
            if protocol.policy_asset_id not in trial.get("initial_assets", []):
                raise ValueError("Transfer experiment did not declare the protocol's frozen policy input")
            read_only = (trial.get("inference_adapter") or {}).get("adaptation") == "forbidden"
            legacy_read_only = trial["algorithm"] == "frozen_policy" and not trial.get("inference_adapter")
            if protocol.adaptation == "forbidden" and not (read_only or legacy_read_only):
                raise ValueError("This policy-transfer protocol forbids further learning or adaptation")
            if protocol.adaptation == "budgeted" and method != protocol.adaptation_procedure:
                raise ValueError("Adaptation differs from the frozen and costed procedure")
        return {"confirmation_protocol_id": protocol.id, "confirmation_protocol_hash": protocol.digest(),
                "confirmation_kind": protocol.kind, "confirmation_method_id": method_id,
                "scientific_source_hash": method["scientific_source_hash"], "confirmatory": True}

    def assess(self, protocol_id):
        from optimization_framework.evaluation.policy import ValidationService
        from optimization_framework.evaluation.diagnostics import assessment as diagnostic_assessment
        from optimization_framework.evaluation.executables import assessment as executable_assessment
        protocol = self.store.get(protocol_id, "confirmation_protocol")
        if protocol.get("contract_version") == 2:
            from optimization_framework.evaluation.designs import view
            return view(self.store, protocol)
        study = self.store.get(protocol["study_id"], "study")
        required_recipes = study.get("validation_policy", {}).get("required_recipes", [])
        if not isinstance(required_recipes, list) or any(not isinstance(value, str) for value in required_recipes):
            raise ValueError("The study's required_recipes policy must name registered recipe identifiers")
        trials = [trial for trial in self.store.list("trial", protocol["campaign_id"]) if trial.get("confirmation_protocol_id") == protocol_id]
        requirements = self.store.list("validation_requirement", protocol["campaign_id"])
        instance_names = {ProblemInstance(**task["problem"]).digest(): task["name"] for task in self.store.list("task", protocol["campaign_id"])
                          if task.get("problem_instance_id") in study["instance_ids"] and task.get("problem")}
        instance_names.update({ProblemInstance(**binding["problem"]).digest(): self.store.get(binding["task_id"], "task")["name"]
            for binding in self.store.list("evaluator_binding", protocol["campaign_id"])
            if binding["problem_instance_id"] in study["instance_ids"]})
        cells = []
        for method_id in protocol["methods"]:
            for raw in protocol["instances"]:
                instance = ProblemInstance(**raw)
                for seed in protocol["seeds"]:
                    matches = [trial for trial in trials if trial.get("confirmation_method_id") == method_id and trial["seed"] == seed
                               and ProblemInstance(**trial["problem"]).digest() == instance.digest()]
                    if len(matches) > 1:
                        raise ValueError("Duplicate experiments occupy a frozen confirmation cell")
                    trial = matches[0] if matches else None
                    result = (trial.get("result") or trial.get("progress") or {}) if trial else {}
                    complete = bool(trial and trial["status"] == "completed" and result.get("scientific_complete"))
                    cataloged = bool(trial and trial.get("asset_capture_attempt", -1) == trial.get("attempt", 0))
                    diagnostics = diagnostic_assessment(self.store, trial) if trial else {"complete": False, "grants": [], "jobs": []}
                    executable = executable_assessment(self.store, trial) if trial else {"supported": False, "executables": []}
                    checks = []
                    for recipe_id in required_recipes:
                        eligible = []
                        for requirement in requirements:
                            if not trial or requirement["scope"].get("parent_trial_id") != trial["id"] or requirement["recipe_id"] != recipe_id:
                                continue
                            expected_parameters = study.get("validation_policy", {}).get("required_recipe_parameters", {}).get(recipe_id)
                            if expected_parameters is not None and requirement["scope"].get("parameters") != expected_parameters:
                                continue
                            subject = self.store.get(requirement["subject_id"])
                            candidate = subject.get("payload", {}).get("candidate")
                            if requirement["kind"] == "evaluator_correctness" or candidate == result.get("best_candidate", result.get("best_design")):
                                eligible.append(ValidationService(self.store).assess(requirement["id"]))
                        checks.append({"recipe_id": recipe_id, "passed": any(item["measured_pass"] for item in eligible),
                                       "requirement_ids": [item["requirement"]["id"] for item in eligible],
                                       "results": [result for item in eligible for result in item["results"]]})
                    cells.append({"method_id": method_id, "instance_digest": instance.digest(), "seed": seed,
                        "instance_name": instance_names.get(instance.digest(), instance.definition_id),
                        "trial_id": trial["id"] if trial else None, "task_id": trial["task_id"] if trial else None,
                        "status": trial["status"] if trial else "not_allocated", "scientific_complete": complete,
                        "result_digest": content_hash(result) if trial else None,
                        "experiment_spec_hash": trial.get("experiment_spec_hash") if trial else None,
                        "best_objective": result.get("best_objective"), "objective": instance.primary_objective.model_dump(mode="json"),
                        "diagnostics": diagnostics, "required_validation": checks, "executable_evidence": executable,
                        "evidence_cataloged": cataloged,
                        "evidence_complete": complete and cataloged and diagnostics["complete"] and executable["supported"] and all(check["passed"] for check in checks)})
        complete = bool(cells) and all(cell["evidence_complete"] for cell in cells)
        release = next((item for item in self.store.list("confirmation_release", protocol["campaign_id"]) if item["protocol_id"] == protocol_id), None)
        reports = [item for item in self.store.list("confirmation_report", protocol["campaign_id"]) if item["protocol_id"] == protocol_id]
        return {"protocol_id": protocol_id, "campaign_id": protocol["campaign_id"], "study_id": protocol["study_id"],
            "kind": protocol["kind"], "cells": cells, "complete": complete,
            "methods": protocol["methods"], "required_recipes": required_recipes,
            "release": {key: release[key] for key in ("id", "outcome", "authority", "rationale", "created_at")} if release else None,
            "report": reports[-1] if reports else None,
            "status": "complete" if complete else "incomplete", "selection_rule": protocol["selection_rule"],
            "analysis_rule": protocol.get("analysis"), "nomination_id": protocol.get("nomination_id"),
            "interpretation": "The frozen analysis rule determines the conclusion after evidence release." if protocol.get("analysis") else
                "Completion of this declared roster is separate from a claim of superiority; this protocol produces descriptive evidence only."}

    def record_report(self, protocol, assessment, *, authority):
        """Freeze the exact evidence used; later contrary evidence is a new report."""
        study = self.store.get(protocol["study_id"], "study")
        inputs = {"protocol_hash": protocol["content_hash"], "study_hash": study["content_hash"], "cells": assessment["cells"]}
        conclusion = None
        if protocol.get("analysis"):
            from optimization_framework.analysis.studies import confirmation_evidence
            from optimization_framework.analysis.rules import evaluate
            inputs["analysis_evidence"] = assessment.get("analysis_evidence") or confirmation_evidence(self.store, protocol, assessment)
            if protocol.get("contract_version") == 2 and not (inputs["analysis_evidence"].get("nomination") or {}).get("selected_method_ids"):
                conclusion = {"complete": False, "outcome": "inconclusive_execution", "reason": "No eligible profile was nominated before the frozen development cutoff"}
            else:
                conclusion = evaluate(protocol["analysis"], inputs["analysis_evidence"], store=self.store)
            if not assessment["complete"] or not conclusion.get("complete"):
                conclusion = {**conclusion, "complete": False, "outcome": "inconclusive_execution"}
        digest = content_hash(inputs)
        identity = "confirmation_report_" + digest
        try:
            return self.store.get(identity, "confirmation_report")
        except KeyError:
            pass
        previous = [item for item in self.store.list("confirmation_report", protocol["campaign_id"]) if item["protocol_id"] == protocol["id"]]
        report = {"id": identity, "campaign_id": protocol["campaign_id"], "study_id": protocol["study_id"],
            "protocol_id": protocol["id"], "evidence_hash": digest, "evidence": inputs,
            "rule": protocol.get("analysis") or {"id": "descriptive_roster:v1", "selection_rule": protocol["selection_rule"]},
            "claim_level": ("protocol_conclusion" if conclusion["complete"] else "inconclusive") if conclusion else
                "descriptive" if assessment["complete"] else "inconclusive",
            "outcome": conclusion["outcome"] if conclusion else "completed_roster" if assessment["complete"] else "inconclusive",
            "conclusion": conclusion, "interpretation": conclusion.get("interpretation", assessment["interpretation"]) if conclusion else assessment["interpretation"],
            "authority": authority, "created_at": now(),
            **({"qualification_only": True} if assessment.get("qualification_only") else {}),
            "supersedes_report_id": previous[-1]["id"] if previous else None}
        return self.store.put_immutable("confirmation_report", report, "confirmation.reported")

    def reconcile_reports(self, on_error=None):
        for release in self.store.list("confirmation_release"):
            with self.store.connection() as db:
                cursor = db.execute("SELECT COALESCE(MAX(id),0) FROM events WHERE (campaign_id=? AND kind IN "
                    "('trial.evidence_cataloged','validation.measured','validation.waived','validation.waiver_revoked',"
                    "'confirmation.released','asset.published','cost.recorded','budget.amended')) OR kind='executable.evidence_recorded'", (release["campaign_id"],)).fetchone()[0]
            if self._report_cursors.get(release["protocol_id"]) == cursor:
                continue
            try:
                with self.store.transaction():
                    protocol = self.store.get(release["protocol_id"], "confirmation_protocol")
                    assessment = self.assess(protocol["id"])
                    self.record_report(protocol, assessment, authority="evidence_reconciliation")
            except (ValueError, KeyError) as exc:
                if on_error is None:
                    raise
                on_error(release["campaign_id"], "report_requires_attention", str(exc), affected=release["protocol_id"], evidence=[release["id"]])
            self._report_cursors[release["protocol_id"]] = cursor

    def release(self, protocol_id, *, authority="researcher", allow_incomplete=False, rationale="The fixed protocol and required evidence are complete"):
        protocol = self.store.get(protocol_id, "confirmation_protocol")
        identity = "release_" + protocol_id
        with self.store.transaction():
            try:
                return self.store.get(identity, "confirmation_release")
            except KeyError:
                pass
            assessment = self.assess(protocol_id)
            execution = None
            if protocol.get("contract_version") == 2:
                design = self.store.get(protocol["design_id"], "confirmation_design")
                execution = self.store.get(design["execution_id"], "study_execution")
                members = [row for row in self.store.list("trial", protocol["campaign_id"]) if row.get("study_execution_id") == execution["id"]]
                if any(row["status"] in {"queued", "running", "pausing", "paused", "stopping", "interrupted"} for row in members):
                    raise ValueError("Finish or stop every admitted study job before closing its cohort")
                for grant in self.store.list("execution_check_grant", protocol["campaign_id"]):
                    if grant["execution_grant_id"] == execution.get("grant_id") and grant["status"] == "reserved":
                        if not allow_incomplete or authority != "researcher":
                            raise ValueError("Required study checks remain reserved; run them or explicitly close as inconclusive")
                        grant.update(status="cancelled", rationale=rationale, finished_at=now())
                        self.store.put("execution_check_grant", grant, "study.checks_closed")
            if any(cell["status"] in {"queued", "running", "pausing", "paused", "stopping", "interrupted"} for cell in assessment["cells"]):
                raise ValueError("Finish or stop the remaining confirmation work before closing the protocol")
            if not assessment["complete"] and (authority != "researcher" or not allow_incomplete or not rationale.strip()):
                raise ValueError("Incomplete confirmation requires an explicit researcher closure and an inconclusive interpretation")
            trials = [cell["trial_id"] for cell in assessment["cells"] if cell["trial_id"]]
            if execution:
                trials = sorted(set(trials) | {row["id"] for row in members})
            from optimization_framework.evaluation.diagnostics import descendants
            all_trials = self.store.list("trial", protocol["campaign_id"])
            related_ids = descendants(all_trials, trials)
            diagnostics = [trial for trial in all_trials if trial["id"] in related_ids]
            if any(trial["status"] in {"queued", "running", "pausing", "paused", "stopping", "interrupted"} for trial in diagnostics):
                raise ValueError("Finish or stop related validation work before releasing its evidence")
            related = [trial["id"] for trial in diagnostics]
            for grant in self.store.list("diagnostic_grant", protocol["campaign_id"]):
                if grant["parent_trial_id"] in {*trials, *related} and grant["status"] == "reserved":
                    if not allow_incomplete or authority != "researcher":
                        raise ValueError("Declared diagnostics remain pending; execute them or explicitly close as inconclusive")
                    grant.update(status="cancelled", rationale=rationale, finished_at=now())
                    self.store.put("diagnostic_grant", grant, "diagnostic.cancelled")
            report = self.record_report(protocol, assessment, authority=authority)
            record = {"id": identity, "campaign_id": protocol["campaign_id"], "study_id": protocol["study_id"], "protocol_id": protocol_id,
                "protocol_hash": protocol["content_hash"], "assessment": assessment, "report_id": report["id"],
                "outcome": "completed_roster" if assessment["complete"] else "inconclusive", "authority": authority, "rationale": rationale,
                "trial_ids": trials + related, "task_ids": sorted({cell["task_id"] for cell in assessment["cells"] if cell["task_id"]}),
                "created_at": now()}
            release = self.store.put_immutable("confirmation_release", record, "confirmation.released")
            if execution:
                if execution.get("grant_id"):
                    from optimization_framework.execution.resources import ResourceLedger
                    ResourceLedger(self.store).release(execution["grant_id"], rationale=rationale)
                execution.update(status="closed", finished_at=now())
                self.store.put("study_execution", execution, "study.execution_closed")
            return release
