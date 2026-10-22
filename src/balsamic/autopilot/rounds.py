"""Autopilot rounds: while enabled, and no request is open, the lead gets a request turn in the autopilot thread to work the
frontier within the cap. A new round starts when the previous one's work has finished. The last round asks for the report."""
from datetime import datetime, timedelta, timezone

from optimization_framework.storage.sqlite import now

from balsamic.autopilot.settings import settings_of
from balsamic.threads.service import lead_agent

ROUND_PROMPT = ("Autopilot round {round}. Review the evolution graph (campaign_inspect with evolution; the frontier and the live runs). Within the remaining "
                "autopilot compute of {remaining:.0f} worker seconds: stop strategies that are dominated (hypothesis.status archived with stop_reason), promote "
                "finalists (hypothesis.nominate), allocate more time to the promising ones (hypothesis.allocate), and ask for new strategies (research.generate) "
                "when the frontier is thin; prefer intervention.batch for several at once. Every command states its rationale. Start nothing beyond the cap. "
                "Then call yield_work(complete) with one paragraph: what you did and why, and what the next round should look at.")
REPORT_PROMPT = ("Autopilot: the report is due by {report_by} (within a day) or the compute cap is reached. Do not start new runs. Write the report with report_write "
                 "from the finalists and the confirmed evidence, say what remains open, then yield_work(complete).")
RUN_WINDOW_SECONDS = 86400


def autopilot_thread_id(campaign_id):
    return f"thread_autopilot_{campaign_id}"


def ensure_thread(workspace, campaign_id):
    identity = autopilot_thread_id(campaign_id)
    try:
        return workspace.store.get(identity, "thread")
    except KeyError:
        record = {"id": identity, "campaign_id": campaign_id, "name": "autopilot", "title": "Autopilot", "parent_thread_id": None, "fork_message_id": None,
                  "fork_entry_id": None, "agent_leaf": None, "status": "active", "merged_into": None, "pinned_ids": [], "excluded_ids": [], "anchors": [], "summary": None,
                  "guidance_revision": 0, "last_seen_message_id": None, "system": True, "created_at": now(), "updated_at": now()}
        with workspace.lock:
            return workspace.store.put("thread", record, "thread.created")


def spent_since_baseline(workspace, campaign_id, settings):
    return max(0.0, workspace.resources.assessment(campaign_id)["allocated_seconds"] - float(settings.get("baseline_allocated_seconds") or 0))


def remaining(workspace, campaign_id, settings):
    return float(settings.get("compute_cap_seconds") or 0) - spent_since_baseline(workspace, campaign_id, settings)


def report_due(settings, clock=None):
    if not settings.get("report_by"):
        return False
    try:
        due = datetime.fromisoformat(str(settings["report_by"]).replace("Z", "+00:00"))
    except ValueError:
        return False
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    return (clock or datetime.now(timezone.utc)) >= due - timedelta(days=1)


def round_state(workspace, campaign_id):
    """What the tick sees: whether a round may start, and why not otherwise. Read by the monitor too."""
    campaign = workspace.store.get(campaign_id, "campaign")
    settings = settings_of(campaign)
    if not settings.get("enabled"):
        return {"enabled": False, "reason": "autopilot is off", "can_start": False, "settings": settings}
    config = workspace.pi.configuration(campaign_id) or {}
    lead = lead_agent(workspace, campaign_id)
    state = {"enabled": True, "settings": settings, "spent_seconds": spent_since_baseline(workspace, campaign_id, settings),
             "remaining_seconds": remaining(workspace, campaign_id, settings), "rounds_completed": settings.get("rounds_completed", 0)}
    if not lead or config.get("status") != "running":
        return {**state, "can_start": False, "reason": "the agent team is not running"}
    if config.get("request_open"):
        return {**state, "can_start": False, "reason": "a request is open: researcher messages come first"}
    runs = workspace.store.list_agent_runs(lead["id"])
    if any(run["status"] in {"queued", "submitted", "running"} for run in runs):
        return {**state, "can_start": False, "reason": "the lead is in a turn"}
    live = [t for t in workspace.store.list("trial", campaign_id) if t.get("status") in {"queued", "running", "pausing", "stopping"}]
    if live:
        return {**state, "can_start": False, "reason": f"{len(live)} runs of the previous round are still going"}
    if settings.get("report_requested"):
        return {**state, "can_start": False, "reason": "the final round asked for the report"}
    from balsamic.evolution.stages import definition
    gate = definition(workspace, campaign)
    if not gate["ready"]:
        return {**state, "can_start": False, "reason": "the problem definition is incomplete: " + gate["step"]["title"].lower() + " (" + gate["step"]["page"] + ")"}
    from balsamic.evolution.stages import evaluator
    blocked = evaluator(workspace, campaign)
    if blocked:
        return {**state, "can_start": False, "reason": "the evaluator cannot start in this lab: " + blocked["error"]}
    if report_due(settings) or state["remaining_seconds"] <= 0:
        return {**state, "can_start": True, "final": True, "reason": "the cap or the report date is reached: the last round writes the report"}
    return {**state, "can_start": True, "final": False, "reason": "ready for the next round"}


