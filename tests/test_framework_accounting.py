"""Later receipts reconcile actual work without inventing prefix allocations."""
from framework_fixtures import researcher_idea
import copy

import pytest

from optimization_framework.assets.accounting import reconcile
from optimization_framework.assets.catalog import AssetCatalog
from optimization_framework.assets.service_costs import append_snapshot, model_quantities, research_asset
from optimization_framework.contracts.assets import Asset, CostEvent, CostSlice
from optimization_framework.storage.sqlite import Store


def snapshot(catalog, calls, tokens, *, source="model:turn"):
    return append_snapshot(catalog, source, "origin", {"model_calls": calls, "model_input_tokens": tokens},
        category="model", created_at="historical", evidence_ids=["turn"])


def result(catalog, identity, intervals, dependencies=()):
    return catalog.publish(Asset(id=identity, campaign_id="consumer", kind="solution", title=identity,
        costs=intervals, dependency_ids=list(dependencies), cost_provenance="complete", authority="researcher", created_at="historical"))


def quantity(catalog, identity, axis="model_input_tokens"):
    return catalog.attributed_costs([identity], axes=[axis])["quantities"][axis]


def test_later_receipt_resolves_unknown_cost_without_mutating_events_or_rebilling_work(tmp_path):
    catalog = AssetCatalog(Store(tmp_path))
    first = snapshot(catalog, 1, None)
    original = copy.deepcopy(catalog.store.list("cost_event"))
    asset = result(catalog, "answer", [first])
    assert quantity(catalog, "answer")["total"] is None
    final = snapshot(catalog, 1, 20)
    assert first == final
    assert catalog.store.list("cost_event") == original
    assert catalog.store.get(asset["id"], "asset") == asset
    assert quantity(catalog, "answer")["total"] == 20
    assert catalog.actual_costs("origin", axes=["model_calls"])["quantities"]["model_calls"]["total"] == 1
    assert len(catalog.store.list("cost_reconciliation")) == 1
    assert snapshot(catalog, 1, 20) == final
    assert len(catalog.store.list("cost_reconciliation")) == 1 and len(catalog.store.list("cost_event")) == 1


def test_receipt_for_more_work_does_not_allocate_unknown_usage_to_an_older_result(tmp_path):
    catalog = AssetCatalog(Store(tmp_path))
    first = snapshot(catalog, 1, None)
    result(catalog, "early", [first])
    final = snapshot(catalog, 2, 30)
    result(catalog, "later", [final])
    result(catalog, "suffix", [CostSlice(source_id=first.source_id, start=1, stop=2)])
    assert len(catalog.store.list("cost_event")) == 2
    assert quantity(catalog, "early")["total"] is None
    assert quantity(catalog, "suffix")["total"] is None
    assert quantity(catalog, "later")["total"] == 30
    assert quantity(catalog, "early", "model_calls")["total"] == 1
    original = copy.deepcopy(catalog.store.list("cost_event"))
    receipt = reconcile(catalog, first.source_id, 1, {"model_input_tokens": 10}, evidence_ids=["original_provider_receipt"],
                        authority="researcher", rationale="Provider receipt for the first call")
    assert quantity(catalog, "early")["total"] == 10
    assert quantity(catalog, "suffix")["total"] == 20
    assert quantity(catalog, "later")["total"] == 30
    assert receipt["id"] in catalog.attributed_costs(["early"])["accounting_basis"]["reconciliation_ids"]
    assert catalog.store.list("cost_event") == original


