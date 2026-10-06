"""Real Pi tools backed by scoped reads and transactional campaign commands."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

from optimization_framework.contracts.base import content_hash
from optimization_framework.contracts.commands import Command
from optimization_framework.storage.sqlite import now
from optimization_framework.research.discovery.record_view import view_record
from .models import ROLES, LEAD
from .capabilities import implementation_execution

READ_KINDS = {"campaign", "task", "hypothesis", "source", "source_capture", "source_passage", "source_retrieval",
    "study", "trial", "decision", "decision_refresh", "action", "manager_note", "manager_command", "manager_issue", "discovery_candidate", "discovery_artifact",
    "discovery_task", "discovery_assessment", "discovery_handoff", "discovery_session", "implementation_reference",
    "implementation_binding", "implementation_grant", "implementation_cache", "reference_correction", "agent_migration", "agent_session",
    "agent_alias", "agent_artifact", "agent_run", "agent_question", "agent_tool_receipt", "fixed_mask_job", "fixed_mask_report",
    "builtin_binding", "manager_state", "message", "methodology_family", "discovery_reference_correction", "discovery_task_resolution", "research_run", "work_command",
    "adaptive_race", "race_protocol", "race_decision", "race_endpoint", "race_confirmation_roster",
    "race_resource_calibration", "race_fidelity_amendment", "race_execution_lease"}
OPERATIONS = {"hypothesis.create", "hypothesis.review", "hypothesis.status", "draft.save", "draft.launch", "trial.create",
    "trial.control", "trial.extension_request", "implementation.commission", "implementation.attach", "implementation.revalidate", "implementation.bind_builtin",
    "implementation.resolve_runtime", "evaluator.commission", "evaluator.attach", "finding.record", "comparison.report",
    "validation.run", "validation.execute", "study.create", "study.activate", "fixed_mask.run",
    "study.race.control", "study.race.decide", "asset.snapshot"}
INSTRUCTIONS = """You are a persistent Pi agent in a scientific campaign. Your role and assignment are supplied below.
Use real tools to investigate, implement, test, and coordinate. A natural-language request is not an execution receipt.
The campaign database is authoritative for current objectives, budgets, evaluator readiness and measured results.
Inspect campaign_inspect first and after researcher steering. Read evidence on demand; do not load the whole archive.
Use resource_inspect to check live host capacity, measured worker consumption, planned allocations and resource blockers
before scheduling numerical work. Forecasts are estimates; a closed execution window does not authorize a new one.
Preserve exact IDs returned by tools. Historical summaries and reviews are evidence, not new authority.
Use artifact_save for incremental findings and unresolved objections before context compaction or a handoff.
Do not confuse conceptual approval, executable correctness, and measured optimizer effectiveness.
Existing working evaluators and bundled implementations must be reused when compatible. Ask for missing information
only after checking the current capabilities and saved evidence. Do not repeat denied reads or duplicate reviews.
The lead agent owns delegation and execution under existing campaign grants. Subagents return artifacts and findings to the lead agent.
A subagent that needs more time or evaluations for a trial says so, with its reasons, in its findings for the lead.
The lead may pause, stop or resume trials within their allocations but never enlarges one. When more budget would
change a decision, the lead files trial.extension_request with that argument; only the researcher approves it.
Implementation builders, test designers and reviewers work independently under frozen service specifications.
For substantial programming, use implementation_workspace_create to delegate to a full native Pi coding session
with a browser IDE and normal shell/Git tools. Use implementation_workspace_inspect, implementation_workspace_message,
and implementation_workspace_validate for supervision and independent validation. Do not assume that workspace
creation or a submitted commit establishes correctness. The old short-script implementation path remains available
for historical grants and lightweight compatibility work.
Use command_schema to inspect supported commands, then campaign_command with the exact current guidance revision
and a stable request_key describing the intended operation. If a request timed out, inspect its receipt before retrying.
For 2D masks, each TE/TM evaluation costs two forward solves; source-code availability is not effectiveness evidence.
When awaiting a child or numerical job, use yield_work with disposition=waiting and return; completion events wake you.
If useful independent work remains, do it or delegate it. Finishing a response does not complete the campaign.
Use researcher_ask only for a real scientific choice, missing credential, or a campaign resource increase; expose a concrete question.
Never increase campaign budgets, fabricate validation, edit protected checks as a builder, or modify the running app.
The user authorized end-to-end work within campaign allocations. Old discovery per-task quotas are historical;
campaign compute limits, protected reserves and current guidance still apply. All Pi inference uses subscription billing.
"""


def obj(properties=None, required=None):
    return {"type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False}


S = {"type": "string"}
TOOLS = {
    "campaign_inspect": ("Read current problem, runnable evaluators, methods, budgets, aliases and assignments.", obj()),
    "resource_inspect": ("Read live CPU/RAM, worker measurements, resource forecasts and blockers for this campaign. Does not start jobs or change budgets.", obj()),
    "evidence_search": ("Search saved campaign evidence across current and historical sessions; returns exact IDs. Omit kind to search all supported kinds.", obj({"query": S, "kind": {"type": "string", "enum": sorted(READ_KINDS)}, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}})),
    "evidence_read": ("Read a bounded page of an exact evidence record. Follow returned JSON pointers for large records.", obj({"record_id": S, "pointer": S, "offset": {"type": "integer", "minimum": 0}, "max_bytes": {"type": "integer", "minimum": 1024, "maximum": 24576}}, ["record_id"])),
    "source_search": ("Find primary literature and retain retrieval receipts.", obj({"query": S, "provider": {"enum": ["arxiv", "crossref"]}, "limit": {"type": "integer", "minimum": 1, "maximum": 10}}, ["query"])),
    "source_ingest": ("Capture a DOI, arXiv identifier, or public primary-source HTTPS URL.", obj({"identifier": S}, ["identifier"])),
    "source_read": ("Read saved source passages; available even after the legacy source quota was exhausted.", obj({"source_id": S, "query": S, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 12}}, ["source_id"])),
    "artifact_save": ("Save an immutable finding, implementation handoff, analysis, or fixed binary mask with evidence links.", obj({"title": S, "kind": {"enum": ["finding", "handoff", "analysis", "mask", "assessment", "review", "checkpoint"]}, "content": {}, "evidence_ids": {"type": "array", "items": S, "maxItems": 100}}, ["title", "kind", "content"])),
    "mask_create": ("Create a reproducible full-sized binary diagnostic mask without putting every cell in model context.", obj({"task_id": S, "pattern": {"enum": ["zeros", "ones", "stripes_x", "stripes_y", "seeded_random"]}, "seed": {"type": "integer", "minimum": 0}, "fill_fraction": {"type": "number", "minimum": 0, "maximum": 1}}, ["task_id", "pattern"])),
    "proposal_review": ("Record an independent conceptual review of the exact hypothesis revision. Test means plausible, not effective.", obj({"hypothesis_id": S, "hypothesis_hash": S, "verdict": {"enum": ["test", "revise", "reject"]}, "rationale": S, "required_changes": {"type": "array", "items": S}, "suggested_tests": {"type": "array", "items": S, "minItems": 1}}, ["hypothesis_id", "hypothesis_hash", "verdict", "rationale", "suggested_tests"])),
    "delegate": ("Create a persistent specialist child with a focused objective and exact evidence IDs. The lead agent receives its result automatically.", obj({"role": {"enum": [r for r in ROLES if r != LEAD]}, "objective": S, "evidence_ids": {"type": "array", "items": S, "maxItems": 100}, "request_key": S}, ["role", "objective", "request_key"])),
    "agent_message": ("Send a follow-up or steering message to one of this lead agent's children.", obj({"agent_id": S, "message": S}, ["agent_id", "message"])),
    "agent_cancel": ("Cancel a child assignment while retaining its artifacts and costs.", obj({"agent_id": S}, ["agent_id"])),
    "command_schema": ("Read the exact schema of an execution command before preparing it.", obj({"operation": {"enum": sorted(OPERATIONS)}}, ["operation"])),
    "campaign_command": ("Execute a campaign command within existing delegation. Returns a durable receipt; never grants new authority.", obj({"operation": {"enum": sorted(OPERATIONS)}, "payload": {"type": "object"}, "request_key": S, "guidance_revision": {"type": "integer", "minimum": 0}}, ["operation", "payload", "request_key", "guidance_revision"])),
    "implementation_workspace_create": ("Start or reattach a persistent full Pi coding workspace for an active hypothesis. No numerical grant is reserved by creation.", obj({"hypothesis_id": S, "objective": S, "evidence_ids": {"type": "array", "items": S, "maxItems": 100}, "request_key": S, "cpu_budget_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 86400}}, ["hypothesis_id", "objective", "request_key"])),
    "implementation_workspace_inspect": ("Read implementation workspace state, progress, questions, submissions, and event cursor.", obj({"workspace_id": S, "after": {"type": "integer", "minimum": 0}}, ["workspace_id"])),
    "implementation_workspace_message": ("Steer or follow up with the full Pi coding agent in the given workspace.", obj({"workspace_id": S, "message": S, "mode": {"enum": ["steer", "follow_up"]}, "request_key": S, "question_id": S}, ["workspace_id", "message", "request_key"])),
    "implementation_workspace_validate": ("Commission independent validation of one committed source submission within the campaign's implementation allocation. Supply either a frozen envelope_id or a spec; prefer the short envelope ID when available.", obj({"workspace_id": S, "submission_id": S, "envelope_id": S, "spec": {"type": "object"}, "compute_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 86400}, "request_key": S}, ["workspace_id", "submission_id", "compute_seconds", "request_key"])),
    "researcher_ask": ("Save a concrete researcher question and stop for its answer only when required.", obj({"question": S, "reason": S}, ["question", "reason"])),
    "yield_work": ("Checkpoint task disposition. Waiting jobs wake the lead agent automatically; completion must cite actual evidence.", obj({"disposition": {"enum": ["waiting", "complete", "handoff"]}, "summary": S, "evidence_ids": {"type": "array", "items": S, "maxItems": 100}}, ["disposition", "summary"])),
    "workspace_list": ("List this implementation assignment's isolated files.", obj()),
    "workspace_read": ("Read a text file from this assignment's isolated development directory.", obj({"path": S}, ["path"])),
    "workspace_write": ("Write a package or development test file in this assignment's isolated directory.", obj({"path": S, "content": S}, ["path", "content"])),
    "workspace_run": ("Run a Python development script in a network-disabled OS sandbox under the implementation grant.", obj({"path": S, "seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 30}}, ["path"])),
    "submit_output": ("Submit the structured result required by the frozen implementation service contract.", obj({"result": {"type": "object"}}, ["result"])),
}


class PiTools:
    def __init__(self, controller):
        self.controller = controller
        self.workspace, self.store = controller.workspace, controller.store

    def names(self, agent):
        names = {"campaign_inspect", "resource_inspect", "evidence_search", "evidence_read", "source_search", "source_ingest", "source_read", "artifact_save", "mask_create", "yield_work"}
        if agent["role"] == LEAD:
            names |= {"delegate", "agent_message", "agent_cancel", "command_schema", "campaign_command", "researcher_ask",
                      "implementation_workspace_create", "implementation_workspace_inspect", "implementation_workspace_message",
                      "implementation_workspace_validate"}
        if agent["role"] == "proposal_reviewer":
            names.add("proposal_review")
        if agent.get("grant_id"):
            # Protected implementation roles receive only the exact context the
            # library supplies. They cannot read campaign fixtures or peer work.
            names = {"artifact_save", "workspace_list", "workspace_read", "workspace_write", "workspace_run", "submit_output"}
        return names

    def manifest(self, agent_id, run_id):
        agent, run = self._context(agent_id, run_id)
        instructions = INSTRUCTIONS + f"\nRole: {agent['role']}\nAssignment: {agent['objective']}\nEvidence IDs: {json.dumps(agent['evidence_ids'])}"
        if agent.get("output_schema"):
            instructions += "\nSubmit your result with submit_output using this JSON schema:\n" + json.dumps(agent["output_schema"])
        return {"instructions": instructions, "tools": [{"name": name, "description": TOOLS[name][0], "input_schema": TOOLS[name][1]} for name in sorted(self.names(agent))]}

    def _context(self, agent_id, run_id):
        agent = self.store.get(agent_id, "agent_session")
        run = self.store.get(run_id, "agent_run")
        if run["agent_id"] != agent_id or not self.controller.owns(agent["campaign_id"]):
            raise ValueError("Agent assignment is not owned by this campaign")
        config = self.controller.configuration(agent["campaign_id"])
        if config["status"] != "running" or agent["status"] in {"paused", "stopped"} or run["status"] not in {"submitted", "running"}:
            raise ValueError("Assignment is not active; preserve findings and stop")
        if agent.get("deadline_at") and time.time() >= agent["deadline_at"]:
            raise ValueError("Implementation grant expired; preserve the existing package and findings")
        return agent, run

    def call(self, agent_id, run_id, call_id, name, arguments):
        import jsonschema
        if not isinstance(call_id, str) or not 0 < len(call_id) <= 500:
            raise ValueError("Invalid tool call identity")
        identity = "pi_tool_" + content_hash([agent_id, run_id, call_id])[:32]
        with self.workspace.lock:
            lock = self.controller.tool_locks.setdefault(identity, threading.Lock())
        with lock:
            return self._call(agent_id, run_id, name, arguments, identity)

    def _call(self, agent_id, run_id, name, arguments, identity):
        import jsonschema
        request_hash = content_hash([name, arguments])
        with self.workspace.lock, self.store.transaction():
            try:
                old = self.store.get(identity, "agent_tool_receipt")
            except KeyError:
                old = None
            if old:
                if old["request_hash"] != request_hash:
                    raise ValueError("Tool identity belongs to another request")
                return old["result"]
            agent, run = self._context(agent_id, run_id)
            if name not in self.names(agent):
                raise ValueError("Tool is not available to this assignment")
            try:
                jsonschema.validate(arguments, TOOLS[name][1])
            except jsonschema.ValidationError as exc:
                return {"error": exc.message[:2000], "executed": False}
            try:
                intent = self.store.get("intent_" + identity, "agent_tool_intent")
                if intent["request_hash"] != request_hash:
                    raise ValueError("Tool identity belongs to another request")
            except KeyError:
                pass
            self.store.put("agent_tool_intent", {"id": "intent_" + identity, "campaign_id": agent["campaign_id"],
                "agent_id": agent_id, "run_id": run_id, "tool": name, "request_hash": request_hash,
                "arguments": arguments, "created_at": now()}, "agent.tool_started")
        def execute():
            try:
                return self._execute(agent, run, name, arguments, identity)
            except (ValueError, KeyError, OSError, jsonschema.ValidationError) as exc:
                return {"error": str(exc)[:3000], "executed": False,
                    "recovery": "Inspect the exact error and current capabilities before revising the request; do not repeat an identical denied call."}

        def receipt(result):
            self.store.put_immutable("agent_tool_receipt", {"id": identity, "campaign_id": agent["campaign_id"],
                "agent_id": agent_id, "run_id": run_id, "tool": name, "request_hash": request_hash, "result": result,
                "created_at": now()}, "agent.tool_completed")
        if name in {"artifact_save", "mask_create", "proposal_review", "delegate", "agent_message", "agent_cancel", "researcher_ask", "yield_work", "submit_output"}:
            # Local changes and their acknowledgment commit together. External
            # I/O and idempotent command delivery never hold a SQL transaction.
            with self.workspace.lock, self.store.transaction():
                self._context(agent_id, run_id)
                result = execute()
                receipt(result)
        else:
            result = execute()
            with self.workspace.lock, self.store.transaction():
                receipt(result)
        return result

    def record(self, agent, identity):
        entry = self.store.get_entry(identity)
        if entry["kind"] not in READ_KINDS or entry["data"].get("campaign_id", entry["data"].get("id")) != agent["campaign_id"]:
            raise ValueError("Evidence is not readable within this campaign")
        from optimization_framework.research.discovery.references import corrected_view
        return corrected_view(self.store, entry["data"]) if entry["kind"] == "discovery_artifact" else entry["data"]

    def inspect(self, agent):
        campaign_id = agent["campaign_id"]
        campaign = self.store.get(campaign_id, "campaign")
        tasks = self.workspace.current_tasks(campaign_id)
        hypotheses = self.store.list("hypothesis", campaign_id)
        from optimization_framework.research.discovery.proposals import readiness, hypothesis_revision
        team_view = self.controller.view(campaign_id)
        team_view["agents"] = [{k: a.get(k) for k in ("id", "role", "parent_agent_id", "status", "model", "usage", "event_cursor")}
            for a in team_view["agents"][-30:]]
        team_view["aliases"] = team_view["aliases"][:100]
        races = []
        for record in self.store.list("adaptive_race", campaign_id):
            view = self.workspace.racing.view(record["id"])
            races.append({**{key: view.get(key) for key in ("id", "study_id", "status", "stage", "revision", "deadline_at",
                "elapsed_seconds", "total_seconds", "max_workers", "running_workers", "worker_seconds_spent", "remaining_worker_seconds", "last_error", "report_path")},
                "preflight_status": view["preflight"]["status"],
                "configurations": [{key: row[key] for key in ("id", "algorithm", "status", "mean_score", "seeds", "rung_seconds", "maturity")}
                    for row in view["configurations"]],
                "recent_decisions": [{"id": row["id"], "action": row["action"], "rationale": row["rationale"][:1000]}
                    for row in view["decisions"][-10:]],
                "retrieval": "Read race_decision and race_endpoint evidence individually; the study race API has full endpoint details."})
        return {"campaign": campaign, "guidance_revision": self.workspace.memory.state(campaign_id)["guidance_revision"],
            "researcher_guidance": self.workspace.memory.state(campaign_id)["guidance"],
            "tasks": [{**t, "evaluator_readiness": self.workspace.evaluators.readiness(t)} for t in tasks[:20]],
            "hypothesis_count": len(hypotheses), "retrieval": "Use evidence_search to page through further tasks, hypotheses or sessions.",
            "hypotheses": [{"id": h["id"], "candidate_id": h.get("candidate_id"), "title": h["title"],
                "hypothesis_hash": hypothesis_revision(h), "concept_review": readiness(self.store, h),
                "status": h.get("status"), "algorithm": h.get("algorithm"),
                "implementation_readiness": self.workspace.implementations.readiness(h, tasks[0] if tasks else None)} for h in hypotheses[:50]],
            "resources": {"experiments": self.workspace.resources.assessment(campaign_id),
                "implementation_committed_seconds": self.workspace.implementations.compute_committed(campaign_id)},
            "adaptive_testing": races,
            "agent_team": team_view,
            "latest_researcher_requests": [{"id": r["id"], "status": r["status"], "message": r["request"]["message"]}
                for r in self.store.list("manager_command", campaign_id)[-5:]],
            "latest_researcher_messages": [{"id": r["id"], "content": r.get("content", "")[:12000]}
                for r in self.store.list("message", campaign_id) if r.get("role") == "user"][-5:],
            "capabilities": {"fixed_mask_diagnostics": "fixed_mask.run", "implementation": "implementation.commission",
                "resource_observability": "resource_inspect; live consumption and estimated future demand, without allocating compute",
                "adaptive_testing": "study.race.decide and study.race.control; immutable protocol and fresh-seed confirmation",
                "implementation_execution": implementation_execution(),
                "experiment": "draft.save then draft.launch; trial.create", "memory": "evidence_search and evidence_read",
                "guidance": "Use current implementation_readiness, not stale historical executable fields."}}

    def _execute(self, agent, run, name, args, identity):
        cid = agent["campaign_id"]
        if name == "campaign_inspect":
            return self.inspect(agent)
        if name == "resource_inspect":
            from optimization_framework.execution.observability import get_observer
            snapshot = deepcopy(get_observer(self.workspace).snapshot(cid))
            # Numerical archives and long allocation histories have separate
            # evidence reads. Keep a telemetry inspection bounded for the lead agent.
            snapshot["workers"]["jobs"] = snapshot["workers"].get("jobs", [])[:16]
            plans = snapshot.get("plans", [])
            snapshot["plan_count"] = len(plans)
            snapshot["plans"] = plans[-4:]
            for plan in snapshot["plans"]:
                plan["upcoming_jobs"] = plan.get("upcoming_jobs", [])[:12]
                plan["decisions"] = [{**row, "rationale": (row.get("rationale") or "")[:1000]}
                                     for row in plan.get("decisions", [])[-5:]]
                memory_fields = ("fidelity", "harmonic_count", "predicted_bytes", "headroom_bytes",
                                 "measured_peak_bytes", "single_matrix_bytes", "expanded_grid_shape",
                                 "expanded_complex_grid_bytes", "analytical_lower_bound_bytes", "fits_now", "basis")
                plan["memory_checks"] = [{key: row.get(key) for key in memory_fields}
                                         for row in plan.get("memory_checks", [])[:8]]
            budget = snapshot.get("budget")
            if budget:
                budget["grant_count"] = len(budget.get("grants", []))
                budget["grants"] = budget.get("grants", [])[-20:]
            snapshot["warnings"] = [str(value)[:1000] for value in snapshot.get("warnings", [])[:16]]
            return snapshot
        if name == "implementation_workspace_create":
            return self.controller.development.create(cid, args, actor=LEAD)
        if name == "implementation_workspace_inspect":
            return self.controller.development.events(cid, args["workspace_id"], args.get("after", 0))
        if name == "implementation_workspace_message":
            return self.controller.development.message(cid, args["workspace_id"], {k: v for k, v in args.items() if k != "workspace_id"}, actor=LEAD)
        if name == "implementation_workspace_validate":
            return self.controller.development.validate(cid, args["workspace_id"], {k: v for k, v in args.items() if k != "workspace_id"})
        if name == "evidence_read":
            record = self.record(agent, args["record_id"])
            return view_record(record, record_id=args["record_id"], pointer=args.get("pointer", ""), offset=args.get("offset", 0),
                limit=40, max_bytes=args.get("max_bytes", 12000), tool="evidence_read")
        if name == "evidence_search":
            kind = args.get("kind")
            if kind and kind not in READ_KINDS:
                raise ValueError("Evidence kind is not available")
            kinds = [kind] if kind else sorted(READ_KINDS)
            query = "%" + args.get("query", "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            with self.store.connection() as db:
                rows = db.execute("SELECT id,kind,json_extract(data,'$.title') AS title,json_extract(data,'$.status') AS status FROM records "
                    "WHERE (campaign_id=? OR id=?) AND kind IN (" + ",".join("?" for _ in kinds) + ") AND data LIKE ? ESCAPE '\\' ORDER BY rowid LIMIT ? OFFSET ?",
                    [cid, cid, *kinds, query, args.get("limit", 25), args.get("offset", 0)]).fetchall()
            return {"records": [dict(r) for r in rows], "next_offset": args.get("offset", 0) + len(rows)}
        if name in {"source_search", "source_ingest"}:
            from optimization_framework.research.sources import deliver
            receipt = deliver(self.workspace, {"id": identity, "campaign_id": cid,
                "kind": "literature_search" if name == "source_search" else "source_ingest",
                **({"provider": "arxiv", "limit": 5} if name == "source_search" else {}), **args})
            return receipt
        if name == "source_read":
            self.record(agent, args["source_id"])
            from optimization_framework.research.literature import LiteratureReader
            return LiteratureReader(self.workspace).read(cid, **args)
        if name == "mask_create":
            import numpy as np
            from optimization_framework.contracts.problems import ProblemInstance
            task = self.record(agent, args["task_id"])
            instance = ProblemInstance.model_validate(task["problem"])
            n = instance.candidate_schema.dimensions
            pattern = args["pattern"]
            fraction = args.get("fill_fraction", .5)
            if pattern in {"zeros", "ones"}:
                mask = np.full(n, int(pattern == "ones"))
            elif pattern == "seeded_random":
                mask = (np.random.default_rng(args.get("seed", 0)).random(n) < fraction).astype(int)
            else:
                x, y = instance.configuration.get("grid_x"), instance.configuration.get("grid_y")
                if not x or not y or x * y != n:
                    raise ValueError("Stripe masks require a declared grid_x by grid_y binary problem")
                yy, xx = np.indices((y, x))
                mask = ((xx if pattern == "stripes_x" else yy) < round((x if pattern == "stripes_x" else y) * fraction)).astype(int).ravel()
            candidate = instance.candidate_schema.canonicalize(mask.tolist())
            result = self._execute(agent, run, "artifact_save", {"title": "Fixed mask: " + pattern, "kind": "mask",
                "content": {"mask": candidate, "generator": args, "problem_identity": instance.evaluation_identity},
                "evidence_ids": [args["task_id"]]}, identity)
            return {**result, "dimensions": n, "ones": int(mask.sum()), "pattern": pattern}
        if name == "proposal_review":
            from optimization_framework.research.discovery.proposals import hypothesis_revision
            hypothesis = self.record(agent, args["hypothesis_id"])
            if self.store.get_entry(hypothesis["id"])["kind"] != "hypothesis" or hypothesis_revision(hypothesis) != args["hypothesis_hash"]:
                raise ValueError("Review must pin the exact current hypothesis revision from campaign_inspect")
            if args["verdict"] == "test" and args.get("required_changes"):
                raise ValueError("Required mechanism changes must be resolved before a test verdict")
            artifact = self.store.put_immutable("agent_artifact", {"id": "artifact_" + identity, "campaign_id": cid,
                "agent_id": agent["id"], "run_id": run["id"], "kind": "proposal_review", "content": args,
                "created_at": now()}, "hypothesis.concept_reviewed")
            hypothesis.setdefault("reviews", []).append({"id": artifact["id"], "author": agent["role"], "kind": "conceptual",
                "verdict": args["verdict"], "text": args["rationale"], "created_at": now()})
            self.store.put("hypothesis", hypothesis)
            return {"review_id": artifact["id"], "verdict": args["verdict"]}
        if name == "artifact_save":
            for ref in args.get("evidence_ids", []):
                self.record(agent, ref)
            if len(json.dumps(args).encode()) > 1024 * 1024:
                raise ValueError("Artifact exceeds 1 MiB; split it into linked artifacts")
            artifact = {"id": "artifact_" + identity, "campaign_id": cid, "agent_id": agent["id"],
                "run_id": run["id"], "created_at": now(), **args}
            saved = self.store.put_immutable("agent_artifact", artifact, "agent.artifact_saved")
            current = self.store.get(agent["id"], "agent_session")
            current["artifact_ids"] = list(dict.fromkeys([*current.get("artifact_ids", []), saved["id"]]))
            self.store.put("agent_session", current)
            return {"artifact_id": saved["id"], "content_hash": saved["content_hash"]}
        if name == "delegate":
            if args["role"].startswith("implementation_"):
                raise ValueError("Commission implementation through implementation.commission; its grant delegates builder, test designer and reviewer automatically")
            for ref in args.get("evidence_ids", []):
                self.record(agent, ref)
            aid = "agent_" + content_hash([agent["id"], args["request_key"]])[:30]
            child = self.controller.create_agent(cid, args["role"], args["objective"], aid,
                parent_id=agent["id"], evidence_ids=args.get("evidence_ids"))
            task = self.controller.enqueue(child, "assignment_" + aid, args["objective"])
            return {"agent_id": aid, "run_id": task["id"], "status": task["status"]}
        if name in {"agent_message", "agent_cancel"}:
            child = self.store.get(args["agent_id"], "agent_session")
            if child["parent_agent_id"] != agent["id"]:
                raise ValueError("Only this lead agent's own subagents can be controlled")
            if name == "agent_message":
                return self.controller.enqueue(child, "message_" + identity, args["message"], mode="steer")
            return self.controller.control(cid, {"agent_id": child["id"], "action": "stop", "expected_control_revision": child["control_revision"]}, identity)
        if name == "command_schema":
            return self.workspace.commands.describe()[args["operation"]]
        if name == "campaign_command":
            campaign = self.store.get(cid, "campaign")
            command_id = "pi_command_" + content_hash([cid, args["operation"], args["request_key"]])[:32]
            try:
                accepted = self.store.get(command_id, "work_command")
                if accepted["request"]["operation"] != args["operation"] or accepted["request"]["payload"] != args["payload"]:
                    raise ValueError("Request key belongs to a different command")
                return accepted
            except KeyError:
                pass
            if args["operation"] in {"implementation.commission", "evaluator.commission", "implementation.revalidate"}:
                execution = implementation_execution()
                if not execution["available"]:
                    raise ValueError("Isolated implementation execution is unavailable; no grant was reserved. " + execution["reason"])
            command = Command(id=command_id, campaign_id=cid,
                operation=args["operation"], expected_revision=campaign["version"], expected_guidance_revision=args["guidance_revision"],
                expected_authority_hash=self.workspace.commands.authority_hash(campaign), payload=args["payload"])
            return self.workspace.commands.execute(command, actor="manager")
        if name == "researcher_ask":
            return self.store.put("agent_question", {"id": "question_" + identity, "campaign_id": cid, "agent_id": agent["id"],
                "status": "pending", "created_at": now(), **args}, "agent.question_created")
        if name == "yield_work":
            for ref in args.get("evidence_ids", []):
                self.record(agent, ref)
            if agent["role"] == LEAD and args["disposition"] == "complete":
                active = [a for a in self.store.list("agent_session", cid) if a["parent_agent_id"] == agent["id"] and a["status"] in {"queued", "running", "paused"}]
                if active or any(t["status"] in {"queued", "running", "pausing"} for t in self.store.list("trial", cid)):
                    raise ValueError("Outstanding assignments or experiments remain; wait or reconcile them before completing")
                if any(g["status"] in {"reserved", "queued", "submitted", "running", "building", "validating", "reviewing", "repairing", "stopping"} for g in self.store.list("implementation_grant", cid)) or any(q["status"] == "pending" for q in self.store.list("agent_question", cid)) or any(j["status"] in {"queued", "running"} for j in self.store.list("fixed_mask_job", cid)):
                    raise ValueError("Unresolved questions or numerical/implementation jobs remain")
            current = self.store.get(agent["id"], "agent_session")
            current["disposition"] = args; self.store.put("agent_session", current, "agent.checkpointed")
            current_run = self.store.get(run["id"], "agent_run")
            current_run["disposition"] = args
            self.store.put("agent_run", current_run)
            return {"saved": True, **args}
        if name == "submit_output":
            import jsonschema
            jsonschema.validate(args["result"], agent["output_schema"])
            current = self.store.get(agent["id"], "agent_session")
            current["output"] = args["result"]; self.store.put("agent_session", current, "agent.output_saved")
            saved_run = self.store.get(run["id"], "agent_run")
            saved_run["output"] = args["result"]; self.store.put("agent_run", saved_run)
            return {"saved": True}
        if name.startswith("workspace_"):
            return self._files(agent, name, args)
        raise ValueError("Unknown tool")

    def _files(self, agent, name, args):
        directory = self.workspace.directory / "agents" / agent["id"] / "work"
        directory.mkdir(parents=True, exist_ok=True)
        if name == "workspace_list":
            return {"files": [str(p.relative_to(directory)) for p in directory.rglob("*") if p.is_file() and not p.is_symlink()][:500]}
        relative = Path(args["path"])
        filename = directory / relative
        if relative.is_absolute() or ".." in relative.parts or directory.resolve() not in filename.resolve().parents:
            raise ValueError("File must stay inside this assignment's development directory")
        if any(p.is_symlink() for p in [filename, *filename.parents] if p != directory.parent):
            raise ValueError("Symlinks are not permitted in development paths")
        if name == "workspace_read":
            if filename.stat().st_size > 262144:
                raise ValueError("File too large; split the work product")
            return {"path": args["path"], "content": filename.read_text()}
        if name == "workspace_write":
            if len(args["content"].encode()) > 262144:
                raise ValueError("File exceeds package source limit")
            filename.parent.mkdir(parents=True, exist_ok=True); filename.write_text(args["content"])
            return {"path": args["path"], "saved": True}
        seconds = min(args.get("seconds", 10), max(0, agent.get("deadline_at", time.time() + 30) - time.time()))
        if seconds <= 0:
            raise ValueError("Implementation grant expired")
        import shutil
        if not shutil.which("bwrap"):
            raise ValueError("Isolated development execution requires bubblewrap")
        command = ["bwrap", "--die-with-parent", "--unshare-all", "--new-session", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
        for root in dict.fromkeys(["/usr", "/lib", "/lib64", "/bin", sys.base_prefix, sys.prefix]):
            if Path(root).exists():
                command += ["--ro-bind", root, root]
        wrapper = "import resource,runpy,sys; resource.setrlimit(resource.RLIMIT_FSIZE,(1048576,1048576)); resource.setrlimit(resource.RLIMIT_NOFILE,(128,128)); runpy.run_path(sys.argv[1],run_name='__main__')"
        command += ["--bind", str(directory), "/work", "--chdir", "/work", sys.executable, "-c", wrapper, str(relative)]
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            child = subprocess.Popen(command, stdout=stdout, stderr=stderr, start_new_session=True,
                env={"PATH": "/usr/bin:/bin", "HOME": "/tmp", "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"})
            try:
                child.wait(timeout=seconds)
            except subprocess.TimeoutExpired:
                import signal
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait(timeout=5)
                return {"status": "timeout", "seconds": seconds}
            output = {}
            for key, stream in (("stdout", stdout), ("stderr", stderr)):
                stream.seek(max(0, stream.seek(0, 2) - 24000))
                output[key] = stream.read(24000).decode(errors="replace")
        return {"exit_code": child.returncode, **output, "scope": "Development check; protected validation is separate."}
