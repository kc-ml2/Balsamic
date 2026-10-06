"""Append-only costs of model turns and reusable implementation production."""
from optimization_framework.contracts.assets import Asset, CostEvent, CostSlice, CostSnapshot
from optimization_framework.contracts.base import content_hash
from optimization_framework.assets.accounting import close, prefix, project, reconcile


AXES = ("worker_seconds", "evaluation_requests", "solver_executions", "implementation_seconds",
        "model_seconds", "model_calls", "model_input_tokens", "model_output_tokens", "api_usd")


def model_quantities(usage):
    missing = usage is None
    usage = usage or {}
    calls = None if missing else usage.get("calls", 0 if not usage else None)
    uncertain = bool(usage.get("pending_reservation")) or any(word in usage.get("token_accounting", "") for word in ("unknown", "pending", "reservation"))
    quantities = {axis: 0. for axis in AXES}
    quantities.update(model_calls=calls, model_seconds=0 if calls == 0 else usage.get("elapsed_seconds") if calls is not None and usage.get("timed_calls") == calls else None,
        model_input_tokens=None if uncertain else usage.get("input_tokens", 0 if calls == 0 else None),
        model_output_tokens=None if uncertain else usage.get("output_tokens", 0 if calls == 0 else None))
    if usage.get("billing_mode") == "subscription":
        quantities["api_usd"] = 0  # Subscription calls/tokens remain separate.
    else:
        charge_unknown = any(word in usage.get("cost_accounting", "") for word in ("unknown", "pending", "reservation"))
        quantities["api_usd"] = None if charge_unknown else usage.get("api_cost_usd", usage.get("cost_usd", 0 if calls == 0 else None))
    return quantities


def append_snapshot(catalog, source_id, campaign_id, quantities, *, category, created_at, evidence_ids, receipt=None, work_cursor=None):
    """Reconstruct physical history; new knowledge reconciles an existing prefix."""
    quantities = {axis: float(value) if value is not None else None for axis, value in quantities.items()}
    tracker_id = "cost_cursor_" + content_hash(source_id)
    snapshot_values = {"campaign_id": campaign_id, "source_id": source_id, "quantities": quantities,
        "evidence_ids": sorted(set(evidence_ids)), "receipt": receipt or {}, "work_cursor": str(work_cursor) if work_cursor is not None else None}
    snapshot_id = "cost_snapshot_" + content_hash(snapshot_values)
    with catalog.store.transaction():
        try:
            known_snapshot = catalog.store.get(snapshot_id, "cost_snapshot")
        except KeyError:
            known_snapshot = None
        if known_snapshot:
            # Exact immutable receipt replay needs no new accounting equations.
            # Verify its source prefix through the transactionally maintained
            # ordinal index without parsing every other campaign's cost history.
            with catalog.store.connection() as db:
                position = db.execute("""SELECT COUNT(*) AS count, MIN(p.ordinal) AS first,
                    MAX(p.ordinal) AS last, SUM(CASE WHEN r.campaign_id IS NOT ? THEN 1 ELSE 0 END) AS foreign_owner
                    FROM cost_positions p LEFT JOIN records r ON r.id=p.cost_event_id
                    WHERE p.source_id=?""", (campaign_id, source_id)).fetchone()
            if (not position["count"] or position["first"] != 0
                    or position["count"] != position["last"] + 1 or position["count"] < known_snapshot["stop"]):
                raise ValueError("Cost reconciliation requires the complete original event prefix; import its missing evidence first")
            if position["foreign_owner"]:
                raise ValueError("A cost source retains its original campaign owner")
            return CostSlice(source_id=source_id, stop=known_snapshot["stop"])
        # Imported cursors are intentionally not an authority. Derive the local
        # position from original events and reconciliations, including old data
        # that predates immutable service snapshots.
        events = sorted((event for event in catalog.store.list("cost_event") if event["source_id"] == source_id), key=lambda event: event["ordinal"])
        stop = events[-1]["ordinal"] + 1 if events else 0
        if stop:
            prefix(events, stop)
            if any(event["campaign_id"] != campaign_id for event in events):
                raise ValueError("A cost source retains its original campaign owner")
        reconciliations = [row for row in catalog.store.list("cost_reconciliation") if row["source_id"] == source_id]
        totals, _ = project(events, reconciliations, {source_id: [(0, stop)]} if stop else {}, quantities)
        previous = {axis: value["total"] for axis, value in totals.items()}
        changed_work = not stop
        prior_snapshots = [row for row in catalog.store.list("cost_snapshot") if row["source_id"] == source_id and row["stop"] == stop]
        same_work = False
        prior_work = {row["work_cursor"] for row in prior_snapshots if row.get("work_cursor") is not None}
        if work_cursor is not None and prior_work:
            same_work = str(work_cursor) in prior_work
            changed_work |= not same_work
        progress = any(quantities.get(axis) is not None and previous.get(axis) is not None
            and quantities[axis] > previous[axis] and not close(quantities[axis], previous[axis])
            for axis in ("model_calls", "evaluation_requests", "solver_executions"))
        for axis, value in quantities.items():
            before = previous[axis]
            if value is not None and value < totals[axis]["known"] and not close(value, totals[axis]["known"]):
                raise ValueError("Service cost decreased; its receipt contradicts recorded expenditure")
            if same_work and not progress:
                if before is not None and value is not None and not close(value, before):
                    raise ValueError("Receipt contradicts measured expenditure for the same work boundary")
                continue
            if (before is not None and value is None) or (before is not None and value is not None and value > before and not close(value, before)):
                changed_work = True
        delta = {}
        for axis, value in quantities.items():
            before = previous[axis]
            if value is None or before is None:
                delta[axis] = None
            else:
                delta[axis] = max(0., value - before)
        snapshot = CostSnapshot(id=snapshot_id, **snapshot_values, stop=stop + int(changed_work), created_at=created_at)
        catalog.store.put_immutable("cost_snapshot", snapshot.model_dump(mode="json"), "cost.receipt_captured")
        if changed_work:
            catalog.record_cost(CostEvent(id="cost_" + content_hash([source_id, stop]), campaign_id=campaign_id,
                source_id=source_id, ordinal=stop, category=category, quantities=delta, evidence_ids=[*evidence_ids, snapshot_id],
                status="uncertain" if any(value is None for value in delta.values()) else "measured", created_at=created_at))
            stop += 1
        improved = {axis: value for axis, value in quantities.items() if value is not None and previous[axis] is None}
        if improved:
            reconcile(catalog, source_id, stop, improved, evidence_ids=[snapshot_id], authority="service_receipt",
                rationale="Captured cumulative service receipt resolves previously unmeasured expenditure")
        cursor = {"id": tracker_id, "campaign_id": campaign_id, "source_id": source_id, "stop": stop, "quantities": quantities}
        try:
            existing = catalog.store.get(tracker_id, "service_cost_cursor")
        except KeyError:
            existing = None
        if existing != cursor:
            catalog.store.put("service_cost_cursor", cursor)
        return CostSlice(source_id=source_id, stop=stop)