def test_disjoint_contributions_use_aggregate_evidence_without_guessing_each_share(tmp_path):
    catalog = AssetCatalog(Store(tmp_path))
    for ordinal, value in enumerate((None, 7, None)):
        catalog.record_cost(CostEvent(id=f"cost_{ordinal}", campaign_id="origin", source_id="shared", ordinal=ordinal,
            category="model", quantities={"model_input_tokens": value}, created_at="historical"))
    reconcile(catalog, "shared", 3, {"model_input_tokens": 30}, evidence_ids=["provider_receipt"], authority="researcher", rationale="Cumulative provider receipt")
    result(catalog, "first", [CostSlice(source_id="shared", stop=1)])
    result(catalog, "third", [CostSlice(source_id="shared", start=2, stop=3)])
    assert quantity(catalog, "first")["total"] is None and quantity(catalog, "third")["total"] is None
    costs = catalog.attributed_costs(["first", "third"], axes=["model_input_tokens"])
    assert costs["quantities"]["model_input_tokens"]["total"] == 23
    assert costs["accounting_basis"]["status"] == "complete"
    assert catalog.attributed_costs(["first", "first", "third"], axes=["model_input_tokens"]) == costs


def test_receipts_reject_inconsistent_equations_and_negative_unmeasured_remainders(tmp_path):
    catalog = AssetCatalog(Store(tmp_path))
    for ordinal, value in enumerate((None, 12, None)):
        catalog.record_cost(CostEvent(id=f"cost_{ordinal}", campaign_id="origin", source_id="shared", ordinal=ordinal,
            category="model", quantities={"model_input_tokens": value}, created_at="historical"))
    def receipt(stop, total):
        return reconcile(catalog, "shared", stop, {"model_input_tokens": total}, evidence_ids=["provider"], authority="researcher", rationale="Reported provider total")
    with pytest.raises(ValueError, match="negative expenditure"):
        receipt(3, 10)
    assert not catalog.store.list("cost_reconciliation")
    receipt(3, 15)
    with pytest.raises(ValueError, match="negative expenditure"):
        receipt(1, 8)
    with pytest.raises(ValueError, match="contradicts"):
        receipt(3, 16)
    assert len(catalog.store.list("cost_reconciliation")) == 1


def test_missing_usage_stays_unknown_and_a_missing_prefix_cannot_be_recreated_as_new_charges(tmp_path):
    assert model_quantities(None)["model_calls"] is None
    assert model_quantities({})["model_calls"] == 0
    assert model_quantities({"input_tokens": 10})["model_calls"] is None
    catalog = AssetCatalog(Store(tmp_path))
    catalog.record_cost(CostEvent(id="tail", campaign_id="origin", source_id="model:turn", ordinal=1,
        category="model", quantities={"model_calls": 2}, created_at="historical"))
    with pytest.raises(ValueError, match="complete original event prefix"):
        snapshot(catalog, 3, 30)
    assert len(catalog.store.list("cost_event")) == 1


def test_imported_resumed_costs_reconstruct_their_position_and_keep_future_receipts_portable(tmp_path):
    from test_framework_bundles import campaign, export, inspect, publish
    source, owner, _ = campaign(tmp_path / "source")
    run = {"id": "resumed_turn", "campaign_id": owner, "created_at": "historical", "usage": {"calls": 1, "input_tokens": None}}
    early = research_asset(source.assets, run)
    run["usage"] = {"calls": 3, "input_tokens": 30}
    later = research_asset(source.assets, run)
    _, archive = export(source, owner, [early["id"], later["id"]])
    destination, current, _ = campaign(tmp_path / "destination")
    publish(destination, current, inspect(destination, current, archive))
    assert not destination.store.list("service_cost_cursor")
    original = copy.deepcopy(destination.store.list("cost_event"))
    assert research_asset(destination.assets, run) == later
    assert destination.store.list("cost_event") == original
    run["usage"] = {"calls": 4, "input_tokens": 45}
    newest = research_asset(destination.assets, run)
    assert len(destination.store.list("cost_event")) == len(original) + 1
    assert quantity(destination.assets, newest["id"], "model_calls")["total"] == 4
    assert quantity(destination.assets, later["id"])["total"] == 30
    assert quantity(destination.assets, early["id"])["total"] is None
    assert destination.assets.actual_costs(current)["event_count"] == 0
    _, returned = export(destination, current, [newest["id"]], "export_reconciled")
    third, recipient, _ = campaign(tmp_path / "third")
    publish(third, recipient, inspect(third, recipient, returned))
    assert third.assets.attributed_costs([newest["id"]]) == destination.assets.attributed_costs([newest["id"]])
    assert research_asset(third.assets, run) == newest


