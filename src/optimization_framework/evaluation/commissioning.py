"""Durable missing-evaluator requirements, immutable bindings and readiness."""
from optimization_framework.contracts.base import content_hash
from optimization_framework.contracts.evaluators import EvaluatorManifest
from optimization_framework.implementations.evaluator_validation import current
from optimization_framework.implementations.models import EvaluatorSpec, digest
from optimization_framework.implementations.runtime import verify_runtime, write_package, tree_hashes, bundle_runtime_root, pin_runtime
from optimization_framework.storage.sqlite import atomic_json, now


class EvaluatorService:
    def __init__(self, workspace):
        self.workspace = workspace
        self.store = workspace.store

    def declare(self, task):
        manifest = EvaluatorManifest.model_validate(task["evaluator_manifest"])
        identity = "evaluator_requirement_" + content_hash([task["id"], manifest.version, task["configuration"], task["fidelity"]])
        requirement = {"id": identity, "campaign_id": task["campaign_id"], "task_id": task["id"],
            "manifest": manifest.model_dump(mode="json"), "configuration": task["configuration"],
            "fidelity": task["fidelity"], "created_at": task["created_at"]}
        self.store.put_immutable("evaluator_requirement", requirement, "evaluator.required")
        task.update(evaluator_requirement_id=identity, problem_instance_id=None)
        return task

    def binding(self, task):
        if not task.get("evaluator_requirement_id"):
            return None
        try:
            return self.store.get("binding_" + task["evaluator_requirement_id"], "evaluator_binding")
        except KeyError:
            return None

    def task_view(self, task):
        binding = self.binding(task)
        if not binding:
            return task
        return {**task, "problem": binding["problem"], "problem_instance_id": binding["problem_instance_id"],
                "evaluator_binding_id": binding["id"], "evaluator_version_id": binding["version_id"]}

    @staticmethod
    def describe_task(task):
        if task.get("evaluator_manifest"):
            return EvaluatorManifest.model_validate(task["evaluator_manifest"]).describe(task["problem"]["evaluator_version"])
        from optimization_framework.evaluation.registry import problems
        return problems.get(task["problem"]["definition_id"]).describe()

    @staticmethod
    def registry_for(task):
        from optimization_framework.evaluation.registry import problems
        if task.get("evaluator_manifest"):
            from optimization_framework.evaluation.generated import declared_registry
            return declared_registry(task["problem"], task["evaluator_manifest"])
        return problems

    def require_numerical_evidence(self, task, study_id):
        """An immutable evaluator binding needs a separate decision in each study."""
        binding = self.binding(task)
        if not binding:
            return None
        version = self.store.get(binding["version_id"], "implementation_cache")
        if version["spec"].get("validation_mode", "numerical") != "contract_only":
            return None
        from optimization_framework.contracts.experiments import task_in_study
        from optimization_framework.contracts.validation import ValidationRequirement
        study = self.store.get(study_id, "study")
        if study["campaign_id"] != task["campaign_id"] or not task_in_study(self.task_view(task), study):
            raise ValueError("Evaluator evidence requirement is outside the study's frozen problem scope")
        identity = "evaluator_numerical_" + content_hash([binding["id"], study_id])
        try:
            requirement = self.store.get(identity, "validation_requirement")
        except KeyError:
            requirement = self.workspace.validations.require(ValidationRequirement(id=identity, campaign_id=task["campaign_id"],
                study_id=study_id, subject_id=binding["id"], subject_digest=self.workspace.validations.subject_digest(binding["id"]),
                kind="evaluator_correctness", recipe_id="evaluator_numerical:v1",
                scope={"task_id": task["id"], "version_id": version["id"], "numerical_status": "unverified",
                    "contract_evidence_ids": [binding["validation_evidence_id"]]},
                evidence_requirements=["Independent numerical correctness evidence for this evaluator and intended problem"],
                authority=binding["authority"], created_at=now()))
        self._record_report(binding, requirement, version)
        return requirement

    def _record_report(self, binding, requirement, version):
        report = version["validation_report"]
        if report.get("kind") != "evaluator_correctness" and report.get("passed"):
            return  # Contract success does not answer the numerical requirement.
        if report.get("passed") and not current(version):
            return  # Stale or blocked evidence cannot supply a new measured pass.
        from optimization_framework.contracts.validation import ValidationResult
        evidence = {"id": "evaluator_evidence_" + content_hash([version["id"], report["id"]]),
            "version_id": version["id"], "artifact_digest": version["artifact_digest"], "report": report}
        self.store.put_immutable("evaluator_evidence", evidence, "evaluator.evidence_recorded")
        identity = "evaluator_result_" + content_hash([requirement["id"], report["id"]])
        try:
            self.store.get(identity, "validation_result")
        except KeyError:
            self.workspace.validations.record_result(ValidationResult(id=identity, campaign_id=requirement["campaign_id"],
                requirement_id=requirement["id"], subject_digest=requirement["subject_digest"], recipe_id=requirement["recipe_id"],
                verdict="passed" if report["passed"] else "failed", evidence_ids=[evidence["id"]],
                measurements={"version_id": version["id"], "validation_report_id": report["id"], "check_kind": report["kind"],
                    "known_specification_failure": not report["passed"], "scope": report["scope"]},
                rationale=report.get("error") or report["limitations"], producer="trusted_service",
                authority="implementation_service", created_at=now()))

    def sync_version_evidence(self, version):
        with self.workspace.lock, self.store.transaction():
            for binding in self.store.list("evaluator_binding"):
                if binding["version_id"] == version["id"]:
                    self.require_for_studies(self.store.get(binding["task_id"], "task"))

    def require_for_studies(self, task):
        from optimization_framework.contracts.experiments import task_in_study
        for study in self.store.list("study", task["campaign_id"]):
            if task_in_study(self.task_view(task), study):
                self.require_numerical_evidence(task, study["id"])

    def readiness(self, task, *, refresh=False, study_id=None, allow_waived=True, wait=True):
        if not task.get("evaluator_requirement_id"):
            # An installed evaluator is runnable once it was seen to start here; without wait a first check runs meanwhile.
            from optimization_framework.evaluation.preflight import readiness
            return readiness(task, wait=wait, devices=self.workspace.devices.reachable(task))
        binding = self.binding(task)
        if not binding:
            grants = [grant for grant in self.store.list("implementation_grant", task["campaign_id"]) if grant.get("task_id") == task["id"]]
            grant = grants[-1] if grants else None
            return {"state": (grant or {}).get("status", "missing"), "runnable": False,
                "reason": (grant or {}).get("error") or "This problem requires a validated evaluator. Commission one or attach a compatible library version.",
                "requirement_id": task["evaluator_requirement_id"], "grant_id": (grant or {}).get("id")}
        try:
            version = (self.workspace.implementations.client.version(binding["version_id"]) if refresh else
                       self.store.get(binding["version_id"], "implementation_cache"))
            if refresh:
                self.workspace.implementations.cache_version(version)
            if version.get("kind") != "evaluator" or version["status"] not in {"validated", "contract_validated"}:
                raise ValueError(version.get("revocation_reason") or version.get("validation_report", {}).get("error") or "Evaluator is not validated or has been revoked")
            if version["artifact_digest"] != binding["artifact_digest"] or not current(version, numerical=False):
                raise ValueError("Evaluator identity or applicable correctness evidence changed; revalidate before launching")
        except (ValueError, KeyError) as exc:
            return {"state": "unavailable", "runnable": False, "reason": str(exc), "version_id": binding["version_id"]}
        if not current(version):
            campaign = self.store.get(task["campaign_id"], "campaign")
            study = self.store.get(study_id or campaign["active_study_id"], "study")
            identity = "evaluator_numerical_" + content_hash([binding["id"], study["id"]])
            pending = {"state": "numerical_evidence_required", "runnable": False, "version_id": version["id"],
                "mandatory_contract_validated": True,
                "requirement_id": identity, "reason": "Evaluator contract checks passed, but independent numerical evidence is missing. Record an eligible exploratory waiver or commission numerical validation."}
            if study["scope"] != "exploratory" or not allow_waived:
                return {**pending, "reason": "This evaluator has no independent numerical validation and cannot support confirmation. Use a numerically validated version."}
            try:
                assessment = self.workspace.validations.assess(identity, current_study=study["id"])
            except KeyError:
                return pending
            pending["waiver_allowed"] = assessment["waiver_allowed"]
            if assessment["status"] == "waived":
                eligibility = {"basis": "waiver", "binding_id": binding["id"], "study_id": study["id"],
                    "requirement_id": identity, "waiver_id": assessment["active_waiver_ids"][-1]}
                return {**pending, "state": "ready_with_waiver", "runnable": True, "eligibility": eligibility,
                    "reason": "Exploratory use is authorized by a scoped waiver. Numerical correctness remains unverified."}
            return pending
        eligibility = {}
        if version["spec"].get("validation_mode") == "contract_only":
            campaign = self.store.get(task["campaign_id"], "campaign")
            eligibility = {"basis": "numerical_validation", "binding_id": binding["id"],
                "study_id": study_id or campaign["active_study_id"],
                "validation_report_id": version["validation_report"]["id"],
                "validation_report_digest": content_hash(version["validation_report"])}
        return {"state": "ready", "runnable": True, "reason": "The exact evaluator version has independent correctness evidence.",
                "mandatory_contract_validated": True,
                "version_id": version["id"], "validation_report_id": version["validation_report"]["id"],
                **({"eligibility": eligibility} if eligibility else {})}

    def attach(self, task_id, version_id, *, rationale, authority="researcher", expected_context=None, _bundle=None):
        bundle = _bundle if _bundle is not None else self.workspace.implementations.client.artifact(version_id)
        version, artifact = bundle["version"], bundle["artifact"]
        if version.get("kind") != "evaluator" or version["status"] not in {"validated", "contract_validated"} or not current(version, numerical=False):
            raise ValueError("Select an evaluator version with current mandatory contract evidence")
        if version["id"] != version_id or digest(artifact) != version["artifact_digest"]:
            raise ValueError("Evaluator service returned an inconsistent artifact")
        spec = EvaluatorSpec.model_validate(artifact["spec"])
        with self.workspace.lock, self.store.transaction():
            task = self.store.get(task_id, "task")
            self.workspace.check_manager_context(task["campaign_id"], expected_context)
            if task.get("archived") or not task.get("evaluator_requirement_id"):
                raise ValueError("Select a current task with a declared evaluator requirement")
            requirement = self.store.get(task["evaluator_requirement_id"], "evaluator_requirement")
            if requirement["manifest"] != spec.manifest.model_dump(mode="json"):
                raise ValueError("Evaluator manifest differs from the frozen problem requirement")
            previous = self.binding(task)
            if previous:
                if previous["version_id"] != version_id or previous["artifact_digest"] != version["artifact_digest"]:
                    raise ValueError("The evaluator binding is frozen; use a new task and linked study for another version")
                self.require_for_studies(task)
                return previous
            instance = spec.manifest.resolve(version_id, requirement["configuration"], requirement["fidelity"])
            instance_id = "instance_" + instance.digest()
            self.workspace.implementations.cache_version(version)
            evidence = {"id": "evaluator_evidence_" + content_hash([version_id, version["validation_report"]["id"]]),
                "version_id": version_id, "artifact_digest": version["artifact_digest"], "report": version["validation_report"]}
            self.store.put_immutable("evaluator_evidence", evidence, "evaluator.evidence_recorded")
            self.store.put_immutable("problem_instance", {"id": instance_id, **instance.model_dump(mode="json")})
            binding = {"id": "binding_" + requirement["id"], "campaign_id": task["campaign_id"], "task_id": task_id,
                "requirement_id": requirement["id"], "problem_instance_id": instance_id, "problem": instance.model_dump(mode="json"),
                "version_id": version_id, "artifact_digest": version["artifact_digest"],
                "validation_report_id": version["validation_report"]["id"], "authority": authority,
                "validation_evidence_id": evidence["id"],
                "rationale": rationale, "created_at": now()}
            self.store.put_immutable("evaluator_binding", binding, "evaluator.attached")
            self.require_for_studies(task)
            self.workspace.implementations._exposure(task["campaign_id"], version)
            return self.store.get(binding["id"], "evaluator_binding")

    def prepare(self, task, *, study_id=None):
        if not task.get("evaluator_requirement_id"):
            readiness = self.readiness(task)
            if not readiness["runnable"]:
                raise ValueError(readiness["reason"])
            return None
        readiness = self.readiness(task, refresh=True, study_id=study_id)
        if not readiness["runnable"]:
            raise ValueError(readiness["reason"])
        binding = self.binding(task)
        bundle = self.workspace.implementations.client.artifact(binding["version_id"])
        version, artifact = bundle["version"], bundle["artifact"]
        if (version["artifact_digest"] != binding["artifact_digest"] or digest(artifact) != binding["artifact_digest"]
                or version.get("kind") != "evaluator" or version["status"] not in {"validated", "contract_validated"} or not current(version, numerical=False)):
            raise ValueError("Pinned evaluator is unavailable or changed")
        eligibility = readiness.get("eligibility", {})
        if eligibility.get("basis") == "numerical_validation" and eligibility["validation_report_id"] != version["validation_report"]["id"]:
            raise ValueError("Evaluator evidence changed during preparation; refresh and freeze the intended report")
        verify_runtime(bundle_runtime_root(bundle), artifact["runtime"])
        asset = self.workspace.implementations.cost_asset(bundle)
        return {**bundle, "cost_asset_ids": [asset["id"]], "binding": binding,
                **({"eligibility": readiness["eligibility"]} if readiness.get("eligibility") else {})}

    @staticmethod
    def pin(directory, bundle):
        root = directory / "evaluator"
        write_package(root / "package", bundle["artifact"]["package"])
        version = bundle["version"]
        if tree_hashes(root / "package") != version["package_hashes"]:
            raise ValueError("Evaluator package differs from its validated source")
        atomic_json(root / "bundle.json", bundle)
        pin_runtime(root, bundle)
        return {"evaluator_version_id": version["id"], "evaluator_artifact_digest": version["artifact_digest"],
                "evaluator_runtime_digest": version["runtime_digest"], "evaluator_binding_id": bundle["binding"]["id"],
                "evaluator_validation_report_id": version["validation_report"]["id"],
                "evaluator_cost_asset_ids": bundle.get("cost_asset_ids", []),
                **({"evaluator_eligibility": bundle["eligibility"]} if bundle.get("eligibility") else {})}

    def check_eligibility(self, trial):
        task = self.store.get(trial["task_id"], "task")
        readiness = self.readiness(task, study_id=trial["study_id"])
        if not readiness["runnable"]:
            raise ValueError(readiness["reason"])
        pinned = trial.get("evaluator_eligibility")
        if pinned:
            binding = self.binding(task)
            if pinned.get("binding_id") != binding["id"] or pinned.get("study_id") != trial["study_id"]:
                raise ValueError("The frozen evaluator evidence refers to another study or binding")
            if pinned.get("basis") == "waiver":
                assessment = self.workspace.validations.assess(pinned["requirement_id"], current_study=trial["study_id"])
                if pinned.get("waiver_id") in assessment["active_waiver_ids"]:
                    return
            elif pinned.get("basis") == "numerical_validation":
                from optimization_framework.implementations.revalidation import reports
                version = self.store.get(binding["version_id"], "implementation_cache")
                if any(report["id"] == pinned["validation_report_id"] and report.get("passed")
                       and content_hash(report) == pinned["validation_report_digest"] for report in reports(version)):
                    return
            raise ValueError("The frozen evaluator authorization changed; create a new experiment for a different evidence basis")
        if readiness.get("eligibility"):
            raise ValueError("The frozen evaluator authorization changed; create a new experiment for a different waiver")

    def check_launch(self, trial):
        if not trial.get("evaluator_version_id"):
            return
        bundle = self.workspace.implementations.client.artifact(trial["evaluator_version_id"])
        version = bundle["version"]
        self.workspace.implementations.cache_version(version)
        if version["artifact_digest"] != trial["evaluator_artifact_digest"] or version["status"] not in {"validated", "contract_validated"} or not current(version, numerical=False):
            raise ValueError("Evaluator changed or was revoked before launch")
        self.check_eligibility(trial)
        pin_runtime(self.workspace.job_dir(trial["id"]) / "evaluator", bundle)
        self.workspace.implementations.runtime_contribution(trial, bundle)