def research_asset(catalog, run):
    quantities = model_quantities(run.get("usage"))
    interval = append_snapshot(catalog, "model:" + run["id"], run["campaign_id"], quantities,
        category="model", created_at=run["created_at"], evidence_ids=[run["id"]], receipt={"usage": run.get("usage")},
        work_cursor=run.get("cost_work_revision"))
    contexts = run.get("context_snapshot", {})
    exposed = sorted({task["problem"]["scientific_identity"] for task in contexts.get("tasks", []) if task.get("problem")})
    identity = "model_work_" + content_hash([run["id"], interval.stop])
    billing_mode = (run.get("usage") or {}).get("billing_mode")
    try:
        # Later receipts may identify the billing mode as well as quantities.
        # Preserve the original asset's metadata; the snapshot retains the
        # newly reported mode without rewriting its historical result.
        billing_mode = catalog.store.get(identity, "asset")["payload"].get("billing_mode")
    except KeyError:
        pass
    return catalog.publish(Asset(id=identity, campaign_id=run["campaign_id"],
        kind="finding", title="Model work for a campaign turn", producer_id=run["id"], costs=[interval],
        payload={"research_run_id": run["id"], "role": "cost_provenance", "billing_mode": billing_mode},
        cost_provenance="complete", exposure_status="unknown", exposed_instance_ids=exposed,
        authority="research_service", created_at=run["created_at"]))


def capture_research(workspace, run):
    asset = research_asset(workspace.assets, run)
    for hypothesis in workspace.store.list("hypothesis", run["campaign_id"]):
        if hypothesis.get("research_run_id") == run["id"] and hypothesis.get("research_cost_asset_ids") != [asset["id"]]:
            hypothesis["research_cost_asset_ids"] = [asset["id"]]
            workspace.store.put("hypothesis", hypothesis, "hypothesis.cost_provenance")
    return asset


def runtime_asset(catalog, receipt):
    quantities = {axis: 0. for axis in AXES}
    quantities.update(worker_seconds=receipt["costs"]["elapsed_seconds"],
        implementation_seconds=receipt["costs"]["elapsed_seconds"], runtime_resolution_seconds=receipt["costs"]["elapsed_seconds"])
    interval = append_snapshot(catalog, "runtime:" + receipt["id"], receipt["campaign_id"], quantities,
        category="overhead", created_at=receipt["created_at"], evidence_ids=[receipt["id"]], receipt={"costs": receipt["costs"]})
    return catalog.publish(Asset(id="runtime_work_" + receipt["id"], campaign_id=receipt["campaign_id"], kind="implementation",
        title="Local runtime resolution", producer_id=receipt["id"], costs=[interval], cost_provenance="complete",
        payload={"runtime_resolution_receipt_id": receipt["id"], "runtime_digest": receipt["runtime_digest"]},
        authority="implementation_service", created_at=receipt["created_at"]))