def test_legacy_events_reconstruct_accounting_without_trusting_a_mutable_cursor(tmp_path):
    catalog = AssetCatalog(Store(tmp_path))
    for ordinal, calls in enumerate((1, 2)):
        catalog.record_cost(CostEvent(id=f"old_{ordinal}", campaign_id="origin", source_id="model:turn", ordinal=ordinal,
            category="model", quantities={"model_calls": calls, "model_input_tokens": None}, created_at="historical"))
    from optimization_framework.contracts.base import content_hash
    catalog.store.put("service_cost_cursor", {"id": "cost_cursor_" + content_hash("model:turn"),
        "source_id": "model:turn", "campaign_id": "origin", "stop": 99, "quantities": {"model_calls": 50}})
    original = copy.deepcopy(catalog.store.list("cost_event"))
    current = snapshot(catalog, 3, 30)
    assert current.stop == 2 and catalog.store.list("cost_event") == original
    result(catalog, "current", [current])
    assert quantity(catalog, "current")["total"] == 30
    assert snapshot(catalog, 4, 45).stop == 3
    assert catalog.actual_costs("origin", axes=["model_calls"])["quantities"]["model_calls"]["total"] == 4


def test_unmeasured_work_keeps_attempt_boundaries_until_its_receipt_arrives(tmp_path):
    catalog = AssetCatalog(Store(tmp_path))
    run = {"id": "unmeasured", "campaign_id": "origin", "created_at": "historical", "usage": None, "cost_work_revision": 1}
    early = research_asset(catalog, run)
    run["cost_work_revision"] = 2
    later = research_asset(catalog, run)
    assert early["id"] != later["id"] and len(catalog.store.list("cost_event")) == 2
    original = copy.deepcopy(catalog.store.list("cost_event"))
    run["usage"] = {"calls": 2, "input_tokens": 40}
    assert research_asset(catalog, run) == later
    assert catalog.store.list("cost_event") == original
    assert quantity(catalog, later["id"])["total"] == 40
    assert quantity(catalog, early["id"])["total"] is None


def test_same_work_receipt_cannot_turn_a_conflicting_measurement_into_new_work(tmp_path):
    catalog = AssetCatalog(Store(tmp_path))
    run = {"id": "one_turn", "campaign_id": "origin", "created_at": "historical", "cost_work_revision": 1,
        "usage": {"calls": 1, "input_tokens": 20}}
    asset = research_asset(catalog, run)
    events = copy.deepcopy(catalog.store.list("cost_event"))
    run["usage"]["input_tokens"] = 21
    with pytest.raises(ValueError, match="same work boundary"):
        research_asset(catalog, run)
    assert catalog.store.list("cost_event") == events
    # An incomplete later message cannot erase the supported measurement or
    # invent another unmeasured attempt with the same producer work identity.
    run["usage"]["input_tokens"] = None
    assert research_asset(catalog, run) == asset
    assert catalog.store.list("cost_event") == events
    assert quantity(catalog, asset["id"])["total"] == 20
    run["usage"] = {"calls": 2, "input_tokens": 30}
    assert research_asset(catalog, run)["id"] != asset["id"]
    assert len(catalog.store.list("cost_event")) == 2


