"""Monitor rows: problem, budget (measured and forecast, labelled), compute (workers, queue, GPU), agents, autopilot, needs-you; and the pill.

Unknown is said, never shown as zero; a forecast is called a forecast."""


def inbox_view(workspace, campaign_id):
    from optimization_framework.campaigns.decisions import public_decision
    store = workspace.store
    decisions = [public_decision(workspace, row) for row in store.list("decision", campaign_id) if row.get("status") in {"pending", "executing"}]
    issues = [row for row in store.list("manager_issue", campaign_id) if row.get("status") == "pending"]
    questions = [row for row in store.list("agent_question", campaign_id) if row.get("status") == "pending"]
    return {"decisions": decisions, "issues": issues, "questions": questions,
            "resolved": [row for row in store.list("decision", campaign_id) if row.get("status") == "resolved"][-20:]}


def gpu_row(host):
    """The GPU row from the host snapshot: busy when any device is measured busy; honest when nothing is reported."""
    gpu = (host or {}).get("gpu")
    devices = gpu.get("devices") if isinstance(gpu, dict) else gpu if isinstance(gpu, list) else []
    readings = [d for d in (devices or []) if isinstance(d, dict)]
    measured = [d for d in readings if isinstance(d.get("utilization_percent"), (int, float))]
    if not readings:
        return {"reported": False, "basis": "no GPU reported by the host"}
    if not measured:
        return {"reported": True, "measured": False, "devices": len(readings), "basis": "devices listed; utilization not measured (nvidia-smi unavailable)"}
    return {"reported": True, "measured": True, "devices": len(readings), "busy": any(d["utilization_percent"] >= 10 for d in measured),
            "utilization_percent": max(d["utilization_percent"] for d in measured),
            "memory_used_bytes": sum(d.get("memory_used_bytes") or 0 for d in measured), "memory_total_bytes": sum(d.get("memory_total_bytes") or 0 for d in measured),
            "basis": "measured by nvidia-smi"}


def monitor_view(workspace, campaign_id):
    store = workspace.store
    campaign = store.get(campaign_id, "campaign")
    try:
        assessment = workspace.resources.assessment(campaign_id)
    except (KeyError, ValueError) as exc:
        assessment = {"unavailable": str(exc)}
    live = store.list_trials_in_status({"queued", "running", "pausing", "stopping"}, campaign_id)
    remaining_live = sum(max(0.0, float(t.get("wall_seconds") or 0) - float(t.get("execution_seconds") or 0)) for t in live)
    budget_seconds = float(campaign.get("compute_budget_seconds") or 0)
    worker = {"budget_seconds": budget_seconds, **({"allocated_seconds": assessment["allocated_seconds"], "actual_seconds": assessment["actual_seconds"],
              "forecast_seconds": assessment["actual_seconds"] + remaining_live, "forecast_basis": "measured use plus the remaining allotment of live runs"}
              if "allocated_seconds" in assessment else {"unavailable": assessment.get("unavailable")}),
              "exhausted": "allocated_seconds" in assessment and assessment["allocated_seconds"] >= budget_seconds - 1e-6 and budget_seconds > 0}
    from optimization_framework.agents import usage
    charged = usage.charged(store, campaign_id)
    committed = workspace.implementations.api_committed(campaign_id)
    cap = campaign.get("llm_budget_usd")
    llm = {"cap_usd": cap, "agent_charged_usd": charged, "implementation_committed_usd": committed, "spent_usd": charged + committed,
           "exhausted": cap is not None and charged + committed >= float(cap) - 1e-9 and float(cap) > 0,
           "basis": "charged by the harness's price catalogue; subscription calls cost nothing here"}
    admission = getattr(workspace, "admission", None)
    try:
        admission_view = admission.view(campaign_id) if admission is not None else None
    except (KeyError, ValueError) as exc:
        admission_view = {"unavailable": str(exc)}
    host = None
    try:
        from optimization_framework.execution.observability import get_observer
        snapshot = get_observer(workspace).snapshot(campaign_id)
        host = snapshot.get("host") if isinstance(snapshot, dict) else None
    except Exception:   # the host snapshot is best effort; the row says when it is missing
        host = None
    compute = {"busy": len(live), "limit": workspace.max_workers, "pools": workspace.devices.view([t for t in live if t["status"] != "queued"]), "queued": len((admission_view or {}).get("waiting") or []) if isinstance(admission_view, dict) else None,
               "waiting": (admission_view or {}).get("waiting") if isinstance(admission_view, dict) else None, "level": admission.level().get("level") if admission is not None else None,
               "gpu": gpu_row(host), "host": {"cpu_percent": ((host or {}).get("cpu") or {}).get("utilization_percent"), "memory_available_bytes": ((host or {}).get("memory") or {}).get("effective_available_bytes")} if host else None}
    try:
        agents = workspace.pi.progress(campaign_id) if workspace.pi.owns(campaign_id) else {"status": "absent", "headline": "No agent team yet", "active": False}
    except (KeyError, ValueError) as exc:
        agents = {"status": "unknown", "headline": str(exc), "active": False}
    inbox = inbox_view(workspace, campaign_id)
    needs_you = {"decisions": len(inbox["decisions"]), "issues": len(inbox["issues"]), "questions": len(inbox["questions"])}
    from balsamic.autopilot.rounds import round_state
    try:
        autopilot = round_state(workspace, campaign_id)
    except (KeyError, ValueError) as exc:
        autopilot = {"enabled": False, "reason": str(exc), "can_start": False}
    from balsamic.monitor.digest import open_digest
    digest = open_digest(workspace, campaign_id)
    rows = {"problem": {"name": campaign["name"], "objective": campaign.get("objective"), "tasks": len([t for t in store.list("task", campaign_id) if not t.get("archived")]), "stage_pin": campaign.get("stage_pin")},
            "budget": {"worker": worker, "llm": llm}, "compute": compute, "agents": agents, "autopilot": autopilot, "needs_you": needs_you}
    attention = []
    if sum(needs_you.values()):
        attention.append(("needs_you", f"{sum(needs_you.values())} waiting for you"))
    if agents.get("status") in {"paused", "stopped", "failed"}:
        attention.append(("agents", "The agent team is " + agents["status"]))
    if worker.get("exhausted"):
        attention.append(("budget", "Worker budget exhausted"))
    if llm["exhausted"]:
        attention.append(("budget", "Model budget exhausted"))
    if "unavailable" in worker:
        attention.append(("budget", "Worker accounting unavailable"))
    if digest:
        pill = {"state": "digest", "text": "Away digest", "row": "digest"}
    elif attention:
        pill = {"state": "attention", "text": attention[0][1], "row": attention[0][0]}
    else:
        pill = {"state": "clear", "text": "All clear", "row": None}
    return {"rows": rows, "pill": pill, "attention": [{"row": row, "text": text} for row, text in attention], "digest_id": digest["id"] if digest else None}
