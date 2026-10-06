"""Decision freshness is a projection, never a renewal of execution authority."""
from copy import deepcopy


def refresh_operations(workspace, identity):
    """Full schemas for this review's historical actions and scientific follow-up."""
    record = workspace.store.get(identity, "decision_refresh")
    operations = {"campaign.update", "research.start", "study.create", "trial.create", "trial.control",
        "draft.save", "draft.launch", "finding.record", "comparison.report", "hypothesis.review",
        "validation.run", "implementation.commission", "implementation.attach", "literature.search", "source.ingest"}
    operations.update(item["action"]["command_operation"] for item in record["decisions"]
        if item.get("action") and item["action"].get("command_operation"))
    return operations


def _get(workspace, identity, kind):
    try:
        return workspace.store.get(identity, kind) if identity else None
    except KeyError:
        return None


def public_decision(workspace, record):
    from .decision_presentation import present_decision
    campaign = workspace.store.get(record["campaign_id"], "campaign")
    current_guidance = workspace.memory.state(record["campaign_id"])["guidance_revision"]
    action = _get(workspace, record.get("action_id"), "action")
    presentation = present_decision(record, action)
    source_guidance = (action or {}).get("guidance_revision", record.get("guidance_revision"))
    if source_guidance is None and (record.get("action_id") or record.get("incremental_solver_calls") is not None):
        source_guidance = (_get(workspace, record.get("research_run_id"), "research_run") or {}).get("guidance_revision")
    source_charter = record.get("charter_version")
    executable = (["accept"] if record.get("action_id") else
        ["0"] if record.get("trial_id") and record.get("incremental_solver_calls") is not None else [])
    stale = bool(executable) and (source_charter != campaign["version"] or
        (action or {}).get("charter_version", source_charter) != campaign["version"] or
        source_guidance is not None and source_guidance != current_guidance)
    pending = record.get("status") == "pending"
    freshness = {"state": "stale" if stale else "ready", "stale": stale,
        "can_accept": pending and not stale, "blocked_choice_ids": executable if stale else [],
        "can_refresh": pending and stale, "reason": "",
        "current_charter_version": campaign["version"], "proposal_charter_version": source_charter,
        "current_guidance_revision": current_guidance, "proposal_guidance_revision": source_guidance}
    if stale:
        freshness["reason"] = ("This proposal uses an older charter or earlier researcher guidance. "
            "Ask the manager to reconsider it before approving execution; defer or decline remains available.")
    if action and action.get("kind") == "probe":
        from optimization_framework.research.action_policy import probe_execution_issue
        design_issue = probe_execution_issue(action, autonomous=False)
        if design_issue:
            freshness.update(state="blocked", can_accept=False, blocked_choice_ids=["accept"], can_refresh=pending,
                reason=(freshness["reason"] + " " if stale else "") + design_issue)
    if presentation["needs_clarification"]:
        blocked = [option["id"] for option in presentation["options"] if option["id"] not in {"defer", "reject"}]
        clarification = ("This is an internal question for the campaign manager. Send it to the manager for review."
            if presentation["audience"] == "manager" else
            "The saved request does not define a clear proposal and distinct choices. Ask the manager to clarify them before deciding.")
        freshness.update(state="blocked", can_accept=False, can_refresh=pending,
            blocked_choice_ids=list(dict.fromkeys([*freshness["blocked_choice_ids"], *blocked])),
            reason=(freshness["reason"] + " " if freshness["reason"] else "") + clarification)
    refresh = _get(workspace, record.get("refresh_id"), "decision_refresh")
    if refresh:
        command = _get(workspace, refresh["manager_command_id"], "manager_command") or {}
        run = _get(workspace, command.get("research_run_id"), "research_run")
        freshness.update(refresh_command_id=refresh["command_id"], manager_command_id=refresh["manager_command_id"],
            refresh_id=refresh["id"], review_completed=False)
        if run:
            freshness["research_run_id"] = run["id"]
            if run.get("decision_review"):
                from optimization_framework.research.decision_review import progress
                freshness["review_progress"] = progress(workspace, run)
            result = run.get("result") or {}
            current = run.get("charter_version") == campaign["version"] and run.get("guidance_revision") == current_guidance
            substantive = bool(result.get("actions") or result.get("decisions") or
                any(message.get("content", "").strip() for message in result.get("messages", [])))
            completed = (run.get("status") in {"completed", "awaiting_researcher"} and current and substantive and
                bool(run.get("finalized_result_id")) and not run.get("error") and
                any(step.get("role") == "research_synthesizer" and step.get("status") == "completed"
                    for step in result.get("trace", [])) and
                not result.get("stale_charter") and not (run.get("usage") or {}).get("pending_reservation"))
            freshness["review_decision_ids"] = [item["id"] for item in workspace.store.list("decision", record["campaign_id"])
                if item.get("research_run_id") == run["id"]]
            if run["status"] in {"running", "stopping", "needs_reconciliation"}:
                freshness.update(state="updating", can_refresh=False, can_accept=False,
                    reason="The campaign manager is reconsidering this decision. This request is not approval to execute it.")
            elif completed and pending:
                freshness.update(state="reviewed", can_refresh=False, can_accept=False, review_completed=True,
                    reason="A current manager review is available. Review its response and new decisions; the original recommendation remains unapproved.")
            elif pending and freshness["can_refresh"]:
                freshness.update(state="blocked", reason=run.get("error") or
                    "The previous review did not produce a current completed response. Request reconsideration again.")
        elif command.get("status") in {"queued", "waiting_provider"}:
            if refresh.get("review_protocol") == "parallel_v1":
                freshness["review_progress"] = {"phase": "queued", "total": 0, "completed": 0,
                    "running": 0, "failed": 0, "tasks": [], "max_parallel_reviews": refresh.get("max_parallel_reviews", 3),
                    "can_retry": False, "retry_reason": "The campaign manager has not dispatched this review yet."}
            freshness.update(state="updating", can_refresh=False, can_accept=False,
                reason="Reconsideration is queued with the campaign manager and will use current guidance when it starts.")
        elif pending and freshness["can_refresh"]:
            freshness.update(state="blocked", reason=command.get("error") or "Reconsideration needs another manager turn.")
    return {**deepcopy(record), "freshness": freshness, "presentation": presentation}


