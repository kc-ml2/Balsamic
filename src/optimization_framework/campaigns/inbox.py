"""Durable campaign inputs and consumption positions, independent of model calls."""
from optimization_framework.contracts.base import content_hash
from optimization_framework.contracts.experiments import DELIBERATE_STOPS
from optimization_framework.storage.sqlite import now


EVENTS = {"trial.evidence_cataloged", "implementation.updated", "implementation.ready", "implementation.evidence_updated",
    "validation.measured", "validation.waived", "validation.waiver_revoked", "manager.guidance_changed",
    "manager.issue", "manager.issue_resolved", "command.rejected", "draft.saved", "research.stale_result", "research.action_rejected"}


def admit_user(store, command):
    identity = "manager_input_" + command["id"]
    try:
        return store.get(identity, "manager_input")
    except KeyError:
        return store.put("manager_input", {"id": identity, "campaign_id": command["campaign_id"],
            "kind": "manager_request", "manager_command_id": command["id"], "status": "pending",
            "evidence_ids": [], "created_at": command["created_at"]}, "manager.input_received")


def consume_events(workspace, campaign_id):
    """Input creation and cursor advance commit together; routine progress is coalesced."""
    store = workspace.store
    identity = "manager_inbox_cursor_" + campaign_id
    with workspace.lock, store.transaction():
        try:
            cursor = store.get(identity, "manager_inbox_cursor")
        except KeyError:
            cursor = {"id": identity, "campaign_id": campaign_id, "position": 0}
        events = store.events(campaign_id, after=cursor["position"], limit=1000)
        for event in events:
            if event["kind"] not in EVENTS:
                continue
            key = event["data"].get("record_id")
            if not key:
                continue
            entry = store.get_entry(key)
            kind, record = entry["kind"], entry["data"]
            if record.get("campaign_id") != campaign_id:
                continue
            if kind == "trial":
                # A deliberate stop is a direction, and the manager's own stop must not summon it.
                if record["status"] in {"queued", "running", "stopping", "pausing", "paused"} or record.get("stopped_by") in DELIBERATE_STOPS:
                    continue
                revision = [record.get("attempt", 0), "evidence"]
            elif kind == "implementation_grant":
                if record["status"] not in {"completed", "failed", "blocked", "interrupted", "needs_reconciliation", "cancelled", "closed_uncertain"}:
                    continue
                if record["status"] == "completed" and not (record.get("attached") or record.get("evidence_reconciled")):
                    continue  # The ready event follows attachment, which can commit separately.
                revision = [record.get("job_id"), record["status"], record.get("version_id"), len(record.get("attempts", []))]
            elif kind == "manager_issue":
                if event["kind"] == "manager.issue" and record["code"] in {"manager_provider", "manager_run", "manager_request", "model_cost_capture", "delegated_action", "command_delivery"}:
                    continue  # An unavailable/failed reasoner must not summon itself repeatedly.
                revision = record.get("revision", 1)
            else:
                revision = record.get("revision", record.get("content_hash", key))
            semantic_key = [kind, key, revision]
            if kind == "command_rejection":
                # Changing an action UUID does not make the same rejected work
                # new evidence or justify an unlimited sequence of model turns.
                semantic_key = [kind, {field: record["request"].get(field) for field in
                    ("operation", "payload", "expected_revision", "expected_guidance_revision", "expected_authority_hash")}]
            elif kind == "action" and event["kind"] == "research.action_rejected":
                semantic_key = [kind, {field: record.get(field) for field in ("kind", "command_operation", "command_payload",
                    "hypothesis_id", "task_id", "charter_version", "guidance_revision")}]
            input_id = "manager_input_" + content_hash([campaign_id, semantic_key])
            try:
                store.get(input_id, "manager_input")
            except KeyError:
                store.put("manager_input", {"id": input_id, "campaign_id": campaign_id, "kind": event["kind"],
                    "status": "pending", "event_id": event["id"], "evidence_ids": [key],
                    "source_revision": revision, "created_at": event["created_at"]}, "manager.input_received")
        if events:
            cursor["position"] = events[-1]["id"]
            store.put("manager_inbox_cursor", cursor)


def pending(store, campaign_id):
    return [row for row in store.list("manager_input", campaign_id) if row["status"] == "pending"]


def bind(store, inputs, command_id, run_id):
    for item in inputs:
        latest = store.get(item["id"], "manager_input")
        if latest["status"] == "consumed":
            if latest["research_run_id"] != run_id:
                raise ValueError("A campaign input already belongs to another manager turn")
            continue
        latest.update(status="consumed", manager_command_id=command_id, research_run_id=run_id, consumed_at=now())
        store.put("manager_input", latest, "manager.input_consumed")


def check_issues(workspace, command):
    """An issue blocks its affected proposal or executable, not the whole campaign."""
    payload = command.payload
    fields = {"trial_id", "hypothesis_id", "draft_id", "grant_id", "version_id", "implementation_version_id",
              "task_id", "requirement_id", "asset_id", "initial_assets", "dependencies", "assessment_id", "candidate_id"}
    targets = {value for key, values in payload.items() if key in fields
               for value in (values if isinstance(values, list) else [values]) if isinstance(value, str)}
    if command.id.startswith("action_"):
        targets.add(command.id.removeprefix("action_"))
    queue, visited = list(targets), set()
    def nested_targets(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in fields:
                    for reference in item if isinstance(item, list) else [item]:
                        if isinstance(reference, str):
                            yield reference
                elif key in {"procedure", "request", "plan", "cells"}:
                    yield from nested_targets(item)
        elif isinstance(value, list):
            for item in value:
                yield from nested_targets(item)
    targets.update(nested_targets(payload))
    queue = list(targets)
    while queue:
        identity = queue.pop()
        if identity in visited:
            continue
        visited.add(identity)
        try:
            record = workspace.store.get(identity)
        except KeyError:
            continue
        refs = set(nested_targets(record))
        targets.update(refs)
        queue.extend(refs - visited)
    issues = [row for row in workspace.store.list("manager_issue", command.campaign_id)
              if row["status"] == "pending" and row.get("affected") in targets]
    if issues:
        raise ValueError("A pending manager issue affects this action: " + ", ".join(row["id"] for row in issues))
