"""Versioned, editable campaign context with durable provenance and retrieval."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
import re

from optimization_framework.implementations.models import digest
from optimization_framework.storage.sqlite import identifier, now


CONTEXT_LIMIT = 96 * 1024
GUIDANCE_LIMIT = 48 * 1024


def content_key(revision):
    """Identity of a context revision's content, ignoring its number, cursor and clock."""
    document = revision["document"]
    body = document.split("\n---\n", 1)[1] if document.startswith("---\n") else document
    structured = {key: value for key, value in (revision.get("structured") or {}).items() if key != "revision"}
    return digest([body, structured, revision.get("guidance"), revision.get("source_ids"),
                   revision.get("charter_version"), revision.get("guidance_revision")])


class CampaignMemory:
    def __init__(self, workspace):
        self.workspace = workspace
        self.store = workspace.store
        with self.store.connection() as db:
            db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS manager_search USING fts5(campaign_id UNINDEXED, record_id UNINDEXED, body)")

    def state(self, campaign_id):
        identity = "manager_" + campaign_id
        try:
            return self.store.get(identity, "manager_state")
        except KeyError:
            campaign = self.store.get(campaign_id, "campaign")
            return self.store.put("manager_state", {"id": identity, "campaign_id": campaign_id,
                "revision": 0, "guidance_revision": 0, "event_cursor": 0, "context_id": None,
                "guidance": "## Research direction\n\n" + campaign["objective"] +
                    "\n\n## Constraints and preferences\n\nRecord durable instructions here.\n\n## Open questions\n\n", "created_at": now()})

    def edit(self, campaign_id, content, expected_revision, reason="Researcher edited campaign memory"):
        if not content.strip() or len(content.encode()) > GUIDANCE_LIMIT:
            raise ValueError("Campaign guidance must contain text and fit within 48 KiB")
        with self.workspace.lock:
            state = self.state(campaign_id)
            if state["revision"] != expected_revision:
                raise ValueError("Campaign memory changed; inspect the latest revision before saving")
            state.update(guidance=content, guidance_revision=state["guidance_revision"] + 1)
            note = {"id": identifier("memory_note"), "campaign_id": campaign_id,
                "kind": "researcher_guidance", "content": content, "author": "researcher", "reason": reason,
                "guidance_revision": state["guidance_revision"], "created_at": now()}
            self.store.put_many([("manager_state", state, None), ("manager_note", note, "manager.guidance_changed")])
            return self.sync(campaign_id, force=True)

    def issue(self, campaign_id, code, message, *, affected=None, options=None, evidence=None, reopen=False):
        self.store.get(campaign_id, "campaign")
        identity = "issue_" + digest([campaign_id, code, affected])[:24]
        with self.workspace.lock:
            try:
                issue = self.store.get(identity, "manager_issue")
                # Unchanged polling failures do not generate repeated messages/events.
                if issue["message"] == message and (issue["status"] == "pending" or not reopen):
                    return issue
            except KeyError:
                issue = {"id": identity, "campaign_id": campaign_id, "created_at": now(), "occurrences": 0}
            revision = issue.get("revision", issue["occurrences"])
            issue.pop("resolved_at", None)
            issue.pop("resolution_basis", None)
            issue.update(code=code, message=message, affected=affected, evidence=evidence or [],
                         options=options or ["Discuss with manager", "Defer"], status="pending",
                         occurrences=issue["occurrences"]+1, revision=revision+1, updated_at=now())
            self.store.put("manager_issue", issue, "manager.issue")
            self.store.put("message", {"id": "message_" + identity + "_" + str(issue["occurrences"]),
                "campaign_id": campaign_id, "role": "assistant", "origin": "manager_system",
                "content": message, "issue_id": identity, "created_at": now()}, "message.created")
            return issue

    def resolve_issue(self, issue_id, choice, comment, *, expected_revision=None):
        with self.workspace.lock:
            issue = self.store.get(issue_id, "manager_issue")
            if choice not in {"resolved", "deferred"}:
                raise ValueError("Choose resolved or deferred; use the manager conversation for further direction")
            revision = issue.get("revision", issue.get("occurrences", 1))
            if expected_revision is not None and revision != expected_revision:
                raise ValueError("This manager issue changed; review its current state before resolving it")
            issue.update(status=choice, comment=comment, resolved_at=now(), revision=revision+1)
            state = self.state(issue["campaign_id"])
            state["guidance_revision"] += 1
            self.store.put("manager_state", state)
            self.store.put("manager_issue", issue, "manager.issue_resolved")
            self.store.put("manager_note", {"id": identifier("memory_note"), "campaign_id": issue["campaign_id"],
                "kind": "decision", "content": f"{issue['message']}\nResearcher choice: {choice}\n{comment}",
                "source_ids": [issue_id], "author": "researcher", "created_at": now()}, "manager.note")
            return issue

    def _records(self, campaign_id):
        kinds = ("campaign", "task", "hypothesis", "trial", "decision", "decision_refresh", "action", "message", "source", "source_retrieval", "manager_note", "manager_issue", "manager_input", "manager_command", "implementation_grant",
                 "study", "nomination", "finalist_selection", "finalist_confirmation_binding", "budget_amendment", "campaign_budget_amendment", "validation_requirement", "validation_result", "waiver", "waiver_revocation",
                 "confirmation_protocol", "confirmation_allocation_binding", "confirmation_release", "confirmation_report", "execution_attempt", "diagnostic_grant", "reuse_decision", "asset", "work_command", "command_rejection", "outbox",
                 "experiment_draft", "draft_launch", "reproduction_comparison", "study_execution", "study_activation", "confirmation_design",
                 "protocol_cell", "method_binding", "cell_launch", "execution_grant", "execution_grant_release",
                 "evaluator_requirement", "evaluator_binding", "discovery_session", "discovery_task",
                 "discovery_artifact", "discovery_candidate", "discovery_handoff", "discovery_wrap_up", "methodology_family", "source_capture", "discovery_assessment", "discovery_assessment_decision")
        records = []
        for kind in kinds:
            values = [self.store.get(campaign_id, "campaign")] if kind == "campaign" else self.store.list(kind, campaign_id)
            if kind == "task":
                values = [{**self.workspace.evaluators.task_view(item), "evaluator_readiness": self.workspace.evaluators.readiness(item)} for item in values]
            records.extend((kind, item) for item in values)
        evidence_ids = {item["validation_evidence_id"] for kind, item in records if kind == "evaluator_binding" and item.get("validation_evidence_id")}
        evidence_ids.update(identity for kind, item in records if kind == "validation_result"
            for identity in item.get("evidence_ids", []) if identity.startswith("evaluator_evidence_"))
        records.extend(("evaluator_evidence", self.store.get(identity, "evaluator_evidence")) for identity in sorted(evidence_ids))
        releases = self.store.list("confirmation_release", campaign_id)
        released = {identity for release in releases for key in ("trial_ids", "task_ids") for identity in release[key]}
        released_cohorts = {row["protocol_id"] for row in releases}
        locked = {item["id"] for _, item in records if item.get("locked") or (
            item.get("split", item.get("task_split")) in {"test", "confirmation", "heldout"} and item["id"] not in released) or
            item.get("protected_cohort_id") and item["protected_cohort_id"] not in released_cohorts}
        known_ids = {item["id"] for _, item in records}

        def references(value):
            if isinstance(value, str):
                return {value} if value in known_ids else set()
            if isinstance(value, dict):
                result = set()
                for key, item in value.items():
                    if key == "id":
                        continue
                    # Trial masks are numerical payloads, not record links.
                    # Inspect the shape only, avoiding millions of scalar
                    # checks when a 2D design appears in progress and archive.
                    if key in {"candidate", "best_candidate", "best_design", "design"} and isinstance(item, list) and item:
                        first = item[0]
                        if isinstance(first, (int, float, bool)) or (isinstance(first, list) and first
                                and isinstance(first[0], (int, float, bool))):
                            continue
                    result.update(references(item))
                return result
            if isinstance(value, list):
                # Worker progress embeds large numerical masks. A numeric row
                # cannot contain a record reference and need not be recursed.
                if value and all(isinstance(item, (int, float, bool)) for item in value):
                    return set()
                result = set()
                for item in value:
                    result.update(references(item))
                return result
            return set()
        edges = [(item["id"], references(item)) for _, item in records]
        changed = True
        while changed:
            changed = False
            for identity, refs in edges:
                if identity not in locked and refs & locked:
                    locked.add(identity)
                    changed = True
        visible = [(kind, item) for kind, item in records if item["id"] not in locked]
        for kind, item in records:
            if kind == "trial" and item.get("protected_cohort_id") and item["id"] in locked:
                visible.append(("protected_work_status", {"id": "status_"+item["id"], "campaign_id": campaign_id,
                    "status": item["status"], "study_execution_id": item.get("study_execution_id"),
                    "protected_evidence": True, "message": "Predeclared cohort work; results withheld until evidence release"}))
        # Operational failures still reach the manager while heldout numerical
        # evidence stays protected. Never copy arbitrary worker error text here.
        hidden_issues = {item["id"] for kind, item in records if kind == "manager_issue" and item["id"] in locked}
        for kind, item in records:
            if kind == "manager_issue" and item["id"] in hidden_issues:
                visible.append((kind, {
                    "id": item["id"], "campaign_id": campaign_id, "code": "heldout_operational_issue",
                    "affected": item.get("affected"), "status": item["status"], "created_at": item["created_at"],
                    "message": "An operational issue affects heldout work. The manager must resolve execution or authority without inspecting protected measurements.",
                    "evidence": [], "protected_evidence": True}))
            elif kind == "message" and item.get("issue_id") in hidden_issues:
                visible.append((kind, {"id": item["id"], "campaign_id": campaign_id, "role": item["role"],
                    "issue_id": item["issue_id"], "origin": "manager_system", "created_at": item["created_at"],
                    "content": "An issue in heldout work requires the campaign manager's attention. Numerical evidence remains protected."}))
        return visible

    @staticmethod
    def _text(kind, item):
        if kind == "confirmation_allocation_binding":
            return json.dumps({"kind": kind, **{key: item.get(key) for key in
                ("id", "campaign_id", "study_id", "protocol_id", "content_hash")},
                "methods": {identity: {key: value.get(key) for key in
                    ("source_trial_id", "source_procedure_id", "source_control_revision", "allocation_overrides")}
                    for identity, value in item["methods"].items()}}, ensure_ascii=False)
        if kind == "finalist_selection":
            from optimization_framework.analysis.finalists import summary
            return json.dumps({"kind": kind, **summary(item)}, ensure_ascii=False)
        if kind == "decision_refresh":
            return json.dumps({"kind": kind, **{key: item.get(key) for key in
                ("id", "manager_command_id", "requested_charter_version", "requested_guidance_revision", "comment")},
                "decisions": [{"decision_id": row["decision"]["id"],
                    "action_id": (row.get("action") or {}).get("id"),
                    "desired_choice": row.get("desired_choice"), "comment": row.get("comment", "")}
                    for row in item["decisions"]]}, ensure_ascii=False)
        if kind == "work_command":
            # Historical reply snapshots are for caller reconciliation. Manager
            # retrieval reads scientific evidence through its own visible records.
            return json.dumps({"kind": kind, "id": item["id"], "operation": item["request"]["operation"],
                "actor": item["actor"], "status": item["status"],
                "outcome": {key: value for key, value in item["outcome"].items()
                    if key not in {"trial", "campaign", "issue", "hypothesis", "source"}}}, ensure_ascii=False)
        if kind == "asset":
            # Listing applicability is not an implicit decision to read a past
            # geometry, policy, dataset, or finding into the manager's context.
            return json.dumps({"kind": kind, **{key: item.get(key) for key in (
                "id", "title", "kind", "producer_id", "applicability", "cost_provenance", "exposure_status", "availability")}}, ensure_ascii=False)
        if kind == "trial":
            result = item.get("result") or item.get("progress") or {}
            return json.dumps({"id": item["id"], "kind": kind, "algorithm": item["algorithm"], "task_id": item["task_id"],
                "status": item["status"], "question": item.get("question"), "best_efficiency": result.get("best_efficiency"),
                "best_objective": result.get("best_objective"), "objective": (item.get("problem") or {}).get("primary_objective"),
                "scientific_complete": result.get("scientific_complete"), "study_id": item.get("study_id"),
                "evaluations": result.get("evaluations"), "implementation_version_id": item.get("implementation_version_id"),
                "evaluator_version_id": item.get("evaluator_version_id"), "evaluator_eligibility": item.get("evaluator_eligibility"),
                "claim_level": "exploratory_unverified_evaluator" if (item.get("evaluator_eligibility") or {}).get("basis") == "waiver" else "measured_screening",
                "reason": item.get("reason")}, ensure_ascii=False)
        if kind == "discovery_task":
            return json.dumps({"kind": kind, **{key: item.get(key) for key in
                ("id", "session_id", "brief", "status", "wait_reason", "artifact_ids", "error", "handoff_id", "handoff_reason")},
                "summary": item.get("result", {}).get("summary")}, ensure_ascii=False)
        if kind in {"discovery_handoff", "discovery_wrap_up"}:
            return json.dumps({"kind": kind, **{key: item.get(key) for key in
                ("id", "session_id", "task_id", "reason", "summary", "scientific_complete", "artifact_ids", "next_action")}}, ensure_ascii=False)
        selected = {key: value for key, value in item.items() if key not in {
            "source", "algorithm_config", "context_snapshot", "checkpoint", "request", "provider_snapshot", "result"}}
        return json.dumps({"kind": kind, **selected}, ensure_ascii=False)

    def sync(self, campaign_id, *, force=False):
        with self.workspace.lock:
            state = self.state(campaign_id)
            with self.store.connection() as db:
                cursor = db.execute("SELECT COALESCE(MAX(id),0) FROM events WHERE campaign_id=?", (campaign_id,)).fetchone()[0]
            if not force and state["context_id"] and state["event_cursor"] == cursor:
                revision = self.store.get(state["context_id"], "context_revision")
                if "structured" not in revision:
                    return self.sync(campaign_id, force=True)
                if not self.store.in_transaction and not (self.workspace.directory / "campaigns" / campaign_id / "manager" / "context.md").exists():
                    self._project(campaign_id, revision, self._records(campaign_id))
                return revision
            records = self._records(campaign_id)
            with self.store.connection() as db:
                db.execute("DELETE FROM manager_search WHERE campaign_id=?", (campaign_id,))
                db.executemany("INSERT INTO manager_search(campaign_id,record_id,body) VALUES (?,?,?)",
                               [(campaign_id, item["id"], self._text(kind, item)) for kind, item in records])
            campaign = self.store.get(campaign_id, "campaign")
            from .context import assemble
            structured = assemble(self.workspace, campaign, state, records).model_dump(mode="json")
            issues = [item for kind, item in records if kind == "manager_issue" and item["status"] == "pending"]
            decisions = [item for kind, item in records if kind == "decision" and item.get("status") == "pending"]
            work = [item for kind, item in records if kind == "trial" and item["status"] in {"queued", "running", "paused", "pausing"}]
            notes = [item for kind, item in records if kind == "manager_note" and item.get("kind") != "researcher_guidance"][-20:]
            metadata = {"campaign_id": campaign_id, "revision": state["revision"]+1, "charter_version": campaign["version"],
                        "guidance_revision": state["guidance_revision"], "event_cursor": cursor, "updated_at": now()}
            document = "---\n" + json.dumps(metadata, ensure_ascii=False, indent=2) + "\n---\n\n# Campaign context\n\n"
            document += "## Authoritative objective\n\n" + campaign["objective"] + "\n\n## Researcher guidance\n\n" + state["guidance"]
            if campaign.get("active_study_id"):
                study = self.store.get(campaign["active_study_id"], "study")
                document += "\n\n## Active frozen study\n\n```json\n" + json.dumps(study, indent=2) + "\n```"
            domain = [{"task_id": item["id"], "problem": item.get("problem")} for kind, item in records if kind == "task" and not item.get("archived")]
            document += "\n\n## Declared problems and capabilities\n\n```json\n" + json.dumps(domain, indent=2) + "\n```"
            authority = {key: campaign.get(key) for key in ("autonomy", "compute_budget_seconds", "llm_budget_usd", "implementation_compute_budget_seconds", "delegated_trial_seconds")}
            document += "\n\n## Authority and resource envelopes\n\n```json\n" + json.dumps(authority, indent=2) + "\n```"
            document += "\n\n## Pending issues and decisions\n\n" + "\n".join(f"- [{i['id']}] {i.get('message', i.get('title', ''))}" for i in issues+decisions)
            document += "\n\n## Active experiments\n\n" + "\n".join(f"- [{i['id']}] {i['algorithm']}: {i['status']}" for i in work)
            document += "\n\n## Recent recorded findings and decisions\n\n" + "\n".join(f"- [{i['id']}] {i['content']}" for i in notes)
            revision = {"id": identifier("context"), **metadata, "guidance": state["guidance"], "document": document,
                        "structured": structured,
                        "document_hash": digest(document), "source_ids": [item["id"] for _, item in records]}
            current = self.store.get(state["context_id"], "context_revision") if state["context_id"] else None
            if current and "structured" in current and content_key(current) == content_key(revision):
                # Most events (progress, costs, logs) do not change the context;
                # advance the cursor instead of storing an identical snapshot.
                state.update(event_cursor=cursor)
                self.store.put("manager_state", state)
                if not self.store.in_transaction and not (self.workspace.directory / "campaigns" / campaign_id / "manager" / "context.md").exists():
                    self._project(campaign_id, current, records)
                return current
            state.update(revision=metadata["revision"], context_id=revision["id"], event_cursor=cursor)
            self.store.put_many([("context_revision", revision, None), ("manager_state", state, None)])
            if not self.store.in_transaction:
                self._project(campaign_id, revision, records)
            return revision

    def _project(self, campaign_id, revision, records):
        """SQLite commits are canonical; interrupted text projections are rebuildable."""
        root = self.workspace.directory / "campaigns" / campaign_id / "manager"
        snapshots = root / "revisions"
        snapshots.mkdir(parents=True, exist_ok=True)
        path = snapshots / f"{revision['revision']:08d}.md"
        self._atomic_text(path, revision["document"])
        self._atomic_text(root / "context.md", revision["document"])
        if revision.get("structured"):
            self._atomic_text(root / "context.json", json.dumps(revision["structured"], ensure_ascii=False, indent=2) + "\n")
            self._atomic_text(snapshots / f"{revision['revision']:08d}.json", json.dumps(revision["structured"], ensure_ascii=False, indent=2) + "\n")
        self._atomic_text(root / "records.jsonl", "".join(self._text(kind, item) + "\n" for kind, item in records))
        notes = root / "records"
        notes.mkdir(exist_ok=True)
        for kind, item in records:
            if kind in {"manager_note", "decision", "manager_issue"}:
                body = self._text(kind, item)
                self._atomic_text(notes / f"{item['id']}-{digest(body)[:12]}.md", "```json\n" + body + "\n```\n")
        self._append_journal(root / "journal.jsonl", campaign_id, revision["event_cursor"])

    def _append_journal(self, path: Path, campaign_id: str, through: int):
        """Project only new committed events; repair a partial trailing line."""
        with path.open("a+b") as output:
            output.seek(0, 2)
            end = output.tell()
            if end:
                output.seek(end - 1)
                if output.read(1) != b"\n":
                    position = end - 1
                    while position >= 0:
                        output.seek(position)
                        if output.read(1) == b"\n":
                            break
                        position -= 1
                    end = position + 1
                    output.truncate(end)
                if end:
                    position = end - 2
                    while position >= 0:
                        output.seek(position)
                        if output.read(1) == b"\n":
                            break
                        position -= 1
                    output.seek(position + 1)
                    last = json.loads(output.read(end - position - 2))["id"]
                else:
                    last = 0
            else:
                last = 0
            if last > through:
                raise ValueError("Projected manager journal is ahead of its context revision")
            output.seek(0, 2)
            with self.store.connection() as db:
                rows = db.execute("SELECT id,kind,created_at,data FROM events WHERE campaign_id=? AND id>? AND id<=? ORDER BY id ASC",
                                  (campaign_id, last, through))
                for row in rows:
                    output.write((json.dumps({**dict(row), "data": json.loads(row["data"])}) + "\n").encode())

    @staticmethod
    def _atomic_text(path: Path, content):
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content)
        temporary.replace(path)

    def retrieve(self, campaign_id, question, limit=20):
        words = list(dict.fromkeys(re.findall(r"[\w-]{3,}", question)))[:30]
        with self.store.connection() as db:
            if words:
                query = " OR ".join('"'+word.replace('"', '""')+'"' for word in words)
                rows = db.execute("SELECT record_id,body FROM manager_search WHERE campaign_id=? AND manager_search MATCH ? ORDER BY rank LIMIT ?",
                                  (campaign_id, query, limit)).fetchall()
            else:
                rows = []
        return [{"id": row["record_id"], "text": row["body"]} for row in rows]

    def assemble(self, campaign_id, question):
        revision = self.sync(campaign_id)
        records = self.retrieve(campaign_id, question)
        package = {"revision_id": revision["id"], "revision": revision["revision"],
                   "guidance_revision": revision["guidance_revision"], "event_cursor": revision["event_cursor"],
                   "document": revision["document"], "structured": revision.get("structured"), "retrieved_records": []}
        if len(json.dumps(package).encode()) > CONTEXT_LIMIT:
            # Full typed history remains in the versioned export. The prompt
            # keeps every current constraint and issue, and retrieves history.
            structured = deepcopy(revision["structured"])
            historical = {key: structured.pop(key) for key in ("findings", "counterevidence", "reuse_decisions", "source_ids")}
            structured.update(findings=[], counterevidence=[], reuse_decisions=[], source_ids=[])
            package.update(document="Full context is saved in the versioned context.json and records.jsonl exports. "
                "The structured view below retains all current guidance, authority, resources, active studies, issues and proposed actions.",
                structured=structured, history_counts={key: len(value) for key, value in historical.items()},
                history_selection="Relevant retrieved records and recent findings; historical omission is not resolution or contrary evidence.")
            if len(json.dumps(package).encode()) > CONTEXT_LIMIT:
                raise ValueError("Active campaign context exceeds 96 KiB. Review the context and resolve or scope outstanding guidance; no constraints were discarded.")
            history_limit = (CONTEXT_LIMIT + len(json.dumps(package).encode())) // 2
            for key in ("findings", "counterevidence", "reuse_decisions"):
                for item in reversed(historical[key]):
                    structured[key].insert(0, item)
                    if len(json.dumps(package).encode()) > history_limit:
                        structured[key].pop(0)
                        break
        for record in records:
            candidate = {**package, "retrieved_records": package["retrieved_records"] + [record]}
            if len(json.dumps(candidate).encode()) <= CONTEXT_LIMIT:
                package = candidate
        return package

    def add_notes(self, campaign_id, updates, allowed_ids):
        for update in updates:
            if not update.get("source_ids") or not set(update["source_ids"]) <= set(allowed_ids):
                continue
            identity = "memory_note_" + digest([campaign_id, update])[:24]
            self.store.put("manager_note", {**update, "id": identity, "campaign_id": campaign_id,
                "author": "manager", "created_at": now(), "interpretation": True}, "manager.note")
