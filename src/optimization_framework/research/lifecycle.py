"""Durable model results, work delivery, and conservative restart reconciliation."""
from copy import deepcopy

from optimization_framework.storage.sqlite import now


def save_result(workspace, run_id, result):
    """A returned result is committed before deriving messages or allocating work."""
    with workspace.lock, workspace.store.transaction():
        run = workspace.store.get(run_id, "research_run")
        identity = "research_result_" + run_id
        try:
            saved = workspace.store.get(identity, "research_result")
        except KeyError:
            saved = workspace.store.put_immutable("research_result", {"id": identity, "campaign_id": run["campaign_id"],
                "research_run_id": run_id, "result": deepcopy(result), "created_at": now()}, "research.result_received")
        run.update(result_id=identity, usage=saved["result"].get("usage", run.get("usage", {})))
        workspace.store.put("research_run", run)
        return saved


def reconciliation(workspace, run):
    """An in-flight call is never retried based on an absent acknowledgement."""
    subscription = (run.get("usage") or {}).get("billing_mode") == "subscription"
    detail = "Its subscription usage is uncertain and the attempted call is retained" if subscription else "Its conservative cost is retained"
    run.update(status="needs_reconciliation", dispatch_phase="uncertain",
        error=f"A provider request was in flight. {detail}; automatic replay is disabled.")
    identity = "reconcile_" + run["id"]
    try:
        workspace.store.get(identity, "decision")
    except KeyError:
        workspace.store.put("decision", {"id": identity, "campaign_id": run["campaign_id"],
            "charter_version": run["charter_version"], "title": "Resolve an interrupted provider call",
            "context": run["error"], "status": "pending", "created_at": now(), "research_run_id": run["id"],
            "options": [{"id": "close_reserved", "label": "Keep recorded usage and close",
                "description": "Do not replay uncertain work. Start a new discussion with the current provider and allowance."}],
            "recommendation": "close_reserved"}, "decision.created")
    workspace.store.put("research_run", run, "research.interrupted")


def recover(workspace):
    """Only proven unsent work or saved results can recover automatically."""
    from .decision_review import recover as recover_decision_reviews
    recover_decision_reviews(workspace)
    for run in workspace.store.list("research_run"):
        if run.get("decision_review") or run.get("parent_review_run_id"):
            continue
        if run.get("discovery_task_id"):
            continue  # Discovery owns task/step recovery; never replay the legacy graph.
        if run["status"] not in {"running", "stopping"} and not (
                run.get("result_id") and not run.get("finalized_result_id")):
            continue
        with workspace.lock, workspace.store.transaction():
            if run.get("result_id"):
                run.update(dispatch_phase="recovery_pending", stopped_before_recovery=run["status"] == "stopping", status="running")
            elif (run.get("usage") or {}).get("pending_reservation"):
                reconciliation(workspace, run)
                continue
            elif run.get("dispatch_phase") == "queued" or (
                    run.get("dispatch_phase") in {"dispatched", "recovery_pending"} and not run.get("checkpoint") and not run.get("usage", {}).get("calls")):
                if run["status"] == "stopping":
                    run.update(status="stopped", finished_at=now())
                else:
                    run["dispatch_phase"] = "recovery_pending"
            else:
                run.update(status="interrupted", error="Service restarted; completed role evidence and usage are retained. Review before resuming.")
            workspace.store.put("research_run", run, "research.recovered")


