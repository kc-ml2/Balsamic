"""Durable research tools; numerical mutations retain campaign-manager authority."""
from __future__ import annotations

import threading
import time
from typing import Literal

from pydantic import Field, model_validator

from optimization_framework.contracts.base import Contract, content_hash
from optimization_framework.research.evidence import EvidenceError
from optimization_framework.research.literature import LiteratureReader
from optimization_framework.storage.sqlite import now


class Search(Contract):
    query: str = Field(min_length=1, max_length=1000)
    provider: Literal["arxiv", "crossref"] = "arxiv"
    limit: int = Field(default=5, ge=1, le=10)


class Ingest(Contract):
    identifier: str = Field(min_length=1, max_length=2000, description="A DOI, arXiv identifier, or public primary-source HTTPS URL. Not a stored source ID. For a source returned by search, use source.read directly.")


class Read(Contract):
    source_id: str | None = None
    capture_id: str | None = None
    query: str = Field(default="", max_length=1000)
    offset: int = Field(default=0, ge=0, le=10000)
    limit: int = Field(default=8, ge=1, le=12)

    @model_validator(mode="after")
    def one_source(self):
        if bool(self.source_id) == bool(self.capture_id):
            raise ValueError("Select one source_id or capture_id")
        return self


class Evidence(Contract):
    record_id: str = Field(min_length=1, max_length=300)
    pointer: str = Field(default="", max_length=8192,
        description="RFC 6901 JSON pointer into the record (or experiment/assessment reply). Empty selects the whole value; indexes provide exact child pointers.")
    offset: int = Field(default=0, ge=0,
        description="Entry offset for array/object pages; character offset for text pages.")
    limit: int = Field(default=50, ge=1, le=100,
        description="Maximum entries in a projected array/object page. Text pages use max_bytes. Small complete values remain unchanged.")
    max_bytes: int = Field(default=24 * 1024, ge=1024, le=24 * 1024,
        description="Maximum serialized UTF-8 result bytes including paging metadata. Choose a smaller page (for example 4096) when the current context has little room; this also bounds text pages.")


class Implementations(Contract):
    version_id: str | None = Field(default=None, max_length=300)


class PrepareAssessment(Contract):
    source_artifact_id: str = Field(min_length=1, max_length=300)


class AssessmentReference(Contract):
    assessment_id: str = Field(min_length=1, max_length=300)


class SupersedeTask(Contract):
    task_id: str = Field(min_length=1, max_length=300, description="Exact ID of an obsolete assignment that has never sent a model call or run tools.")
    replacement_task_id: str = Field(min_length=1, max_length=300, description="Exact ID of the replacement assignment in this session, preferably its completed narrowed replacement.")
    reason: str = Field(min_length=1, max_length=5000)


ARGUMENTS = {"source.search": Search, "source.ingest": Ingest, "source.read": Read,
             "evidence.read": Evidence, "context.read": Evidence, "experiment.inspect": Evidence, "assessment.inspect": Evidence, "implementation.inspect": Implementations,
             "assessment.prepare": PrepareAssessment, "assessment.launch": AssessmentReference, "assessment.wait": AssessmentReference,
             "task.supersede": SupersedeTask}


def schemas():
    return {key: model.model_json_schema() for key, model in ARGUMENTS.items()}