def test_reexport_of_an_imported_root_includes_later_local_receipts(tmp_path):
    from test_framework_bundles import campaign, export, inspect, publish
    source, owner, _ = campaign(tmp_path / "source")
    run = {"id": "original", "campaign_id": owner, "created_at": "historical", "usage": {"calls": 1, "input_tokens": None}}
    root = research_asset(source.assets, run)
    _, archive = export(source, owner, [root["id"]])
    destination, current, _ = campaign(tmp_path / "destination")
    publish(destination, current, inspect(destination, current, archive))
    original = copy.deepcopy(destination.store.list("cost_event"))
    run["usage"] = {"calls": 1, "input_tokens": 21}
    assert research_asset(destination.assets, run) == root
    assert destination.store.list("cost_event") == original
    _, returned = export(destination, current, [root["id"]], "new_evidence")
    third, recipient, _ = campaign(tmp_path / "third")
    checked = inspect(third, recipient, returned)
    publish(third, recipient, checked)
    assert quantity(third.assets, root["id"])["total"] == 21
    assert third.store.list("cost_event") == original
    assert third.assets.attributed_costs([root["id"]]) == destination.assets.attributed_costs([root["id"]])
    receipts = third.store.list("cost_reconciliation")
    publish(third, recipient, checked, "repeat")
    assert third.store.list("cost_reconciliation") == receipts


def test_conflicting_imported_receipt_is_rejected_before_publishing_accounting(tmp_path):
    from test_framework_bundles import campaign, export, inspect, publish
    from optimization_framework.contracts.commands import Command
    source, owner, _ = campaign(tmp_path / "source")
    run = {"id": "original", "campaign_id": owner, "created_at": "historical", "usage": {"calls": 1, "input_tokens": None}}
    root = research_asset(source.assets, run)
    _, original_bundle = export(source, owner, [root["id"]])
    destination, current, _ = campaign(tmp_path / "destination")
    publish(destination, current, inspect(destination, current, original_bundle))
    for workspace, value in ((source, 10), (destination, 11)):
        reconcile(workspace.assets, "model:original", 1, {"model_input_tokens": value}, evidence_ids=[root["id"]],
            authority="researcher", rationale="Separately reported statement")
    before = copy.deepcopy(destination.store.list("cost_reconciliation"))
    _, conflicting = export(source, owner, [root["id"]], "later_export")
    with conflicting.open("rb") as stream:
        upload = destination.bundles.upload(stream)
    accepted = destination.commands.execute(Command(id="conflicting_receipt", campaign_id=current, expected_revision=1,
        operation="bundle.inspect", payload={"upload_id": upload["upload_id"]}))
    operation = destination.store.get(accepted["outcome"]["operation_id"], "bundle_operation")
    assert operation["status"] == "failed" and "contradicts" in operation["error"]
    assert destination.store.list("cost_reconciliation") == before
    assert quantity(destination.assets, root["id"])["total"] == 11


def test_startup_recovers_terminal_and_interrupted_research_costs_and_admits_late_receipts(tmp_path, monkeypatch):
    from optimization_framework.assets.service_costs import reconcile_workspace
    from optimization_framework.execution.service import Workspace
    from test_framework_provenance import campaign
    from test_framework_service_costs import usage
    workspace, owner, _ = campaign(tmp_path)
    monkeypatch.setattr(Workspace, "_loop", lambda self: None)
    monkeypatch.setattr(Workspace, "_maintenance", lambda self: None)
    for name, state, measurement in (("failed", "failed", {"calls": 1}), ("lost", "running", None),
            ("pending", "running", {"calls": 1, "pending_reservation": {"usd": 8}})):
        workspace.store.put("research_run", {"id": name, "campaign_id": owner, "charter_version": 1,
            "created_at": "historical", "status": state, "usage": measurement, "cost_work_revision": 1})
    hypothesis = researcher_idea(workspace, owner)
    workspace.store.put("hypothesis", {**hypothesis, "research_run_id": "failed"})
    workspace.start()
    try:
        assert workspace.store.get("lost", "research_run")["status"] == "interrupted"
        assert workspace.store.get("pending", "research_run")["status"] == "needs_reconciliation"
        assert len(workspace.store.list("cost_event")) == 3
        original = copy.deepcopy(workspace.store.list("cost_event"))
        cost_asset = workspace.store.get(hypothesis["id"], "hypothesis")["research_cost_asset_ids"][0]
        assert quantity(workspace.assets, cost_asset)["total"] is None
        run = workspace.store.get("failed", "research_run")
        workspace.store.put("research_run", {**run, "usage": usage()})
        reconcile_workspace(workspace)
        assert quantity(workspace.assets, cost_asset)["total"] == 10
        assert workspace.store.list("cost_event") == original
        assert workspace.assets.actual_costs(owner, axes=["model_calls"])["quantities"]["model_calls"]["total"] is None
        reconciled = copy.deepcopy(workspace.store.list("cost_reconciliation"))
    finally:
        workspace.close()
    restarted = Workspace(tmp_path)
    restarted.start()
    try:
        assert restarted.store.list("cost_event") == original
        assert restarted.store.list("cost_reconciliation") == reconciled
        assert len(restarted.store.list("decision", owner)) == 1
    finally:
        restarted.close()


