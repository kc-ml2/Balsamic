"""Campaign guidance: the researcher's durable direction, its revision counters, and operational issues.

Replaces the legacy campaign memory (editable context document, projection journal and retrieval index), which served
the removed campaign manager. What stays is what the lead agent and the command bus need: the guidance text with its
revision, the campaign-wide ``guidance_revision`` that stales an agent's prepared command, the issue inbox, and the
visibility projection that keeps protected (held-out) evidence out of agent context.
"""
from __future__ import annotations

from optimization_framework.implementations.models import digest
from optimization_framework.storage.sqlite import identifier, now


GUIDANCE_LIMIT = 48 * 1024


class CampaignGuidance:
    def __init__(self, workspace):
        self.workspace = workspace
        self.store = workspace.store

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

    def bump(self, campaign_id, event="manager.guidance_changed"):
        """One more researcher direction: every command an agent prepared before it must be prepared again."""
        with self.workspace.lock:
            state = self.state(campaign_id)
            state["guidance_revision"] += 1
            self.store.put("manager_state", state, event)
            return state

    def edit(self, campaign_id, content, expected_revision, reason="Researcher edited campaign guidance"):
        if not content.strip() or len(content.encode()) > GUIDANCE_LIMIT:
            raise ValueError("Campaign guidance must contain text and fit within 48 KiB")
        with self.workspace.lock:
            state = self.state(campaign_id)
            if state["revision"] != expected_revision:
                raise ValueError("Campaign guidance changed; inspect the latest revision before saving")
            state.update(guidance=content, revision=state["revision"] + 1, guidance_revision=state["guidance_revision"] + 1)
            note = {"id": identifier("memory_note"), "campaign_id": campaign_id,
                "kind": "researcher_guidance", "content": content, "author": "researcher", "reason": reason,
                "guidance_revision": state["guidance_revision"], "created_at": now()}
            self.store.put_many([("manager_state", state, None), ("manager_note", note, "manager.guidance_changed")])
            return state

    def issue(self, campaign_id, code, message, *, affected=None, options=None, evidence=None, reopen=False, actions=None):
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
            issue.update(code=code, message=message, affected=affected, evidence=evidence or [], actions=actions or [],
                         options=options or ["Discuss with the lead", "Defer"], status="pending",
                         occurrences=issue["occurrences"]+1, revision=revision+1, updated_at=now())
            self.store.put("manager_issue", issue, "manager.issue")
            self.store.put("message", {"id": "message_" + identity + "_" + str(issue["occurrences"]),
                "campaign_id": campaign_id, "role": "assistant", "origin": "lab_event",
                "content": message, "issue_id": identity, "created_at": now()}, "message.created")
            return issue

    def resolve_issue(self, issue_id, choice, comment, *, expected_revision=None, action=None):
        """resolved and deferred only mark the issue; an action the issue offers is carried out (Workspace.act_on_issue)."""
        with self.workspace.lock:
            issue = self.store.get(issue_id, "manager_issue")
            if choice not in {"resolved", "deferred", "action"}:
                raise ValueError("Choose resolved, deferred or one of the issue's actions; use the lead's conversation for further direction")
            revision = issue.get("revision", issue.get("occurrences", 1))
            if expected_revision is not None and revision != expected_revision:
                raise ValueError("This issue changed; review its current state before resolving it")
            taken = None
            if choice == "action":
                offered = {item["id"]: item for item in issue.get("actions") or []}
                if action not in offered:
                    raise ValueError("Choose one of this issue's actions")
                taken = {**offered[action], "trial_ids": self.workspace.act_on_issue(issue, action)}
                choice = "resolved"
                comment = (comment + "\n" if comment else "") + f"Action: {offered[action]['label']} ({', '.join(taken['trial_ids'])})"
            issue.update(status=choice, comment=comment, resolved_at=now(), revision=revision+1, action=taken)
            state = self.state(issue["campaign_id"])
            state["guidance_revision"] += 1
            self.store.put("manager_state", state)
            self.store.put("manager_issue", issue, "manager.issue_resolved")
            self.store.put("manager_note", {"id": identifier("memory_note"), "campaign_id": issue["campaign_id"],
                "kind": "decision", "content": f"{issue['message']}\nResearcher choice: {choice}\n{comment}",
                "source_ids": [issue_id], "author": "researcher", "created_at": now()}, "manager.note")
            return issue

    def visible_records(self, campaign_id):
        """(kind, record) pairs an agent may read: held-out evidence and what refers to it stay out until released."""
        return self._records(campaign_id)

    def _records(self, campaign_id):
        kinds = ("campaign", "task", "hypothesis", "trial", "decision", "message", "source", "source_retrieval", "manager_note", "manager_issue", "implementation_grant",
                 "study", "nomination", "finalist_selection", "finalist_confirmation_binding", "budget_amendment", "campaign_budget_amendment", "validation_requirement", "validation_result", "waiver", "waiver_revocation",
                 "confirmation_protocol", "confirmation_allocation_binding", "confirmation_release", "confirmation_report", "execution_attempt", "diagnostic_grant", "reuse_decision", "asset", "work_command", "command_rejection", "outbox",
                 "experiment_draft", "draft_launch", "reproduction_comparison", "study_execution", "study_activation", "confirmation_design",
                 "protocol_cell", "method_binding", "cell_launch", "execution_grant", "execution_grant_release",
                 "evaluator_requirement", "evaluator_binding", "source_capture", "thread", "problem_note", "problem_note_revision", "intervention")
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