class DiscoveryTools:
    def __init__(self, controller):
        self.controller = controller
        self.workspace, self.store = controller.workspace, controller.store
        self.reader = LiteratureReader(self.workspace)

    def prepare(self, task, step, calls):
        from .allowance import ToolAllowanceReached
        session = self.store.get(task["session_id"], "discovery_session")
        existing = [row for row in self.store.list("discovery_tool", task["campaign_id"]) if row["task_id"] == task["id"]]
        identities = ["discovery_tool_" + content_hash([step["id"], call.key])[:32] for call in calls]
        if set(identities).issubset({row["id"] for row in existing}):
            return identities
        phase = None
        if task.get("attempt_id"):
            attempt = self.store.get(task["attempt_id"], "discovery_attempt")
            phase = attempt.get("context_snapshot", {}).get("discovery", {}).get("allocation", {}).get("phase")
        if task.get("wrap_up_reason") or phase == "wrap_up" or step.get("usage", {}).get("calls", 0) >= session["policy"]["max_calls_per_task"]:
            raise ToolAllowanceReached("The task has reached wrap-up; requested tools were deferred without execution. Save findings and remaining gaps for the manager.")
        if len({row["id"] for row in existing} | set(identities)) > session["policy"].get("max_tools_per_task", 32):
            raise ToolAllowanceReached("This batch exceeds the remaining task tool allowance. The whole batch is deferred without execution; save findings and the smallest useful continuation.")
        for identity, call in zip(identities, calls):
            try:
                self.store.get(identity, "discovery_tool")
                continue
            except KeyError:
                pass
            # Invalid arguments get a receipt, allowing the model a bounded
            # correction step. They do not trigger any external operation.
            request = {"id": identity, "campaign_id": task["campaign_id"], "session_id": task["session_id"],
                "task_id": task["id"], "step_id": step["id"], "call": call.model_dump(mode="json"),
                "status": "pending", "created_at": now(), "guidance_revision": task["guidance_revision"],
                "charter_version": session["charter_version"]}
            self.store.put("discovery_tool", request, "discovery.tool_queued")
        return identities

    def _receipt(self, request, *, result=None, error=None, elapsed=0, uncertain=False, deferred=False):
        identity = "receipt_" + request["id"]
        try:
            return self.store.get(identity, "discovery_tool_receipt")
        except KeyError:
            pass
        receipt = self.store.put_immutable("discovery_tool_receipt", {"id": identity,
            "campaign_id": request["campaign_id"], "session_id": request["session_id"], "task_id": request["task_id"],
            "request_id": request["id"], "tool": request["call"]["tool"], "created_at": now(),
            "result": result, "error": error, "status": "deferred" if deferred else "failed" if error else "completed",
            "uncertain": uncertain, "elapsed_seconds": elapsed}, "discovery.tool_completed")
        request.update(status=receipt["status"], receipt_id=identity, finished_at=receipt["created_at"])
        self.store.put("discovery_tool", request)
        self.workspace.agent_log.record(request["campaign_id"], "tool.deferred" if deferred else "tool.failed" if error else "tool.completed",
            discovery_session_id=request["session_id"], agent_id=request["task_id"], task_id=request["task_id"],
            tool_call_id=request["id"], result_id=identity, event_key=identity,
            summary=error or request["call"]["tool"], payload=receipt)
        return receipt

    def tick(self, session):
        threads = self.workspace.source_threads
        for identity, thread in list(threads.items()):
            if not thread.is_alive():
                threads.pop(identity, None)
        for request in self.store.list("discovery_tool", session["campaign_id"]):
            if request["session_id"] != session["id"] or request["status"] != "pending":
                continue
            if len(threads) >= 2 or self.workspace.shutdown_event.is_set():
                return
            with self.workspace.lock, self.store.transaction():
                current = self.store.get(session["id"], "discovery_session")
                if current["status"] not in {"running", "waiting_for_provider"}:
                    return
                task = self.store.get(request["task_id"], "discovery_task")
                if task["status"] != "waiting" or task.get("wait_reason") != "tools":
                    continue
                if (request["guidance_revision"] != self.workspace.memory.state(session["campaign_id"])["guidance_revision"] or
                        request["charter_version"] != self.store.get(session["campaign_id"], "campaign")["version"]):
                    self._receipt(request, error="Guidance changed before tool dispatch; the request was not sent")
                    continue
                try:
                    ARGUMENTS[request["call"]["tool"]].model_validate(request["call"]["arguments"])
                except ValueError:
                    self._receipt(request, error="Invalid tool arguments; use the declared tool schema")
                    continue
                if request["call"]["tool"].startswith("source."):
                    spent = sum(bool(row.get("dispatched_at")) for row in self.store.list("discovery_tool", session["campaign_id"])
                                if row["session_id"] == session["id"] and row["call"]["tool"].startswith("source."))
                    if spent >= session["policy"].get("source_request_limit", 64):
                        self._receipt(request, deferred=True, result={"executed": False,
                            "reason": "Session source-request allocation exhausted; use saved evidence and retain the literature coverage gap."})
                        continue
                request.update(status="running", dispatched_at=now(), attempt_id="attempt_" + request["id"])
                self.store.put("discovery_tool", request, "discovery.tool_started")
                self.workspace.agent_log.record(request["campaign_id"], "tool.started", discovery_session_id=session["id"],
                    agent_id=task["id"], task_id=task["id"], role=task["brief"]["role"], tool_call_id=request["id"],
                    event_key="start:" + request["id"], summary=request["call"]["tool"], payload=request["call"])
            thread = threading.Thread(target=self._run, args=(request,), daemon=True, name="discovery-tool-" + request["id"])
            threads[request["id"]] = thread
            thread.start()

    def _run(self, request):
        started = time.monotonic()
        try:
            result = self.execute(request)
            error = None
        except EvidenceError as exc:
            result, error = None, str(exc)
        except KeyError:
            result, error = None, "The requested source/evidence is unavailable; no support is claimed. Choose another source or retain this gap."
        except ValueError as exc:
            result, error = None, "Tool input or authority rejected: " + str(exc)[:1500]
        except Exception as exc:
            result, error = None, f"Tool failed ({type(exc).__name__}); no successful retrieval is claimed."
        with self.workspace.lock, self.store.transaction():
            self._receipt(request, result=result, error=error, elapsed=time.monotonic() - started)

    def execute(self, request):
        """Network I/O runs outside the workspace lock and domain transaction."""
        name = request["call"]["tool"]
        arguments = ARGUMENTS[name].model_validate(request["call"]["arguments"]).model_dump(exclude={"schema_version"})
        session = self.store.get(request["session_id"], "discovery_session")
        campaign_id = request["campaign_id"]
        if name == "task.supersede":
            return self.controller.supersede_task(request, arguments)
        if name in {"assessment.prepare", "assessment.launch"}:
            return self._assessment_command(request, name, arguments)
        if name == "assessment.wait":
            record = self.controller._evidence(session, arguments["assessment_id"], task=self.store.get(request["task_id"], "discovery_task"))
            if self.store.get_entry(record["id"])["kind"] != "discovery_assessment":
                raise ValueError("Wait requires an assessment record")
            # Waiting consumes no model calls. The ordinary numerical scheduler
            # and its per-trial wall limits own execution and cancellation.
            while not self.workspace.shutdown_event.wait(.5):
                current = self.store.get(session["id"], "discovery_session")
                evidence = self.controller.assessments.evidence(record["id"])
                trials = evidence["measurements"]
                if current["status"] in {"paused", "stopped"} or not trials or all(row["status"] not in {"queued", "running", "pausing", "stopping"} for row in trials):
                    return {"assessment_id": record["id"], "evidence": evidence, "session_status": current["status"]}
            raise ValueError("Workspace stopped while awaiting experiments; the saved assessment can be inspected after restart")
        if name in {"source.search", "source.ingest"}:
            from optimization_framework.research.sources import deliver
            receipt = deliver(self.workspace, {"id": "effect_" + request["id"], "campaign_id": campaign_id,
                "kind": "literature_search" if name == "source.search" else "source_ingest", **arguments})
            return {"retrieval_id": receipt["id"], **receipt["result"]}
        if name == "source.read":
            self.controller._evidence(session, arguments["source_id"] or arguments["capture_id"])
            return self.reader.read(campaign_id, **arguments)
        if name in {"evidence.read", "context.read", "experiment.inspect", "assessment.inspect"}:
            from .record_view import view_record
            task = self.store.get(request["task_id"], "discovery_task")
            view = {"record_id": arguments["record_id"], "pointer": arguments["pointer"],
                    "offset": arguments["offset"], "limit": arguments["limit"], "tool": name,
                    "max_bytes": arguments["max_bytes"]}
            if name == "context.read":
                # This capability exposes only the caller's already authorized
                # frozen snapshot, never a peer's run, provider config or trace.
                if arguments["record_id"] != task.get("run_id"):
                    raise ValueError("Context reads require this task's own saved research run")
                run = self.store.get(arguments["record_id"], "research_run")
                if (run.get("discovery_task_id") != task["id"] or
                        run.get("discovery_session_id") != session["id"] or
                        task["session_id"] != session["id"] or
                        run["campaign_id"] != campaign_id or task["campaign_id"] != campaign_id):
                    raise ValueError("Context reads must stay within this task, session and campaign")
                return {"record": view_record(run["context_snapshot"], envelope_bytes=32, **view)}
            record = self.controller._evidence(session, arguments["record_id"], task=task)
            if name == "assessment.inspect":
                if self.store.get_entry(record["id"])["kind"] != "discovery_assessment":
                    raise ValueError("Assessment inspection requires an assessment record")
                result = {"assessment": record, "readiness": self.controller.assessments.readiness(record["id"]),
                          "evidence": self.controller.assessments.evidence(record["id"])}
                return view_record(result, **view)
            if name == "experiment.inspect":
                if self.store.get_entry(arguments["record_id"])["kind"] != "trial":
                    raise ValueError("Experiment inspection requires an experiment record")
                if record["task_id"] != session["problem_task_id"]:
                    raise ValueError("Experiment belongs to a different problem")
                return view_record({"trial": record, "curve": self.workspace.metrics(record["id"])}, **view)
            # The existing envelope is retained, including for exact small
            # subtrees. Leave room for its serialized bytes in the result cap.
            return {"record": view_record(record, envelope_bytes=32, **view)}
        catalog = self.workspace.implementations.catalog(refresh=True)
        from optimization_framework.implementations.references import catalog as reference_catalog
        from optimization_framework.optimizers.registry import methods
        versions = catalog["versions"]
        if arguments["version_id"]:
            versions = [version for version in versions if version["id"] == arguments["version_id"]]
            if not versions:
                raise ValueError("Implementation version is not available")
        return {"versions": [{key: row[key] for key in ("id", "name", "status", "spec", "validation_report") if key in row}
                             for row in versions], "bundled_methods": methods(), "connection_error": catalog["connection_error"],
                "reference_sources": reference_catalog(self.store, campaign_id),
                "reference_source_guidance": "Captured upstream code is not a validated campaign executable. "
                    "Use evidence.read with its reference ID and /files pointers to inspect and reuse the existing source."}

    def _assessment_command(self, request, name, arguments):
        from optimization_framework.contracts.commands import Command
        with self.workspace.lock, self.store.transaction():
            task = self.store.get(request["task_id"], "discovery_task")
            if task["brief"]["role"] != "campaign_manager":
                raise ValueError("Only the campaign manager may prepare or launch an assessment")
            identity = "command_" + request["id"]
            try:
                return self.store.get(identity, "work_command")["outcome"]
            except KeyError:
                pass
            session = self.store.get(request["session_id"], "discovery_session")
            campaign = self.store.get(request["campaign_id"], "campaign")
            if session["status"] not in {"running", "waiting_for_provider"} or task["status"] != "waiting":
                raise ValueError("Discovery must be running before a new numerical command is admitted")
            if name == "assessment.prepare":
                artifact = self.controller._evidence(session, arguments["source_artifact_id"])
                if artifact.get("kind") != "assessment_plan" or artifact.get("stale"):
                    raise ValueError("Prepare a current saved assessment_plan artifact")
                payload = {"session_id": session["id"], "source_artifact_id": artifact["id"], "plan": artifact["content"]}
                operation = "discovery.assessment.save"
            else:
                assessment = self.controller._evidence(session, arguments["assessment_id"])
                if self.store.get_entry(assessment["id"])["kind"] != "discovery_assessment":
                    raise ValueError("Launch requires an assessment record")
                readiness = self.controller.assessments.readiness(assessment["id"])
                payload = {"assessment_id": assessment["id"], "expected_readiness_hash": readiness["readiness_hash"]}
                operation = "discovery.assessment.launch"
            command = Command(id=identity, campaign_id=campaign["id"], operation=operation,
                expected_revision=request["charter_version"], expected_guidance_revision=request["guidance_revision"],
                expected_authority_hash=self.workspace.commands.authority_hash(campaign), payload=payload)
            return self.workspace.commands.execute(command, actor="manager")["outcome"]

    def recover(self):
        for request in self.store.list("discovery_tool"):
            if request["status"] != "running":
                continue
            with self.workspace.lock, self.store.transaction():
                try:
                    receipt = self.store.get("receipt_" + request["id"], "discovery_tool_receipt")
                except KeyError:
                    if request["call"]["tool"] == "task.supersede":
                        try:
                            resolution = self.store.get("resolution_" + request["id"], "discovery_task_resolution")
                        except KeyError:
                            # This local transaction either committed fully or
                            # did nothing. Recheck authority before retrying it.
                            request.update(status="pending")
                            self.store.put("discovery_tool", request)
                        else:
                            self._receipt(request, result=resolution["outcome"])
                        continue
                    if request["call"]["tool"] in {"assessment.prepare", "assessment.launch"}:
                        try:
                            command = self.store.get("command_" + request["id"], "work_command")
                        except KeyError:
                            request.update(status="pending")
                            self.store.put("discovery_tool", request)
                        else:
                            self._receipt(request, result=command["outcome"])
                        continue
                    if request["call"]["tool"] == "assessment.wait":
                        request.update(status="pending")
                        self.store.put("discovery_tool", request)
                        continue
                    # A source service may have committed before the task receipt.
                    try:
                        source = self.store.get("retrieval_effect_" + request["id"], "source_retrieval")
                    except KeyError:
                        self._receipt(request, error="Tool interrupted without a saved result; no automatic external retry was issued", uncertain=True)
                    else:
                        self._receipt(request, result={"retrieval_id": source["id"], **source["result"]})
                else:
                    request.update(status=receipt["status"], receipt_id=receipt["id"])
                    self.store.put("discovery_tool", request)

    def results(self, task):
        receipts = []
        for identity in task.get("pending_tool_ids", []):
            try:
                receipts.append(self.store.get("receipt_" + identity, "discovery_tool_receipt"))
            except KeyError:
                return None
        return receipts