def test_terminal_library_receipts_reconcile_after_restart_and_reject_stale_responses(tmp_path):
    from optimization_framework.execution.service import Workspace
    from optimization_framework.implementations.service import ImplementationService
    from test_framework_provenance import campaign
    from test_framework_service_costs import usage
    workspace, owner, _ = campaign(tmp_path / "workspace")
    service = ImplementationService(tmp_path / "library")
    class Client:
        def events(self, **kwargs): return service.events(**kwargs)
        def job(self, identity): return service.store.get(identity, "implementation_job")
    workspace.implementations.client = Client()
    grant = workspace.store.put("implementation_grant", {"id": "grant", "campaign_id": owner, "job_id": "failed_build",
        "request": {"compute_seconds": 10, "api_budget_usd": 8}, "status": "failed", "created_at": "historical"})
    original_job = service.store.put("implementation_job", {"id": "failed_build", "campaign_id": owner, "revision": 1,
        "created_at": "historical", "updated_at": "historical", "status": "failed", "accounting_final": True,
        "usage": {"calls": 1, "pending_reservation": {"usd": 8}}, "compute_seconds": 10, "unknown_compute_cost": True,
        "attempts": [], "execution_started_at": 100}, "implementation.progress")
    workspace.implementations.reconcile()
    original = copy.deepcopy(workspace.store.list("cost_event"))
    assert original[0]["quantities"]["implementation_seconds"] is None
    assert workspace.implementations.compute_committed(owner) == 10
    # A settled grant with a cursor still receives subsequent producer events.
    restarted = Workspace(workspace.directory, implementation_client=Client())
    service.update_job("failed_build", usage=usage(), compute_seconds=5, unknown_compute_cost=False,
        attempts=[{"report": {"costs": {"evaluation_requests": 2, "solver_executions": 2}}}])
    restarted.implementations.reconcile()
    assert restarted.store.list("cost_event") == original
    totals = restarted.assets.actual_costs(owner, axes=["implementation_seconds", "model_input_tokens", "api_usd"])["quantities"]
    assert {axis: value["total"] for axis, value in totals.items()} == {"implementation_seconds": 5, "model_input_tokens": 10, "api_usd": .1}
    current = restarted.store.get("grant", "implementation_grant")
    assert current["source_job_revision"] == 2 and restarted.implementations.compute_committed(owner) == 5
    assert restarted.implementations._apply_job(grant, original_job) == current
    receipts = copy.deepcopy(restarted.store.list("cost_reconciliation"))
    restarted.implementations.reconcile()
    assert restarted.store.list("cost_event") == original and restarted.store.list("cost_reconciliation") == receipts