def reconcile_workspace(workspace):
    """Recover terminal accounting after process loss, and admit later receipts."""
    kinds = ("research_run", "runtime_resolution_receipt", "cost_ingestion")
    stamp = workspace.store.revision(*kinds)
    if getattr(workspace, "_costs_reconciled_at", None) == stamp:
        return
    clean = True
    sources = [("research_run", row) for row in workspace.store.list("research_run") if row["status"] not in {"running", "stopping"}]
    sources += [("runtime_resolution_receipt", row) for row in workspace.store.list("runtime_resolution_receipt")]
    for kind, row in sources:
        identity = "cost_ingestion_" + content_hash([kind, row["id"]])
        receipt_hash = content_hash(row)
        try:
            if workspace.store.get(identity, "cost_ingestion")["receipt_hash"] == receipt_hash:
                continue
        except KeyError:
            pass
        try:
            with workspace.lock, workspace.store.transaction():
                asset = capture_research(workspace, row) if kind == "research_run" else runtime_asset(workspace.assets, row)
                workspace.store.put("cost_ingestion", {"id": identity, "campaign_id": row["campaign_id"],
                    "receipt_hash": receipt_hash, "asset_id": asset["id"]})
        except (ValueError, KeyError) as exc:
            clean = False
            workspace.memory.issue(row["campaign_id"], "cost_reconciliation", str(exc), affected=row["id"])
    if clean:
        # Our own ingestion writes count too; the next pass confirms a fixed point.
        workspace._costs_reconciled_at = workspace.store.revision(*kinds)


def record_implementation_job(catalog, job):
    if not job.get("accounting_final"):
        raise ValueError("Implementation accounting is still settling; retry when the library job finishes")
    quantities = model_quantities(job.get("usage"))
    quantities["implementation_seconds"] = None if job.get("unknown_compute_cost") else job.get("compute_seconds")
    # The service wall interval includes model waits. Those waits are shown
    # separately; unmeasured waits must not become numerical worker time.
    elapsed, model_time = quantities["implementation_seconds"], quantities["model_seconds"]
    quantities["worker_seconds"] = None if elapsed is None or model_time is None or job.get("unknown_compute_cost") else max(0., elapsed - model_time)
    reports = [attempt.get("report") for attempt in job.get("attempts", [])]
    for axis in ("evaluation_requests", "solver_executions"):
        if not job.get("unknown_compute_cost") and reports and all(report and "costs" in report for report in reports):
            values = [report["costs"].get(axis) for report in reports]
            quantities[axis] = None if any(value is None for value in values) else sum(values)
        else:
            quantities[axis] = None
    return append_snapshot(catalog, "implementation:" + job["id"], job["campaign_id"], quantities,
        category="implementation", created_at=job["created_at"], evidence_ids=[job["id"]],
        receipt={key: job.get(key) for key in ("usage", "compute_seconds", "unknown_compute_cost", "attempts")},
        work_cursor=job.get("execution_started_at"))


def implementation_asset(catalog, version, jobs, *, dependency_ids=(), upstream_complete=False):
    dependency_ids = sorted(set(dependency_ids))
    intervals = [record_implementation_job(catalog, job) for job in jobs]
    if not jobs:
        return catalog.publish(Asset(id="implementation_cost_unknown_" + version["id"], campaign_id="external_library",
            kind="implementation", title=version["name"], payload={"implementation_version_id": version["id"]},
            cost_provenance="unknown", exposure_status="unknown", authority="implementation_library", created_at=version["created_at"]))
    return catalog.publish(Asset(id="implementation_work_" + content_hash([version["id"], [item.model_dump() for item in intervals], sorted(dependency_ids), upstream_complete]),
        campaign_id=jobs[0]["campaign_id"], kind="implementation", title=version["name"],
        payload={"implementation_version_id": version["id"], "production_job_ids": [job["id"] for job in jobs],
                 "accounting_rule": "Fully attribute local implementation work and declared inputs; retain model usage as separate quantities"},
        producer_id=version["id"], costs=intervals, dependency_ids=list(dependency_ids),
        cost_provenance="complete" if upstream_complete else "partial",
        exposure_status="unknown", exposed_instance_ids=version.get("exposed_conditions", []),
        applicability={"problem_id": version["spec"].get("problem_id", version["spec"].get("manifest", {}).get("id", "meent_grating")),
                       **({"executable_kind": "evaluator"} if version.get("kind") == "evaluator" else {})},
        authority="implementation_library", created_at=version["created_at"]))
