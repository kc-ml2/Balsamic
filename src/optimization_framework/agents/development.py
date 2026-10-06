"""Persistent implementation workspaces, independent of short validation grants."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from optimization_framework.contracts.base import Contract, content_hash
from optimization_framework.storage.sqlite import atomic_json, now
from .development_runtime import DockerWorkspace


class WorkspaceCreate(Contract):
    hypothesis_id: str
    objective: str = Field(default="Implement this method from the saved specification and references.", min_length=1, max_length=50000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    request_key: str = Field(min_length=1, max_length=200)
    cpu_budget_seconds: float | None = Field(default=None, gt=0, le=86400)


class WorkspaceMessage(Contract):
    message: str = Field(min_length=1, max_length=50000)
    mode: Literal["steer", "follow_up"] = "steer"
    request_key: str = Field(min_length=1, max_length=200)
    question_id: str | None = None


class WorkspaceControl(Contract):
    action: Literal["pause", "resume", "stop"]
    request_key: str = Field(min_length=1, max_length=200)
    expected_revision: int = Field(ge=0)


class SubmissionManifest(Contract):
    contract: Literal["ask_tell", "optimizer_v1"] = "optimizer_v1"
    entrypoint: str = "optimizer:create_optimizer"
    files: list[str] = Field(min_length=1, max_length=100)
    dependencies: dict[str, str] = Field(default_factory=dict, max_length=20)
    test_summary: str = Field(default="", max_length=10000)


class WorkspaceValidate(Contract):
    submission_id: str
    spec: dict | None = None
    envelope_id: str | None = None
    compute_seconds: float = Field(gt=0, le=86400)
    request_key: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def one_specification(self):
        if (self.spec is None) == (self.envelope_id is None):
            raise ValueError("Provide exactly one frozen spec or validation envelope ID")
        return self


class WorkspaceEnvelope(Contract):
    spec: dict
    request_key: str = Field(min_length=1, max_length=200)


class DevelopmentWorkspaces:
    def __init__(self, controller, driver=None):
        self.controller = controller
        self.workspace = controller.workspace
        self.store = controller.store
        self.directory = self.workspace.directory / "development"
        self.driver = driver or DockerWorkspace(self.directory)

    def enabled(self):
        return os.environ.get("GRATING_DEVELOPMENT_ENABLED", "false").lower() == "true"

    def get(self, campaign_id, identity):
        record = self.store.get(identity, "development_workspace")
        if record["campaign_id"] != campaign_id:
            raise ValueError("Implementation workspace belongs to another campaign")
        return record

    def public(self, record):
        base = os.environ.get("GRATING_DEVELOPMENT_PUBLIC_URL", "").rstrip("/")
        runtime = record.get("runtime") or {}
        return {**{k: v for k, v in record.items() if k not in {"runtime", "cpu_last", "event_files", "request_hash"}},
            "ide_url": base + f"/implementation-workspaces/{record['id']}/ide/?folder=/work/repo",
            "runtime": {"running": runtime.get("running", False), "paused": runtime.get("paused", False)},
            "terminal_command": f"docker exec --user 1000:1000 -it {self.driver.name(record)} tmux attach-session -t implementation",
            "validation_envelopes": [{k: v for k, v in e.items() if k != "spec"}
                for e in self.store.list("development_envelope", record["campaign_id"]) if e["workspace_id"] == record["id"]],
            "submissions": [{k: v for k, v in s.items() if k != "package"} for s in self.store.list("development_submission", record["campaign_id"]) if s["workspace_id"] == record["id"]],
            "questions": [q for q in self.store.list("development_question", record["campaign_id"]) if q["workspace_id"] == record["id"] and q["status"] == "pending"]}

    def view(self, campaign_id):
        return {"enabled": self.enabled(), "workspaces": [self.public(r) for r in self.store.list("development_workspace", campaign_id)]}

    def create(self, campaign_id, payload, *, actor="researcher"):
        if not self.enabled():
            raise ValueError("Full implementation workspaces are disabled in the server configuration")
        values = WorkspaceCreate.model_validate(payload)
        hypothesis = self.store.get(values.hypothesis_id, "hypothesis")
        if hypothesis["campaign_id"] != campaign_id or hypothesis["status"] in {"archived", "finalist"}:
            raise ValueError("Choose an active hypothesis in this campaign")
        identity = "development_" + content_hash([campaign_id, values.hypothesis_id])[:24]
        with self.workspace.lock, self.store.transaction():
            try:
                saved = self.get(campaign_id, identity)
                if saved["request_key"] == values.request_key and saved["request_hash"] != content_hash(values.model_dump()):
                    raise ValueError("Workspace request key was reused with different content")
                return self.public(saved)
            except KeyError:
                pass
            for evidence_id in values.evidence_ids:
                self.evidence(campaign_id, evidence_id)
            config = self.controller.configuration(campaign_id)
            if not config or not config["enabled"]:
                raise ValueError("Activate the campaign lead agent before creating an implementation workspace")
            record = {"id": identity, "campaign_id": campaign_id, "hypothesis_id": values.hypothesis_id,
                "title": hypothesis["title"], "parent_agent_id": config["lead_id"], "created_at": now(),
                "objective": values.objective, "evidence_ids": values.evidence_ids,
                "request_key": values.request_key, "request_hash": content_hash(values.model_dump()),
                "status": "queued", "desired_status": "running", "control_revision": 0,
                "guidance_revision": 0, "cpu_budget_seconds": values.cpu_budget_seconds,
                "usage": {"container_cpu_seconds": 0, "subscription_calls": 0, "input": 0, "output": 0,
                          "billing_mode": "subscription", "api_cost_usd": 0}, "event_files": [], "cursor": 0}
            self.store.put("development_workspace", record, "development.created")
            self.message(campaign_id, identity, {"message": values.objective +
                "\nRead .campaign/assignment.md and .campaign/evidence.json first. Save checkpoints in .campaign/checkpoint.md. "
                "Use the normal Pi coding tools to implement, debug, and test. Submit a committed package with campaign_submit. "
                "Use campaign_checkpoint for progress, questions, and blockers. Continue authorized development until a concrete submission or genuine blocker.",
                "request_key": "initial-" + values.request_key, "mode": "follow_up"}, actor=actor)
            return self.public(self.get(campaign_id, identity))

    def evidence(self, campaign_id, identity):
        entry = self.store.get_entry(identity)
        data = entry["data"]
        if data.get("campaign_id") != campaign_id or entry["kind"] not in {
                "source", "source_excerpt", "hypothesis", "agent_artifact", "discovery_handoff", "discovery_artifact",
                "discovery_candidate", "implementation_reference", "reference_correction"}:
            raise ValueError("Evidence is not shareable implementation context in this campaign")
        if data.get("agent_id"):
            author = self.store.get(data["agent_id"], "agent_session")
            if author["role"] in {"implementation_test_designer", "implementation_validator"}:
                raise ValueError("Protected reviewer context cannot enter a builder workspace")
        return {"kind": entry["kind"], "data": data}

    def _command(self, record, values, actor):
        identity = "development_command_" + content_hash([record["id"], values["request_key"]])[:24]
        request_hash = content_hash([values, actor])
        try:
            old = self.store.get(identity, "development_command")
            # Commands saved before the role rename hashed actor "pi".
            legacy = content_hash([values, "pi"]) if actor == "lead" else request_hash
            if old["request_hash"] not in {request_hash, legacy}:
                raise ValueError("Command identity was reused with different content")
            return old
        except KeyError:
            pass
        return self.store.put("development_command", {"id": identity, "campaign_id": record["campaign_id"],
            "workspace_id": record["id"], "created_at": now(), "request_hash": request_hash,
            "actor": actor, "status": "queued", **values}, "development.command_queued")

    def message(self, campaign_id, identity, payload, *, actor="researcher"):
        values = WorkspaceMessage.model_validate(payload)
        with self.workspace.lock, self.store.transaction():
            record = self.get(campaign_id, identity)
            command = self._command(record, {"operation": "message", **values.model_dump()}, actor)
            if command.get("recorded"):
                return command
            if values.question_id:
                question = self.store.get(values.question_id, "development_question")
                if question["workspace_id"] != identity or question["status"] != "pending":
                    raise ValueError("Question is not pending in this workspace")
                question.update(status="answered", answer=values.message, answered_at=now())
                self.store.put("development_question", question)
            if actor == "researcher":
                record["guidance_revision"] += 1
                self.store.put("development_workspace", record)
                self.notify(record, command["id"], "The developer directed the implementation agent: " + values.message)
            command.update(recorded=True, guidance_revision=record["guidance_revision"])
            self.store.put("development_command", command)
            self.event(record, "input_" + command["id"], "instruction", {"text": values.message, "actor": actor, "command_id": command["id"], "status": "queued"})
            return command

    def control(self, campaign_id, identity, payload, *, actor="researcher"):
        values = WorkspaceControl.model_validate(payload)
        with self.workspace.lock, self.store.transaction():
            record = self.get(campaign_id, identity)
            command = self._command(record, {"operation": values.action, **values.model_dump()}, actor)
            if command.get("recorded"):
                return self.public(record)
            if record["control_revision"] != values.expected_revision:
                raise ValueError("Workspace control changed; refresh before continuing")
            if values.action == "resume" and record.get("cpu_budget_seconds") is not None and record["usage"]["container_cpu_seconds"] >= record["cpu_budget_seconds"]:
                raise ValueError("Development CPU allowance is exhausted; retain the checkpoint and explicitly extend the allowance")
            record.update(desired_status={"resume": "running", "pause": "paused", "stop": "stopped"}[values.action],
                control_revision=record["control_revision"] + 1, control_requested_at=now(),
                status="resuming" if values.action == "resume" else "pausing" if values.action == "pause" else "stopping")
            record.pop("error", None)
            command["recorded"] = True
            self.store.put("development_command", command)
            self.store.put("development_workspace", record, "development.control_requested")
            return self.public(record)

    def notify(self, record, key, text):
        config = self.controller.configuration(record["campaign_id"])
        if not config or not config["enabled"]:
            return
        parent = self.store.get(config["lead_id"], "agent_session")
        self.controller.enqueue(parent, "pi_development_" + content_hash([record["id"], key])[:24],
            f"Implementation workspace {record['id']} ({record['title']}): {text}\n"
            "Use implementation_workspace_inspect to read current status and saved evidence. A development result is not protected validation.")

    def event(self, record, key, event_type, payload):
        identity = "development_event_" + content_hash([record["id"], key])[:24]
        try:
            return self.store.get(identity, "development_event")
        except KeyError:
            pass
        current = self.get(record["campaign_id"], record["id"])
        current["cursor"] += 1
        self.store.put("development_workspace", current)
        return self.store.put_immutable("development_event", {"id": identity, "campaign_id": record["campaign_id"],
            "workspace_id": record["id"], "seq": current["cursor"], "type": event_type, "occurred_at": now(), "payload": payload})

    def events(self, campaign_id, identity, after=0):
        record = self.get(campaign_id, identity)
        events = sorted((e for e in self.store.list("development_event", campaign_id)
                         if e["workspace_id"] == identity and e["seq"] > after), key=lambda e: e["seq"])
        events = events[:100] if after else events[-100:]
        commands = [c for c in self.store.list("development_command", campaign_id) if c["workspace_id"] == identity]
        return {"workspace": self.public(record), "events": events, "cursor": events[-1]["seq"] if events else after, "commands": commands[-100:]}

    def sync(self, campaign_id):
        for saved in self.store.list("development_workspace", campaign_id):
            try:
                self._sync_one(saved)
            except Exception as exc:
                with self.workspace.lock, self.store.transaction():
                    record = self.get(campaign_id, saved["id"])
                    error = str(exc)[:2000]
                    changed = record.get("error") != error
                    record.update(error=error, status="blocked")
                    self.store.put("development_workspace", record, "development.blocked" if changed else None)
                    if changed:
                        self.notify(record, "blocker_" + content_hash(error), error + " Saved work is retained; capability will be rechecked automatically.")

    def _sync_one(self, saved):
        cid, identity = saved["campaign_id"], saved["id"]
        root = self.driver.root(saved)
        record = self.get(cid, identity)
        if record["desired_status"] == "running" and self.enabled():
            if not (root / "prepared.json").exists():
                capability = self.driver.capability()
                if not capability["available"]:
                    raise ValueError(capability["reason"])
                evidence = [self.evidence(cid, ref) for ref in record["evidence_ids"]]
                hypothesis = self.store.get(record["hypothesis_id"], "hypothesis")
                brief = f"# {record['title']}\n\n{record['objective']}\n\n## Hypothesis\n\n" + json.dumps(hypothesis, indent=2) + "\n\n" + DEVELOPMENT_INSTRUCTIONS
                self.driver.prepare(record, brief, evidence)
                atomic_json(root / "prepared.json", {"workspace_id": identity})
            runtime = self.driver.start(record)
            with self.workspace.lock:
                record = self.get(cid, identity)
                recovered = bool(record.get("error"))
                record.update(runtime=runtime)
                record.pop("error", None)
                if record["status"] in {"queued", "blocked", "resuming", "stopped"}:
                    record["status"] = "starting"
                self.store.put("development_workspace", record)
                if recovered:
                    self.notify(record, "recovered_" + str(record["cursor"]), "Workspace capability recovered. Continue within existing authorization.")
        runtime = record.get("runtime") or self.driver.inspect(record)
        cpu = self.driver.cpu_seconds(runtime)
        with self.workspace.lock:
            record = self.get(cid, identity)
            if cpu is not None:
                old = record.get("cpu_last", 0)
                record["usage"]["container_cpu_seconds"] += cpu - old if cpu >= old else cpu
                record["cpu_last"] = cpu
                self.store.put("development_workspace", record)
        # Host-authored commands are mounted read-only in the container.
        for command in self.store.list("development_command", cid):
            if command["workspace_id"] != identity or command["status"] != "queued":
                continue
            target = root / "incoming/commands" / (command["created_at"].replace(":", "-") + "_" + command["id"] + ".json")
            if target.parent.exists() and not target.exists():
                atomic_json(target, command)
        self.receive(record)
        self.validation_feedback(record)
        record = self.get(cid, identity)
        budget = record.get("cpu_budget_seconds")
        used = record["usage"]["container_cpu_seconds"]
        if budget is not None and used >= budget and record["desired_status"] == "running":
            with self.workspace.lock:
                record.update(desired_status="paused", status="pausing", error="Development CPU allowance reached. Workspace and session are saved.")
                self.store.put("development_workspace", record, "development.budget_reached")
                self.notify(record, "budget_" + str(budget), record["error"])
        elif budget is not None and used >= .8 * budget and not record.get("budget_checkpoint_requested"):
            self.message(cid, identity, {"message": "Development CPU allowance is approaching its limit. Save .campaign/checkpoint.md and use campaign_checkpoint with remaining work before starting more expensive commands.",
                "request_key": "budget-checkpoint-" + str(budget), "mode": "steer"}, actor="system")
            record = self.get(cid, identity)
            record["budget_checkpoint_requested"] = True
            self.store.put("development_workspace", record)
        if record["desired_status"] in {"paused", "stopped"}:
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(record.get("control_requested_at", record["created_at"]))).total_seconds()
            if record.get("control_acknowledged") or elapsed >= 5 or (budget is not None and used >= budget):
                if record["desired_status"] == "paused":
                    self.driver.pause(record)
                else:
                    self.driver.stop(record)
                with self.workspace.lock:
                    current = self.get(cid, identity)
                    current["status"] = record["desired_status"]
                    current.pop("control_acknowledged", None)
                    self.store.put("development_workspace", current)

    def receive(self, record):
        root = self.driver.root(record)
        for path in sorted((root / "outgoing/events").glob("*.json"))[:200]:
            if path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
                continue
            try:
                event = json.loads(path.read_text())
                key = path.stem
                with self.workspace.lock, self.store.transaction():
                    current = self.get(record["campaign_id"], record["id"])
                    event_id = "development_event_" + content_hash([record["id"], key])[:24]
                    try:
                        self.store.get(event_id, "development_event")
                        continue
                    except KeyError:
                        pass
                    kind, payload = event["type"], event.get("payload", {})
                    # Submissions execute no candidate code on the host. Capture below,
                    # outside the transaction, before any scientific authority is granted.
                    if kind == "submission":
                        pass
                    else:
                        self._apply_event(current, key, kind, payload)
                        self.event(current, key, kind, payload)
                if kind == "submission":
                    self.capture_submission(record, key, payload)
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                with self.workspace.lock, self.store.transaction():
                    self.event(record, path.stem, "bridge_error", {"error": str(exc)[:2000]})
                atomic_json(root / "incoming/results" / (path.stem + ".json"), {"error": str(exc)[:2000]})
            finally:
                # The canonical append-only record survives; archiving also bounds polling.
                archive = root / "outgoing/received"
                archive.mkdir(exist_ok=True)
                path.rename(archive / path.name)

    def _apply_event(self, record, key, kind, payload):
        cid = record["campaign_id"]
        if kind == "receipt":
            command = self.store.get(payload["command_id"], "development_command")
            if command["workspace_id"] != record["id"]:
                raise ValueError("Receipt belongs to another workspace")
            command.update(status=payload["status"], acknowledged_at=now())
            self.store.put("development_command", command)
            if command["operation"] in {"pause", "stop"}:
                record["control_acknowledged"] = True
        elif kind == "ready":
            record.update(status="idle", session_id=payload.get("session_id"), active_tools=payload.get("tools", []))
        elif kind == "agent_start":
            record["status"] = "running"
        elif kind == "agent_settled":
            record["status"] = "idle"
        elif kind == "checkpoint":
            record["checkpoint"] = payload
            record["activity"] = payload.get("summary", "")[:2000]
            if payload.get("question"):
                self.store.put("development_question", {"id": "development_question_" + content_hash([record["id"], key])[:24],
                    "workspace_id": record["id"], "campaign_id": cid, "question": payload["question"], "status": "pending", "created_at": now()}, "development.question_created")
            self.notify(record, key, json.dumps(payload))
        elif kind == "message" and payload.get("role") == "assistant":
            usage = payload.get("usage")
            if usage:
                record["usage"]["subscription_calls"] += 1
                for name in ("input", "output", "cacheRead", "cacheWrite"):
                    record["usage"][name] = record["usage"].get(name, 0) + usage.get(name, 0)
            record["activity"] = payload.get("text", "")[-2000:]
        record["last_activity_at"] = now()
        self.store.put("development_workspace", record)

    def capture_submission(self, record, key, payload):
        package, manifest = self.driver.snapshot(record, payload["commit"], payload.get("manifest_path", "implementation-manifest.json"))
        with self.workspace.lock, self.store.transaction():
            identity = "development_submission_" + content_hash([record["id"], payload["commit"], package, manifest])[:24]
            submission = {"id": identity, "campaign_id": record["campaign_id"], "workspace_id": record["id"],
                "hypothesis_id": record["hypothesis_id"], "commit": payload["commit"], "package": package,
                "manifest": manifest, "notes": payload.get("notes", ""), "status": "submitted", "created_at": now()}
            try:
                saved = self.store.get(identity, "development_submission")
            except KeyError:
                saved = self.store.put("development_submission", submission, "development.submitted")
                self.notify(record, identity, f"Commit {submission['commit']} submitted as {identity}. Review its manifest and commission independent validation using implementation_workspace_validate within the remaining implementation allocation.")
            self.event(record, key, "submission", {"submission_id": identity, "commit": payload["commit"]})
        atomic_json(self.driver.root(record) / "incoming/results" / (key + ".json"), {"submission_id": saved["id"], "status": saved["status"]})

    def validate(self, campaign_id, identity, payload):
        from optimization_framework.implementations.models import BoundOptimizerSpec, ImplementationSpec
        from .capabilities import implementation_execution
        values = WorkspaceValidate.model_validate(payload)
        record = self.get(campaign_id, identity)
        submission = self.store.get(values.submission_id, "development_submission")
        if submission["workspace_id"] != identity:
            raise ValueError("Submission belongs to another workspace")
        source = values.spec
        if values.envelope_id:
            envelope = self.store.get(values.envelope_id, "development_envelope")
            if envelope["workspace_id"] != identity or envelope["campaign_id"] != campaign_id:
                raise ValueError("Validation envelope belongs to another workspace")
            source = envelope["spec"]
        spec = (BoundOptimizerSpec if source.get("evaluator_version_id") else ImplementationSpec).model_validate(source)
        if spec.dependencies != submission["manifest"]["dependencies"]:
            raise ValueError("Validation dependencies must match the committed submission manifest")
        capability = implementation_execution()
        if not capability["available"]:
            raise ValueError("Protected validation is unavailable: " + capability["reason"])
        grant = self.workspace.implementations.reserve_commission(record["hypothesis_id"], spec,
            compute_seconds=values.compute_seconds, max_calls=12, api_budget_usd=0,
            idempotency_key="development-" + content_hash([identity, values.request_key])[:24],
            package=submission["package"], accounting_mode="execution_v1")
        submission.update(grant_id=grant["id"], status="validating")
        self.store.put("development_submission", submission, "development.validation_requested")
        return {"submission_id": submission["id"], "grant_id": grant["id"]}

    def freeze_envelope(self, campaign_id, identity, payload):
        from optimization_framework.implementations.models import BoundOptimizerSpec, ImplementationSpec, digest
        values = WorkspaceEnvelope.model_validate(payload)
        record = self.get(campaign_id, identity)
        spec = (BoundOptimizerSpec if values.spec.get("evaluator_version_id") else ImplementationSpec).model_validate(values.spec)
        if spec.problem_id != self.store.get(record["hypothesis_id"], "hypothesis").get("problem_id", spec.problem_id):
            raise ValueError("Validation envelope uses a different problem")
        serialized = spec.model_dump(mode="json")
        identity_hash = digest(serialized)
        envelope_id = "development_envelope_" + content_hash([identity, identity_hash])[:24]
        with self.workspace.lock, self.store.transaction():
            try:
                saved = self.store.get(envelope_id, "development_envelope")
            except KeyError:
                saved = self.store.put_immutable("development_envelope", {"id": envelope_id,
                    "workspace_id": identity, "campaign_id": campaign_id, "hypothesis_id": record["hypothesis_id"],
                    "created_at": now(), "request_key": values.request_key, "spec_digest": identity_hash,
                    "spec": serialized, "checks": {"behavior": len(spec.behavior_checks),
                        "mechanism": len(spec.mechanism_checks), "diagnostic": len(spec.diagnostic_checks)},
                    "problem_id": spec.problem_id, "n_cells": [spec.n_cells_min, spec.n_cells_max],
                    "dependencies": spec.dependencies}, "development.envelope_frozen")
                self.notify(record, envelope_id, f"Protected validation envelope {envelope_id} frozen with spec digest {identity_hash}. Use this ID with implementation_workspace_validate after an exact submission and explicit remaining allocation; do not reinsert the full specification into the lead agent's context.")
            return {k: v for k, v in saved.items() if k != "spec"}

    def validation_feedback(self, record):
        for submission in self.store.list("development_submission", record["campaign_id"]):
            if submission["workspace_id"] != record["id"] or not submission.get("grant_id") or submission.get("feedback_sent"):
                continue
            grant = self.store.get(submission["grant_id"], "implementation_grant")
            if grant["status"] not in {"completed", "failed", "blocked", "interrupted", "cancelled", "needs_reconciliation"}:
                continue
            # Only public findings and the grant identity go back; protected fixture
            # source and independent reviewer context remain in the service.
            feedback = {k: grant.get(k) for k in ("id", "status", "version_id", "error", "compute_seconds")}
            feedback["findings"] = []
            for attempt in grant.get("attempts", []):
                report = attempt.get("report") or {}
                feedback["findings"].extend({"check": check.get("name"), "detail": str(check.get("detail", ""))[:1000]}
                    for check in report.get("checks", []) if check.get("passed") is False)
                review = attempt.get("review") or {}
                feedback["findings"].extend({"review": str(item)[:1000]} for item in review.get("findings", []))
            self.message(record["campaign_id"], record["id"], {"message": "Independent validation outcome for " + submission["commit"] + ":\n" + json.dumps(feedback)
                + "\nInspect the recorded outcome. Preserve this revision. Repair failures in a new commit when authorized; a new validation run needs remaining allocation.",
                "request_key": "validation-feedback-" + submission["id"], "mode": "follow_up"}, actor="system")
            submission.update(status="validated" if grant["status"] == "completed" else "needs_revision", feedback_sent=True,
                              version_id=grant.get("version_id"), validation_outcome=feedback)
            self.store.put("development_submission", submission)


DEVELOPMENT_INSTRUCTIONS = """## Development workflow