def test_receipt_commands_keep_authority_revision_replay_and_rejections(tmp_path):
    from optimization_framework.contracts.commands import Command
    from optimization_framework.contracts.requests import CampaignUpdate
    from test_framework_provenance import campaign
    workspace, owner, _ = campaign(tmp_path)
    asset = workspace.assets.publish(Asset(id="answer", campaign_id=owner, kind="solution", title="answer",
        costs=[snapshot(workspace.assets, 1, None)], cost_provenance="complete", authority="researcher", created_at="historical"))
    command = Command(id="receipt_once", campaign_id=owner, expected_revision=1, operation="cost.reconcile", payload={
        "asset_id": asset["id"], "source_id": "model:turn", "stop": 1, "quantities": {"model_input_tokens": 20},
        "evidence_ids": [workspace.store.list("cost_snapshot")[0]["id"]], "rationale": "An independently checked provider statement"})
    accepted = workspace.commands.execute(command)
    assert workspace.commands.execute(command) == accepted
    assert quantity(workspace.assets, asset["id"])["total"] == 20
    assert len(workspace.store.list("cost_reconciliation")) == 1
    contradictory = command.model_copy(update={"id": "contradiction", "payload": {**command.payload, "quantities": {"model_input_tokens": 21}}})
    with pytest.raises(ValueError, match="contradicts"):
        workspace.commands.execute(contradictory)
    assert len(workspace.store.list("command_rejection")) == 1
    workspace.update_campaign(owner, CampaignUpdate(name="Updated authority"))
    assert workspace.commands.execute(command) == accepted
    with pytest.raises(ValueError, match="revision"):
        workspace.commands.execute(command.model_copy(update={"id": "stale"}))
    assert len(workspace.store.list("cost_reconciliation")) == 1


def test_imported_executable_reuse_keeps_research_lineage_and_adds_local_work_once(tmp_path):
    from optimization_framework.contracts.commands import Command
    from optimization_framework.contracts.requests import CampaignInput, TaskInput, TrialInput
    from optimization_framework.execution.service import Workspace
    from optimization_framework.implementations.runtime_resolution import resolve, receipt
    from optimization_framework.implementations.service import ImplementationService
    from test_framework_bundles import Client, export, inspect, publish
    from test_framework_evaluators import specification
    from test_framework_provenance import finish
    from test_framework_revalidation import campaign_fixture, no_model
    from test_framework_service_costs import usage
    spec = specification()
    source, service, owner, task = campaign_fixture(tmp_path / "source", spec=spec)
    source.implementations.client = Client(service)
    research = source.store.put("research_run", {"id": "research_origin", "campaign_id": owner["id"], "created_at": "historical",
        "status": "completed", "usage": usage(2, 4)})
    research_cost = research_asset(source.assets, research)
    grant = source.store.list("implementation_grant", owner["id"])[0]
    source.store.put("implementation_grant", {**grant, "upstream_cost_asset_ids": [research_cost["id"]]})
    trial = source.create_trial(TrialInput(campaign_id=owner["id"], task_id=task["id"], algorithm="coordinate", max_steps=3, wall_seconds=10))
    finish(source, trial)
    root = source.store.get(trial["id"], "trial")["output_asset_ids"][0]
    before = source.assets.attributed_costs([root], axes=["model_calls"])
    assert before["quantities"]["model_calls"]["total"] == 3
    _, archive = export(source, owner["id"], [root])
    library = ImplementationService(tmp_path / "destination-library", adapter_factory=no_model)
    client = Client(library)
    client.resolve_runtime = lambda identity, body: resolve(library, identity, body)
    client.runtime_resolution = lambda owner, key: receipt(library, owner, key)
    destination = Workspace(tmp_path / "destination", implementation_client=client)
    campaign = destination.create_campaign(CampaignInput(name="Reuse exact evaluator", compute_budget_seconds=100,
        implementation_compute_budget_seconds=120, validation_reserve_seconds=0,
        tasks=[TaskInput(name="Same problem", problem_id=spec.manifest.id, evaluator_manifest=spec.manifest)]))
    current, task_id = campaign["id"], destination.current_tasks(campaign["id"])[0]["id"]
    publish(destination, current, inspect(destination, current, archive))
    original_events = copy.deepcopy(destination.store.list("cost_event"))
    version = trial["evaluator_version_id"]
    def command(identity, operation, payload):
        return destination.commands.execute(Command(id=identity, campaign_id=current,
            expected_revision=destination.store.get(current, "campaign")["version"], operation=operation, payload=payload))
    resolved = command("resolve", "implementation.resolve_runtime", {"version_id": version})
    assert destination.store.get(resolved["outcome"]["effect_id"], "outbox")["status"] == "completed"
    checked = command("recheck", "implementation.revalidate", {"version_id": version, "compute_seconds": 30,
        "checks": {"kind": "evaluator", "rationale": "Verify imported source in its local runtime"}})
    recheck = destination.store.get(checked["outcome"]["grant_id"], "implementation_grant")
    assert library.run_job(recheck["job_id"])["status"] == "completed"
    destination.implementations.reconcile()
    command("attach", "evaluator.attach", {"task_id": task_id, "version_id": version, "rationale": "Use independently rechecked evidence"})
    later = destination.create_trial(TrialInput(campaign_id=current, task_id=task_id, algorithm="coordinate", max_steps=2, wall_seconds=10))
    destination.evaluators.check_launch(later)
    finish(destination, later)
    answer = destination.store.get(later["id"], "trial")["output_asset_ids"][0]
    costs = destination.assets.attributed_costs([answer], axes=["model_calls", "evaluation_requests", "implementation_seconds"])
    assert costs["quantities"]["model_calls"]["total"] == 3
    assert research_cost["id"] in costs["asset_ids"]
    requests = sum(attempt["report"]["costs"]["evaluation_requests"]
        for producer in (service, library) for job in producer.store.list("implementation_job") for attempt in job["attempts"])
    assert costs["quantities"]["evaluation_requests"]["total"] == requests + 2
    assert costs["accounting_basis"]["status"] == "complete"
    for event in original_events:
        assert destination.store.get(event["id"], "cost_event") == event
    local = destination.assets.actual_costs(current, axes=["model_calls"])
    assert local["quantities"]["model_calls"]["total"] == 0
    resolutions = [row for row in destination.store.list("cost_event", current) if row["source_id"].startswith("runtime:")]
    assert len(resolutions) == 1 and resolutions[0]["quantities"]["worker_seconds"] > 0
    events = copy.deepcopy(destination.store.list("cost_event"))
    first = destination.implementations.cost_asset(client.artifact(version))
    assert destination.implementations.cost_asset(client.artifact(version)) == first
    assert destination.store.list("cost_event") == events
    assert len(library.store.list("implementation_job")) == 1