def tick(workspace):
    """Called from the workspace's maintenance thread; starts at most one round per campaign per pass."""
    for campaign in workspace.store.list("campaign"):
        settings = settings_of(campaign)
        if not settings.get("enabled"):
            continue
        try:
            count_finished_rounds(workspace, campaign["id"])
            state = round_state(workspace, campaign["id"])
            if state.get("can_start"):
                start_round(workspace, campaign["id"], final=state.get("final", False))
        except (KeyError, ValueError) as exc:
            workspace.memory.issue(campaign["id"], "autopilot_round", str(exc), affected=campaign["id"])


def count_finished_rounds(workspace, campaign_id):
    """A round is complete when its run ended; the settings carry the count and the digest is written then."""
    with workspace.lock, workspace.store.transaction():
        campaign = workspace.store.get(campaign_id, "campaign")
        settings = settings_of(campaign)
        last = settings.get("last_round_run_id")
        if not last:
            return
        run = workspace.store.get(last, "agent_run")
        if run["status"] in {"completed", "failed", "interrupted", "stopped"} and not settings.get("last_round_counted"):
            settings["rounds_completed"] = settings.get("rounds_completed", 0) + 1
            settings["last_round_counted"] = True
            settings["last_round_finished_at"] = now()
            campaign["autopilot"] = settings
            workspace.store.put("campaign", campaign, "campaign.autopilot_round_completed")
            from balsamic.monitor.digest import maybe_write_digest
            maybe_write_digest(workspace, campaign_id)


def start_round(workspace, campaign_id, *, final=False):
    campaign = workspace.store.get(campaign_id, "campaign")
    settings = settings_of(campaign)
    thread = ensure_thread(workspace, campaign_id)
    lead = lead_agent(workspace, campaign_id)
    number = settings.get("rounds_completed", 0) + 1
    prompt = REPORT_PROMPT.format(report_by=settings.get("report_by") or "the cap") if final else ROUND_PROMPT.format(round=number, remaining=remaining(workspace, campaign_id, settings))
    identity = f"pi_autopilot_{campaign_id}_{number}"
    with workspace.lock, workspace.store.transaction():
        workspace.pi._request(campaign_id, True)
        message = {"id": "message_" + identity, "campaign_id": campaign_id, "role": "user", "origin": "autopilot", "thread_id": thread["id"],
                   "content": f"Round {number}" + (" (final: the report)" if final else "") + f": review the frontier within {remaining(workspace, campaign_id, settings):.0f} s of the cap.", "created_at": now()}
        workspace.store.put("message", message, "message.created")
        run = workspace.pi.enqueue(lead, identity, prompt, mode="follow_up", origin={"thread_id": thread["id"], "message_id": message["id"], "actor": "autopilot"},
                                   **({"leaf_entry_id": thread["agent_leaf"]} if thread.get("agent_leaf") else {}), thread_revision=thread.get("guidance_revision"), autopilot=True)
        settings.update(last_round_run_id=run["id"], last_round_counted=False, last_round_started_at=now(), report_requested=bool(final))
        campaign["autopilot"] = settings
        workspace.store.put("campaign", campaign, "campaign.autopilot_round")
    return run


def cap_guard(workspace):
    """Refuses an autopilot command whose reservation would take the campaign past the cap."""
    def guard(command, actor, campaign):
        if actor != "autopilot":
            return
        settings = settings_of(campaign)
        if not settings.get("enabled"):
            raise ValueError("Autopilot is off; this turn's commands are refused")
        payload = command.payload or {}
        reservation = 0.0
        if command.operation in {"trial.create", "trial.continue", "hypothesis.allocate"}:
            reservation = float(payload.get("wall_seconds") or payload.get("worker_seconds") or 0)
        elif command.operation == "intervention.batch":
            reservation = sum(float((item.get("payload") or {}).get("worker_seconds") or (item.get("payload") or {}).get("wall_seconds") or 0) for item in payload.get("items") or [])
        if reservation and spent_since_baseline(workspace, campaign["id"], settings) + reservation > float(settings.get("compute_cap_seconds") or 0) + 1e-6:
            raise ValueError(f"Autopilot's compute cap ({settings.get('compute_cap_seconds'):g} s) does not cover {reservation:g} s more; ask the researcher to raise it")
    return guard


def register(workspace):
    workspace.commands.guards.append(cap_guard(workspace))
    previous = workspace.on_manager_tick

    def on_tick():
        if previous:
            previous()
        tick(workspace)
    workspace.on_manager_tick = on_tick