def finalize(coordinator, run_id, saved):
    workspace, store = coordinator.workspace, coordinator.store
    with workspace.lock, store.transaction():
        run = store.get(run_id, "research_run")
        if run.get("finalized_result_id") == saved["id"]:
            return run
        result = deepcopy(saved["result"])
        request = run["request"]
        campaign = store.get(run["campaign_id"], "campaign")
        stale = (campaign["version"] != run["charter_version"] or
            workspace.memory.state(run["campaign_id"])["guidance_revision"] != run.get("guidance_revision", 0))
        stopped = run["status"] in {"stopping", "stopped"} or run.get("stopped_before_recovery", False)
        if stale:
            result["stale_charter"] = True
        memory = run["context_snapshot"].get("manager_context", {})
        allowed_ids = [r["id"] for r in memory.get("retrieved_records", [])]
        allowed_ids += [m["id"] for m in run["context_snapshot"].get("history", []) if m.get("id")]
        allowed_ids += [r["asset_id"] for r in run["context_snapshot"].get("reference_evidence", []) if "payload" in r]
        if not stale:
            workspace.memory.add_notes(run["campaign_id"], result.get("memory_updates", []), allowed_ids)
        for hypothesis in result.get("hypotheses", []):
            hypothesis.update(campaign_id=run["campaign_id"], charter_version=run["charter_version"],
                research_run_id=run_id, created_at=now())
            store.put("hypothesis", hypothesis, "hypothesis.created")
        coordinator._save_critiques(run, result)
        for index, message in enumerate(result.get("messages", [])):
            store.put("message", {**message, "id": f"message_{run_id}_{index}", "campaign_id": run["campaign_id"],
                "created_at": now(), "research_run_id": run_id, "origin": result.get("mode"),
                "stale_charter": stale}, "message.created")
        for decision in result.get("decisions", []):
            coordinator._save_decision(run, decision)
        from optimization_framework.campaigns.commands import DELEGATED
        dispatched = False
        # A researcher's critique or revision of one idea is not a request for
        # the manager to act on the suggestions it returns.
        targeted = bool(request.get("hypothesis_id")) and request.get("mode") in {"review", "evolve"}
        for action in result.get("actions", []):
            if run.get("decision_refresh_id"):
                action.update(requires_researcher=True, decision_refresh_id=run["decision_refresh_id"])
            action.update(campaign_id=run["campaign_id"], charter_version=run["charter_version"],
                research_run_id=run_id, created_at=now(), guidance_revision=run.get("guidance_revision", 0),
                authority_hash=workspace.commands.authority_hash(run["context_snapshot"]["campaign"]))
            routine = action["kind"] in {"probe", "implement", "search", "compare"} or (
                action["kind"] == "command" and action.get("command_operation") in DELEGATED)
            from optimization_framework.research.action_policy import probe_execution_issue
            design_issue = probe_execution_issue(action, autonomous=True) if action["kind"] == "probe" else None
            eligible = (campaign["autonomy"] == "delegated" and not dispatched and not result.get("decisions")
                and not targeted
                and not stale and not stopped and routine and not action.get("requires_researcher") and not design_issue
                and not (result.get("usage") or {}).get("pending_reservation"))
            if eligible:
                effect = {"id": "dispatch_" + action["id"], "campaign_id": run["campaign_id"],
                    "kind": "manager_action", "action_id": action["id"], "run_id": run_id,
                    "status": "pending", "created_at": now()}
                store.put("outbox", effect, "effect.queued")
                dispatched = True
            elif stale or stopped:
                action["status"] = "needs_reconsideration" if stale else "deferred"
            elif design_issue and not action.get("requires_researcher"):
                # A missing procedure is a manager design task, not a request to
                # approve an ambiguous plan that would fail again after approval.
                action.update(status="blocked", allocation_issue=design_issue)
                if not targeted:
                    workspace.memory.issue(run["campaign_id"], "experiment_design", design_issue, affected=action["id"])
                    store.event(run["campaign_id"], "research.action_rejected", {"record_id": action["id"]})
            else:
                from optimization_framework.campaigns.decision_presentation import action_choices, brief, present_decision
                decision = {"id": "decision_" + action["id"], "title": action["title"],
                    "rationale": action["rationale"], "action_id": action["id"], "status": "pending",
                    "options": action_choices(action), "recommendation": "accept", "decision_format": "action",
                    "recommendation_reason": brief(action["rationale"], 400), "audience": "researcher"}
                presentation = present_decision(decision, action)
                decision.update(background=presentation["background"], proposal=presentation["proposal"],
                    scope_label=presentation["scope_label"])
                coordinator._save_decision(run, decision)
            store.put("action", action)
        run.update(status="stopped" if stopped else result.get("status", "completed"), result=result,
            usage=result.get("usage", {}), trace=result.get("trace", []), finished_at=now(),
            dispatch_phase="finalized", finalized_result_id=saved["id"])
        store.put("research_run", run, "research.finished")
        if (run.get("usage") or {}).get("pending_reservation"):
            reconciliation(workspace, run)
        if stale and not stopped:
            store.event(run["campaign_id"], "research.stale_result", {"record_id": run_id})
        return run


def deliver_action(workspace, effect):
    from optimization_framework.campaigns.research_commands import manager
    with workspace.lock:
        action = workspace.store.get(effect["action_id"], "action")
        try:
            outcome = manager(workspace).execute_action(action, actor="manager")
        except (ValueError, KeyError) as exc:
            # A definitive rejection has no external effect. The original proposal
            # is retained and a new turn can reconsider it after guidance changes.
            with workspace.store.transaction():
                action.update(status="blocked", allocation_issue=str(exc))
                workspace.store.put("action", action, "action.blocked")
                effect.update(status="failed", error=str(exc), finished_at=now())
                workspace.store.put("outbox", effect, "effect.failed")
                workspace.memory.issue(action["campaign_id"], "delegated_action", str(exc), affected=action["id"])
                workspace.store.event(action["campaign_id"], "research.action_rejected", {"record_id": action["id"]})
            return
        with workspace.store.transaction():
            action.update(status="executed_within_delegation", outcome=outcome)
            workspace.store.put("action", action, "action.executed")
            effect.update(status="completed", finished_at=now())
            workspace.store.put("outbox", effect, "effect.applied")