def refresh_context(workspace, identity, campaign_id):
    """Supply explicitly selected provenance even if ordinary retrieval omits it."""
    record = workspace.store.get(identity, "decision_refresh")
    if record["campaign_id"] != campaign_id:
        raise ValueError("Decision reconsideration belongs to another campaign")
    visible = {item["id"] for _, item in workspace.memory._records(campaign_id)}
    if any(item["decision"]["id"] not in visible or item.get("action") and item["action"]["id"] not in visible
            for item in record["decisions"]):
        raise ValueError("A selected decision or action is now protected; reconsideration cannot disclose it")
    context = deepcopy(record)
    # A refresh request is procedural. Preserve the latest actual researcher
    # request even when bounded retrieval removes discussion history; otherwise
    # reconsideration can drift back toward the older selected recommendations.
    requests = [item for item in workspace.store.list("manager_command", campaign_id)
        if item["id"] in visible and not item.get("automatic") and not item.get("decision_refresh_id")]
    if requests:
        source = max(requests, key=lambda item: (item.get("created_at", ""), item["id"]))
        receipt = _get(workspace, source["id"].removeprefix("turn_"), "work_command") or {}
        source_run = _get(workspace, source.get("research_run_id"), "research_run") or {}
        context["continuing_researcher_request"] = {
            "message": source["request"]["message"], "manager_command_id": source["id"],
            "message_id": "message_" + source["id"], "created_at": source.get("created_at"),
            "source_charter_version": receipt.get("request", {}).get("expected_revision", source_run.get("charter_version")),
            "source_guidance_revision": source.get("admitted_guidance_revision"),
            "source_authority_hash": receipt.get("authority_hash"), "source_status": source["status"],
            "basis": "Exact latest non-automatic researcher request, excluding decision-refresh requests. "
                "Use it for scientific continuity under CURRENT authority; it does not approve historical actions or grant new resources."}
    context["instructions"] = (
        "Reconsider each selected historical decision against CURRENT charter, guidance, completed evidence and accepted work. "
        "Original decision/action snapshots are historical proposals, not current authority. A desired choice expresses interest, "
        "not approval. Explain which recommendations remain useful, need changes, duplicate completed work, or should be declined; "
        "identify source decision IDs in the response. Supply any new executable recommendation as a complete typed action requiring "
        "researcher review. Do not execute actions, change budgets, or assume old permissions. This batch review does not resolve "
        "the original decisions. Preserve the user's exact comments and scientific intent. "
        "Resolve internal manager questions from available evidence instead of sending them back to the researcher. "
        "If researcher judgment is still needed, the final manager must use decision_requests with a short title, background, "
        "explicit proposal, and concrete option labels describing the choice and its consequences. Never invent a yes/no "
        "meaning for historical 'Follow the proposed direction' options. Any request for work authorization must use a typed action.")
    for item in context["decisions"]:
        current = workspace.store.get(item["decision"]["id"], "decision")
        item["current_status"] = current["status"]
        item["current_resolution_revision"] = current.get("resolution_revision", 0)
        if current["status"] != "pending":
            item["current_resolution"] = {key: current[key] for key in ("choice", "comment", "outcome") if key in current}
    return context