def test_unsettled_library_jobs_are_refetched_on_events_or_sweep_only(tmp_path):
    from optimization_framework.implementations.service import ImplementationService
    from test_framework_provenance import campaign
    workspace, owner, _ = campaign(tmp_path / "workspace")
    service = ImplementationService(tmp_path / "library")
    fetched = []
    class Client:
        def events(self, **kwargs): return service.events(**kwargs)
        def job(self, identity): fetched.append(identity); return service.store.get(identity, "implementation_job")
    workspace.implementations.client = Client()
    workspace.store.put("implementation_grant", {"id": "grant", "campaign_id": owner, "job_id": "blocked_build",
        "request": {"compute_seconds": 10, "api_budget_usd": 0}, "status": "blocked", "created_at": "historical"})
    service.store.put("implementation_job", {"id": "blocked_build", "campaign_id": owner, "revision": 1,
        "created_at": "historical", "updated_at": "historical", "status": "blocked", "accounting_final": False,
        "usage": {}, "compute_seconds": 0, "attempts": []}, "implementation.progress")
    workspace.implementations.reconcile()
    workspace.implementations.reconcile()
    assert fetched == ["blocked_build"]
    service.update_job("blocked_build", status="failed")
    workspace.implementations.reconcile()
    assert fetched == ["blocked_build", "blocked_build"]
    workspace.implementations._job_sweep_at = 0
    workspace.implementations.reconcile()
    assert len(fetched) == 3