This is a complete Pi coding workspace. Use read, edit, write, bash, Git, tests,
dependency installation, skills, and project instructions. You can install Python
packages with `python -m pip install --user ...`. Code runs in this workspace's
container. `/references/flrl` contains the author's reference code when available;
`/references/paper.txt` and `/references/paper.pdf` contain the supplied paper.
The host campaign database, private files, protected validation fixtures, and
Docker socket are outside this environment. Public network access is available;
new connections to host, TailNet, and private network services are blocked.

The developer and the campaign lead agent can steer this same session. Explicit
developer direction takes precedence over conflicting lead-agent guidance. Reread files changed by
the developer before editing them. Save decisions and continuation instructions
in `.campaign/checkpoint.md`; use `campaign_checkpoint` for progress or questions.
Compaction and reconnects preserve this file, evidence, repository, and session.

Commit your implementation and an `implementation-manifest.json` containing
`contract` (optimizer_v1 or ask_tell), `entrypoint` (module:factory), `files` (a
list of repository-relative source paths), exact `dependencies`, and a
`test_summary`. Submit the full commit hash with `campaign_submit`. The service
captures source from that commit. A submission is not validation or publication.
The lead agent commissions independent checks; failure feedback returns to this session.
Use a new commit for each repair. Keep physical evaluations distinct from unit
tests and do not claim optimizer effectiveness without campaign measurements.

This session has no programming wall-clock deadline. Container CPU usage is
measured separately; an optional development CPU allowance is shown in the
campaign. Existing experiment and protected validation allocations still apply.
"""
