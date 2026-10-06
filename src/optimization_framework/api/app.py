"""Local HTTP application for the researcher-guided grating laboratory."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from functools import lru_cache
import json
import os
from pathlib import Path
import threading
import time
from typing import Literal

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from pydantic import Field
from optimization_framework.implementations.models import ImplementationSpec, Package

from optimization_framework.campaigns.manager import CampaignManager
from optimization_framework.api.compatibility import execute as compatibility_command
from optimization_framework.api.compatibility import prior_request as prior_compatibility_request, resource as compatibility_resource
from optimization_framework.contracts.commands import Command
from optimization_framework.contracts.requests import (Model, CampaignInput, CampaignUpdate, TrialInput, ControlInput, ValidationInput, RecipeInput, StudyInput,
                     HypothesisInput, ReviewInput, DecisionInput, ResearchInput)
from optimization_framework.execution.service import Workspace, ALGORITHMS
from optimization_framework.storage.sqlite import identifier, now
from optimization_framework.research.providers import api_spend


class StatusInput(Model):
    status: Literal["proposed", "investigating", "archived", "finalist"]


class ResearchControl(Model):
    action: Literal["stop", "resume"]


class SourceInput(Model):
    campaign_id: str
    title: str = Field(min_length=1, max_length=1000)
    url: str = Field(min_length=1, max_length=2000)
    excerpt: str = Field(default="", max_length=20000)
    supports: str = Field(default="", max_length=5000)


class SearchInput(Model):
    campaign_id: str
    query: str = Field(min_length=2, max_length=500)
    provider: Literal["arxiv", "crossref"] = "arxiv"
    limit: int = Field(default=5, ge=1, le=10)


class IngestInput(Model):
    campaign_id: str
    identifier: str = Field(min_length=1, max_length=2000)


class VerifyInput(Model):
    n_cells: int = Field(default=8, ge=2, le=128)
    seed: int = Field(default=0, ge=0, le=2**32 - 1)
    compute_seconds: float = Field(default=120, gt=0, le=86400)
    api_budget_usd: float = Field(default=0, ge=0, le=10000)
    idempotency_key: str | None = None


class MemoryInput(Model):
    content: str = Field(min_length=1, max_length=49152)
    expected_revision: int = Field(ge=0)
    reason: str = Field(default="Researcher edited campaign memory", max_length=2000)


class IssueInput(Model):
    choice: Literal["resolved", "deferred"]
    comment: str = Field(default="", max_length=20000)
    expected_revision: int | None = Field(default=None, ge=1)


class BindingInput(Model):
    version_id: str


class CommissionInput(Model):
    spec: ImplementationSpec
    package: Package | None = None
    compute_seconds: float = Field(default=120, gt=0, le=86400)
    max_calls: int = Field(default=12, ge=1, le=20)
    api_budget_usd: float = Field(default=0, ge=0, le=10000)
    idempotency_key: str = Field(min_length=1, max_length=200)


class ImplementationControl(Model):
    action: Literal["cancel", "resume", "close_uncertain"]


def create_app(directory=None, max_workers=2, start_workers=True, implementation_client=None, frontend_directory=None):
    workspace = Workspace(directory or os.environ.get("GRATING_WORKSPACE", "runs/workspace"), max_workers=max_workers,
                          implementation_client=implementation_client)
    from optimization_framework.analysis.tensorboard_view import ScalarExporter, tensorboard_app
    scalar_exporter = ScalarExporter(workspace)
    coordinator = CampaignManager(workspace)
    workspace_id = workspace.store.identity()

    @asynccontextmanager
    async def lifespan(app):
        if start_workers:
            workspace.start()
            scalar_exporter.start()
            app.state.report_writer.recover()
        yield
        if start_workers:
            scalar_exporter.stop()
            workspace.close()

    app = FastAPI(title="Optimization Lab", version="0.2.0", lifespan=lifespan)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.state.workspace = workspace
    app.state.coordinator = coordinator
    app.state.manager = coordinator
    app.state.scalar_exporter = scalar_exporter
    app.mount("/tensorboard", tensorboard_app(scalar_exporter.directory), name="tensorboard")
    from optimization_framework.api.agent_log import install as install_agent_log
    install_agent_log(app, workspace)
    from optimization_framework.agents.api import install as install_pi
    install_pi(app, workspace)
    from optimization_framework.agents.development_api import install as install_development
    install_development(app, workspace)
    from optimization_framework.problem_import.api import install as install_problem_import
    install_problem_import(app, workspace)
    from optimization_framework.api.reports import install as install_reports
    install_reports(app, workspace)

    @app.get("/api/campaigns/{campaign_id}/discovery")
    def discovery(campaign_id: str):
        workspace.store.get(campaign_id, "campaign")
        return workspace.discovery.view(campaign_id)

    @app.get("/api/campaigns/{campaign_id}/models")
    def campaign_models(campaign_id: str):
        return workspace.models.view(campaign_id)

    @app.get("/api/campaigns/{campaign_id}/research-progress")
    def research_progress(campaign_id: str):
        from optimization_framework.research.progress import view
        return view(workspace, campaign_id)

    @app.get("/api/campaigns/{campaign_id}/discovery/assessments/{assessment_id}")
    def discovery_assessment(campaign_id: str, assessment_id: str):
        record = workspace.store.get(assessment_id, "discovery_assessment")
        if record["campaign_id"] != campaign_id:
            raise HTTPException(404, "Assessment not found in this campaign")
        return {"assessment": record, "readiness": workspace.discovery.assessments.readiness(assessment_id),
                "evidence": workspace.discovery.assessments.evidence(assessment_id)}

    @app.middleware("http")
    async def local_mutation_origin(request, call_next):
        # Local dashboard writes must originate from this app or the Vite dev UI.
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and origin:
            from urllib.parse import urlparse
            parsed = urlparse(origin)
            if parsed.netloc != request.headers.get("host") and origin not in {"http://localhost:5173", "http://127.0.0.1:5173"}:
                return JSONResponse({"detail": "Cross-origin write rejected"}, status_code=403)
        expected_workspace = request.headers.get("x-workspace-id")
        if expected_workspace is not None and expected_workspace != workspace_id:
            return JSONResponse({"detail": "The connected workspace changed; refresh before checking or submitting this action"}, status_code=412)
        return await call_next(request)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"detail": "Workspace record not found"}, status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        issue_id = None
        # Operational failures have a durable conversation entry; malformed form
        # fields remain the framework's ordinary 422 response.
        try:
            campaign_id = request.path_params.get("campaign_id") or request.query_params.get("campaign_id")
            try:
                body = await request.json()
            except ValueError:
                body = {}
            campaign_id = campaign_id or (body.get("campaign_id") if isinstance(body, dict) else None)
            segments = request.url.path.split("/")
            if not campaign_id and len(segments) > 3:
                kind = {"hypotheses": "hypothesis", "trials": "trial", "decisions": "decision", "implementation_jobs": "implementation_grant"}.get(segments[2])
                if kind:
                    campaign_id = workspace.store.get(segments[3], kind).get("campaign_id")
            if campaign_id:
                issue_id = workspace.memory.issue(campaign_id, "operation_blocked", str(exc), affected=request.url.path)["id"]
        except (KeyError, ValueError, RuntimeError):
            pass
        return JSONResponse({"detail": str(exc), "issue_id": issue_id}, status_code=409)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "service": "grating-lab", "max_workers": workspace.max_workers}

    @app.get("/api/v1/resources")
    def resource_observations(campaign_id: str | None = None):
        from optimization_framework.execution.observability import get_observer
        return get_observer(workspace).snapshot(campaign_id)

    @app.get("/api/v1/problems")
    def problem_catalog(campaign_id: str | None = None):
        from optimization_framework.evaluation.registry import problems
        definitions = [problems.get(name).describe().model_dump(mode="json") for name in problems.ids()]
        if campaign_id:
            workspace.store.get(campaign_id, "campaign")
            known = {(value["id"], value["version"], value["evaluator_version"]) for value in definitions}
            for task in workspace.store.list("task", campaign_id):
                if task.get("evaluator_manifest"):
                    value = workspace.evaluators.describe_task(workspace.evaluators.task_view(task)).model_dump(mode="json")
                    identity = (value["id"], value["version"], value["evaluator_version"])
                    if identity not in known:
                        definitions.append(value)
                        known.add(identity)
        return {"problems": definitions}

    @app.get("/api/v1/registered-recipes")
    def recipe_catalog():
        from optimization_framework.evaluation.registered_recipes import recipes
        return {"recipes": [recipes.get(identity)[1].model_dump(mode="json") for identity in sorted(recipes.entries())]}

    @app.get("/api/v1/trials/{trial_id}/problem")
    def trial_problem_catalog(trial_id: str):
        return workspace.describe_trial_problem(trial_id)

    @app.get("/api/v1/inference-adapters")
    def inference_catalog():
        from optimization_framework.evaluation.inference import adapters
        return {"adapters": adapters.catalog()}

    @app.get("/api/v1/evaluator-contracts")
    def evaluator_contracts():
        from optimization_framework.implementations.models import EvaluatorSpec, EvaluatorPackage
        from optimization_framework.contracts.evaluators import EvaluatorManifest
        return {"specification": EvaluatorSpec.model_json_schema(), "package": EvaluatorPackage.model_json_schema(),
                "manifest": EvaluatorManifest.model_json_schema()}

    @app.get("/api/v1/campaigns/{campaign_id}/studies")
    def studies(campaign_id: str):
        workspace.store.get(campaign_id, "campaign")
        return workspace.store.list("study", campaign_id)

    @app.get("/api/v1/study-rules")
    def study_rule_catalog():
        from optimization_framework.analysis.rules import catalog
        return {"rules": catalog()}

    @app.get("/api/v1/study-templates")
    def study_templates(task_id: str | None = None):
        from optimization_framework.evaluation.registry import problems
        from optimization_framework.contracts.templates import StudyTemplateVersion
        from optimization_framework.contracts.problems import ProblemInstance
        from optimization_framework.assets.references import input_candidates, matching_sets
        instance = ProblemInstance(**workspace.store.get(task_id, "task")["problem"]) if task_id else None
        entries = []
        for problem_id in problems.ids():
            adapter = problems.get(problem_id)
            for template in adapter.study_templates() if hasattr(adapter, "study_templates") else []:
                parsed = StudyTemplateVersion(**template)
                entries.append({"problem_id": problem_id, "template": parsed.model_dump(mode="json"),
                    "input_candidates": input_candidates(workspace.assets, parsed.input_requirements,
                        instance if instance and instance.definition_id == problem_id else None),
                    "reference_sets": matching_sets(problem_id, parsed.input_requirements)})
        return {"templates": entries}

    @app.get("/api/v1/nominations/{nomination_id}")
    def nomination_evidence(nomination_id: str):
        return workspace.store.get(nomination_id, "nomination")

    @app.get("/api/v1/drafts/{draft_id}")
    def experiment_draft(draft_id: str):
        with workspace.lock, workspace.store.transaction():
            return {"draft": workspace.store.get(draft_id, "experiment_draft"), "readiness": workspace.drafts.readiness(draft_id)}

    @app.get("/api/v1/reproduction-sources")
    def reproduction_sources(campaign_id: str):
        workspace.store.get(campaign_id, "campaign")
        return {"sources": workspace.reproductions.sources(campaign_id)}

    @app.get("/api/v1/reproduction-comparisons/{comparison_id}")
    def reproduction_comparison(comparison_id: str):
        return workspace.store.get(comparison_id, "reproduction_comparison")

    @app.get("/api/v1/study-executions/{execution_id}")
    def study_execution(execution_id: str):
        with workspace.lock, workspace.store.transaction():
            return workspace.study_executions.assess(execution_id)

    @app.get("/api/v1/studies/{study_id}/selection")
    def selection_readiness(study_id: str):
        from optimization_framework.analysis.studies import selection_assessment
        with workspace.lock, workspace.store.transaction():
            return {key: value for key, value in selection_assessment(workspace.store, study_id).items() if key != "evidence"}

    @app.get("/api/v1/studies/{study_id}/race")
    def study_race(study_id: str):
        workspace.store.get(study_id, "study")
        matches = [race for race in workspace.store.list("adaptive_race")
                   if race.get("study_id") == study_id
                   or (race.get("confirmation") or {}).get("study_id") == study_id]
        return {"race": workspace.racing.view(matches[-1]["id"]) if matches else None}

    @app.get("/api/v1/races/{race_id}")
    def adaptive_race(race_id: str):
        return {"race": workspace.racing.view(race_id)}

    @app.post("/api/v1/campaigns/{campaign_id}/studies", status_code=201)
    def create_study(campaign_id: str, body: StudyInput, request: Request):
        outcome = compatibility_command(workspace, request, "study.create", body.model_dump(mode="json"), campaign_id=campaign_id)["outcome"]
        return workspace.store.get(outcome["study_id"], "study")

    @app.get("/api/v1/campaigns/{campaign_id}/comparison")
    def general_comparison(campaign_id: str, study_id: str | None = None, cost_axis: str | None = None, cost_view: str = "full_attributed_cost"):
        from optimization_framework.analysis.general import report
        return report(workspace, campaign_id, study_id=study_id, cost_axis=cost_axis, cost_view=cost_view)

    @app.post("/api/v1/commands")
    def work_command(body: Command):
        # HTTP is the local researcher boundary. Internal agent calls use the
        # manager actor explicitly and cannot choose a stronger authority.
        return workspace.commands.execute(body, actor="researcher")

    @app.get("/api/v1/commands/{command_id}")
    def command_outcome(command_id: str):
        return workspace.store.get(command_id, "work_command")

    @app.get("/api/v1/commands/{command_id}/delivery")
    def command_delivery(command_id: str):
        return workspace.commands.delivery(command_id)

    @app.get("/api/v1/campaigns/{campaign_id}/validations")
    def validation_requirements(campaign_id: str):
        workspace.store.get(campaign_id, "campaign")
        return [workspace.validations.assess(item["id"]) for item in workspace.store.list("validation_requirement", campaign_id)]

    @app.get("/api/v1/confirmations/{protocol_id}")
    def confirmation_assessment(protocol_id: str):
        return workspace.confirmations.assess(protocol_id)

    @app.get("/api/v1/assets")
    def asset_catalog(campaign_id: str | None = None):
        keys = ("id", "campaign_id", "title", "kind", "producer_id", "applicability", "cost_provenance", "exposure_status", "availability", "created_at")
        return [{**{key: asset.get(key) for key in keys}, "availability": workspace.assets.availability(asset)["status"]}
                for asset in workspace.assets.visible(campaign_id)]

    @app.get("/api/v1/assets/{asset_id}")
    def asset_detail(asset_id: str):
        from optimization_framework.storage.history import producers
        from optimization_framework.assets.accounting import sources
        from optimization_framework.assets.service_costs import AXES
        asset = workspace.store.get(asset_id, "asset")
        return {"asset": asset, "full_attributed_cost": workspace.assets.attributed_costs([asset_id], axes=AXES),
                "local_availability": workspace.assets.availability(asset),
                "historical_producers": producers(workspace.store, asset),
                "accounting_sources": sources(workspace.assets, asset_id),
                "decisions": [item for item in workspace.store.list("reuse_decision") if item["asset_id"] == asset_id]}

    @app.post("/api/v1/bundle-uploads")
    async def upload_bundle(request: Request):
        import tempfile
        from optimization_framework.storage.bundles import MAX_BUNDLE_BYTES
        size = 0
        with tempfile.TemporaryFile() as stream:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_BUNDLE_BYTES:
                    raise ValueError("Bundle exceeds the supported byte allowance")
                stream.write(chunk)
            stream.seek(0)
            return await asyncio.to_thread(workspace.bundles.upload, stream)

    @app.get("/api/v1/bundles/operations")
    def bundle_operations(campaign_id: str):
        workspace.store.get(campaign_id, "campaign")
        return workspace.store.list("bundle_operation", campaign_id)

    @app.get("/api/v1/bundles/operations/{operation_id}")
    def bundle_operation(operation_id: str):
        return workspace.store.get(operation_id, "bundle_operation")

    @app.get("/api/v1/bundles/operations/{operation_id}/download")
    def download_bundle(operation_id: str):
        from optimization_framework.contracts.experiments import ArtifactReference
        operation = workspace.store.get(operation_id, "bundle_operation")
        if operation["action"] != "export" or operation["status"] != "completed":
            raise ValueError("Bundle export is not complete")
        reference = ArtifactReference(**operation["archive"])
        workspace.assets.artifacts.verify(reference)
        return FileResponse(workspace.assets.artifacts.resolve(reference), filename=operation["bundle_digest"] + ".zip",
                            media_type="application/vnd.optimization.evidence+zip")

    state_read_lock = threading.Lock()

    @app.get("/api/v1/state")
    @app.get("/api/state")
    def state(campaign_id: str | None = None):
        # Several open browser tabs can request the same multi-megabyte state
        # simultaneously. Coalesce those reads without hiding committed events.
        with state_read_lock:
            with workspace.store.connection() as db:
                if campaign_id:
                    cursor = db.execute("SELECT COALESCE(MAX(id),0) FROM events WHERE campaign_id=?", (campaign_id,)).fetchone()[0]
                else:
                    cursor = db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
            # Provider availability can change without an application event.
            return state_snapshot(campaign_id, cursor, int(time.monotonic() // 30))

    @lru_cache(maxsize=8)
    def state_snapshot(campaign_id: str | None, cursor: int, refresh_window: int):
        from optimization_framework.research.engine import provider_status
        campaigns = workspace.store.list("campaign")
        campaign = workspace.store.get(campaign_id, "campaign") if campaign_id else (campaigns[-1] if campaigns else None)
        current = campaign["id"] if campaign else None
        # Read the cursor before projecting state: a concurrent event then
        # appears on the next refresh instead of being skipped by a stale view.
        recent_events = workspace.store.recent_events(current) if current else []
        provider = workspace.models.config(current, "campaign_manager") if current else provider_status()
        if current and workspace.pi.owns(current):
            config = workspace.pi.configuration(current)
            connection = config.get("provider", {})
            default = (config.get("models") or {}).get("default") or {}
            billing = (connection.get("providers") or {}).get(default.get("provider"), {}).get("billing", "unknown")
            provider = {**provider, **connection, "provider": "pi", "model": default.get("model"),
                "billing_mode": billing, "label": f"Pi / {default.get('provider')}", "llm_family": config.get("llm_family"),
                "enabled": provider.get("enabled", True)}
        response = {"workspace_id": workspace_id, "campaigns": campaigns, "campaign": campaign, "algorithms": ALGORITHMS,
                    "settings": {"llm_configured": provider["configured"], "model": provider["model"],
                                 "provider": provider, "max_workers": workspace.max_workers}}
        if current:
            response["settings"]["model_policy"] = workspace.models.view(current)
        for kind, name in (("task", "tasks"), ("hypothesis", "hypotheses"), ("trial", "trials"),
                           ("decision", "decisions"), ("message", "messages"), ("action", "actions"),
                           ("source", "sources")):
            rows = (workspace.store.list_compact_trials(current) if kind == "trial" else workspace.store.list(kind, current)) if current else []
            if kind == "task":
                rows = [{**workspace.evaluators.task_view(r), "evaluator_readiness": workspace.evaluators.readiness(r)}
                        for r in rows if not r.get("archived")]
            if kind == "hypothesis":
                from optimization_framework.research.discovery.proposals import readiness as proposal_readiness
                rows = [{**h, "implementation_readiness": workspace.implementations.readiness(h),
                         "concept_review": proposal_readiness(workspace.store, h)} for h in rows]
            if kind == "decision":
                from optimization_framework.campaigns.decisions import public_decision
                rows = [public_decision(workspace, row) for row in rows]
            response[name] = rows
        response["research_runs"] = [coordinator.public_run(r) for r in workspace.store.list("research_run", current)] if current else []
        from optimization_framework.research.progress import view as progress_view
        response["research_progress"] = progress_view(workspace, current) if current else None
        response["agent_runtime"] = workspace.pi.view(current) if current else None
        response["source_requests"] = [{key: effect[key] for key in
            ("id", "kind", "status", "query", "identifier", "error", "receipt_id", "created_at") if key in effect}
            for effect in workspace.store.list("outbox", current) if effect["kind"] in {"literature_search", "source_ingest"}] if current else []
        response["events"] = recent_events
        response["event_cursor"] = recent_events[-1]["id"] if recent_events else 0
        study_owners = {study_id: design["execution_id"] for design in workspace.store.list("confirmation_design", current)
                        for study_id in design["study_ids"].values()} if current else {}
        response["studies"] = [{**row, **({"execution_id": study_owners[row["id"]]} if row["id"] in study_owners else {})}
            for row in workspace.store.list("study", current)] if current else []
        response["drafts"] = workspace.store.list("experiment_draft", current) if current else []
        response["draft_launches"] = workspace.store.list("draft_launch", current) if current else []
        response["reproduction_comparisons"] = workspace.store.list("reproduction_comparison", current) if current else []
        response["executable_reuse_decisions"] = [item for item in workspace.store.list("reuse_decision", current)
            if item.get("consequences", {}).get("version_id")] if current else []
        response["study_executions"] = workspace.store.list("study_execution", current) if current else []
        response["nominations"] = [{key: item[key] for key in ("id", "study_id", "rule", "evidence_hash", "result", "selected_method_ids", "prototypes", "created_at")}
            for item in workspace.store.list("nomination", current)] if current else []
        response["finalist_selections"] = workspace.store.list("finalist_selection", current) if current else []
        response["diagnostic_grants"] = [{**{key: grant.get(key) for key in
            ("id", "parent_trial_id", "status", "count", "reserved_seconds", "asset_ids", "trial_ids")},
            "unit": grant["schedule"]["unit"]} for grant in workspace.store.list("diagnostic_grant", current)] if current else []
        if campaign:
            # Polling the UI must not rebuild the manager's full context on
            # every numerical observation. Commands and manager runs call sync
            # before using that context; the state view can read its snapshot.
            memory_state = workspace.memory.state(current)
            context_id = memory_state.get("context_id")
            response["manager_context"] = (workspace.store.get(context_id, "context_revision")
                                           if context_id else workspace.memory.sync(current))
            response["manager_issues"] = [{**issue, "revision": issue.get("revision", issue.get("occurrences", 1))}
                for issue in workspace.store.list("manager_issue", current)]
            response["manager_commands"] = workspace.store.list("manager_command", current)
            from optimization_framework.implementations.bridge import public_grant
            response["implementation_jobs"] = [public_grant(g) for g in workspace.store.list("implementation_grant", current)]
            from optimization_framework.api.reports import public_job
            response["report_writer_jobs"] = [public_job(job) for job in workspace.store.list("report_writer_job", current)]
            response["implementation_library"] = workspace.implementations.catalog()
            from optimization_framework.implementations.references import catalog as reference_catalog
            response["implementation_library"]["references"] = reference_catalog(workspace.store, current)
            assessment = workspace.resources.assessment(current)
            response["budget"] = {"allocated_seconds": assessment["allocated_seconds"],
                "spent_seconds": assessment["actual_seconds"],
                "cap_seconds": campaign["compute_budget_seconds"],
                "llm_spent_usd": sum(api_spend(r.get("usage")) for r in response["research_runs"]) + sum(api_spend(g.get("usage")) for g in response["implementation_jobs"]),
                "implementation_api_committed_usd": workspace.implementations.api_committed(current),
                "implementation_compute_committed_seconds": workspace.implementations.compute_committed(current),
                "implementation_compute_cap_seconds": campaign.get("implementation_compute_budget_seconds", 0),
                "subscription_calls": sum((r.get("usage") or {}).get("subscription_calls", 0) for r in response["research_runs"] + response["implementation_jobs"] + response["report_writer_jobs"]
                    if not r.get("request", {}).get("agent_parent_id")) + sum(a.get("usage", {}).get("calls", 0) for a in workspace.store.list("agent_session", current)),
                "llm_cap_usd": campaign["llm_budget_usd"]}
        return response

    @app.get("/api/events")
    async def events(request: Request, campaign_id: str | None = None, after: int = 0):
        async def stream():
            last = after
            try:
                last = max(last, int(request.headers.get("last-event-id", "0")))
            except ValueError:
                pass
            yield "retry: 2000\n\n"
            heartbeat = 0
            while not await request.is_disconnected():
                rows = workspace.store.events(campaign_id, after=last, limit=500)
                if rows:
                    last = rows[-1]["id"]
                    yield f"id: {last}\nevent: update\ndata: {json.dumps({'last_event_id': last})}\n\n"
                else:
                    heartbeat += 1
                    if heartbeat % 15 == 0:
                        yield ": heartbeat\n\n"
                await asyncio.sleep(1)
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/campaigns", status_code=201)
    def create_campaign(body: CampaignInput, request: Request):
        return compatibility_command(workspace, request, "campaign.create", body.model_dump(mode="json"))["outcome"]["campaign"]

    @app.put("/api/campaigns/{campaign_id}")
    def update_campaign(campaign_id: str, body: CampaignUpdate, request: Request):
        return compatibility_command(workspace, request, "campaign.update", body.model_dump(mode="json", exclude_none=True),
            campaign_id=campaign_id)["outcome"]["campaign"]

    @app.get("/api/campaigns/{campaign_id}/charters")
    def charters(campaign_id: str):
        workspace.store.get(campaign_id, "campaign")
        return workspace.store.list("charter", campaign_id)

    @app.post("/api/trials", status_code=201)
    def create_trial(body: TrialInput, request: Request):
        return compatibility_command(workspace, request, "trial.create", body.model_dump(mode="json"),
            campaign_id=body.campaign_id)["outcome"]["trial"]

    @app.post("/api/trials/{trial_id}/control")
    def control(trial_id: str, body: ControlInput, request: Request):
        trial = workspace.store.get(trial_id, "trial")
        return compatibility_command(workspace, request, "trial.control", {"trial_id": trial_id, **body.model_dump(mode="json")},
            campaign_id=trial["campaign_id"])["outcome"]["trial"]

    @app.post("/api/trials/{trial_id}/validate", status_code=201)
    def validate(trial_id: str, body: ValidationInput, request: Request):
        trial = workspace.store.get(trial_id, "trial")
        return compatibility_command(workspace, request, "trial.validate", {"trial_id": trial_id, **body.model_dump(mode="json")},
            campaign_id=trial["campaign_id"])["outcome"]["trial"]

    @app.post("/api/trials/{trial_id}/recipes", status_code=201)
    def run_recipe(trial_id: str, body: RecipeInput, request: Request):
        trial = workspace.store.get(trial_id, "trial")
        return compatibility_command(workspace, request, "validation.run", {"trial_id": trial_id, **body.model_dump(mode="json")},
            campaign_id=trial["campaign_id"])["outcome"]["trial"]

    @app.get("/api/trials/{trial_id}/metrics")
    def metrics(trial_id: str, limit: int | None = Query(default=None, ge=1, le=1000)):
        return workspace.metrics(trial_id, limit=limit)

    @app.get("/api/trials/{trial_id}/logs")
    def logs(trial_id: str):
        workspace.store.get(trial_id, "trial")
        path = workspace.job_dir(trial_id) / "worker.log"
        return {"text": path.read_text(errors="replace")[-20000:] if path.exists() else ""}

    @app.get("/api/trials/{trial_id}/artifacts/{filename}")
    def artifact(trial_id: str, filename: str):
        trial = workspace.store.get(trial_id, "trial")
        if filename not in {"spec.json", "result.json", "archive.json", "metrics.jsonl", "progress.json"}:
            raise KeyError(filename)
        path = workspace.job_dir(trial_id) / filename
        if filename == "archive.json" and not path.is_file():
            return JSONResponse(trial.get("progress", {}).get("archive", []),
                                headers={"Content-Disposition": 'attachment; filename="archive.json"'})
        if not path.is_file():
            raise KeyError(filename)
        return FileResponse(path, filename=filename)

    @app.post("/api/hypotheses", status_code=201)
    def create_hypothesis(body: HypothesisInput, request: Request):
        return compatibility_command(workspace, request, "hypothesis.create", body.model_dump(mode="json"),
            campaign_id=body.campaign_id)["outcome"]["hypothesis"]

    @app.post("/api/hypotheses/{hypothesis_id}/review")
    def review(hypothesis_id: str, body: ReviewInput, request: Request):
        hypothesis = workspace.store.get(hypothesis_id, "hypothesis")
        return compatibility_command(workspace, request, "hypothesis.review", {"hypothesis_id": hypothesis_id, **body.model_dump(mode="json")},
            campaign_id=hypothesis["campaign_id"])["outcome"]["hypothesis"]

    @app.post("/api/hypotheses/{hypothesis_id}/status")
    def hypothesis_status(hypothesis_id: str, body: StatusInput, request: Request):
        hypothesis = workspace.store.get(hypothesis_id, "hypothesis")
        return compatibility_command(workspace, request, "hypothesis.status", {"hypothesis_id": hypothesis_id, **body.model_dump(mode="json")},
            campaign_id=hypothesis["campaign_id"])["outcome"]["hypothesis"]

    @app.post("/api/hypotheses/{hypothesis_id}/verify", status_code=202, deprecated=True)
    def verify_hypothesis(hypothesis_id: str, body: VerifyInput, request: Request):
        record = workspace.store.get(hypothesis_id, "hypothesis")
        key = body.idempotency_key or request.headers.get("idempotency-key") or "verify_" + hypothesis_id
        previous = prior_compatibility_request(workspace, request, key)
        if previous:
            spec, package = previous["payload"]["spec"], previous["payload"]["package"]
        else:
            if not record.get("source"):
                raise ValueError("Add custom source on a new hypothesis or fork before verifying")
            from optimization_framework.implementations.legacy import import_legacy
            spec, package = import_legacy(record)
            spec, package = spec.model_dump(mode="json"), package.model_dump(mode="json")
        accepted = compatibility_command(workspace, request, "implementation.commission", {
            "hypothesis_id": hypothesis_id, "spec": spec, "package": package, "compute_seconds": body.compute_seconds,
            "api_budget_usd": body.api_budget_usd, "service_idempotency_key": key}, campaign_id=record["campaign_id"], idempotency_key=key)
        return compatibility_resource(workspace, accepted, "implementation_grant", "grant_id")

    @app.get("/api/implementations")
    @app.get("/api/v1/implementations")
    def implementation_catalog(campaign_id: str | None = None, hypothesis_id: str | None = None, task_id: str | None = None):
        catalog = workspace.implementations.catalog(refresh=True)
        if campaign_id:
            from optimization_framework.implementations.reuse import assess
            from optimization_framework.implementations.references import catalog as reference_catalog
            catalog["references"] = reference_catalog(workspace.store, campaign_id)
            catalog["candidates"] = {version["id"]: assess(workspace.implementations, campaign_id, version,
                hypothesis_id=hypothesis_id, task_id=task_id) for version in catalog["versions"]}
        return catalog

    @app.get("/api/v1/implementation-references/{reference_id}")
    def implementation_reference(reference_id: str):
        return workspace.store.get(reference_id, "implementation_reference")

    @app.get("/api/v1/implementations/{version_id}/runtime")
    def implementation_runtime(version_id: str, campaign_id: str):
        workspace.store.get(campaign_id, "campaign")
        return {**workspace.implementations.client.runtime(version_id),
            "receipts": [receipt for receipt in workspace.store.list("runtime_resolution_receipt", campaign_id)
                         if receipt["version_id"] == version_id],
            "operations": [{key: effect.get(key) for key in ("id", "status", "receipt_id", "created_at")}
                           for effect in workspace.store.list("outbox", campaign_id)
                           if effect["kind"] == "implementation_resolve_runtime" and effect["version_id"] == version_id]}

    @app.post("/api/hypotheses/{hypothesis_id}/implementation")
    def attach_implementation(hypothesis_id: str, body: BindingInput, request: Request):
        hypothesis = workspace.store.get(hypothesis_id, "hypothesis")
        accepted = compatibility_command(workspace, request, "implementation.attach", {"hypothesis_id": hypothesis_id, "version_id": body.version_id},
            campaign_id=hypothesis["campaign_id"])
        return compatibility_resource(workspace, accepted, "hypothesis", "hypothesis_id")

    @app.post("/api/hypotheses/{hypothesis_id}/implementation_jobs", status_code=202)
    def commission_implementation(hypothesis_id: str, body: CommissionInput, request: Request):
        hypothesis = workspace.store.get(hypothesis_id, "hypothesis")
        accepted = compatibility_command(workspace, request, "implementation.commission", {"hypothesis_id": hypothesis_id,
            **body.model_dump(mode="json", exclude={"idempotency_key"}), "service_idempotency_key": body.idempotency_key},
            campaign_id=hypothesis["campaign_id"], idempotency_key=body.idempotency_key)
        return compatibility_resource(workspace, accepted, "implementation_grant", "grant_id")

    @app.post("/api/implementation_jobs/{grant_id}/control")
    def implementation_control(grant_id: str, body: ImplementationControl, request: Request):
        grant = workspace.store.get(grant_id, "implementation_grant")
        accepted = compatibility_command(workspace, request, "implementation.control", {"grant_id": grant_id, "action": body.action},
            campaign_id=grant["campaign_id"])
        return compatibility_resource(workspace, accepted, "implementation_grant", "grant_id")

    @app.get("/api/campaigns/{campaign_id}/manager/context")
    def manager_context(campaign_id: str):
        return workspace.memory.sync(campaign_id)

    @app.get("/api/campaigns/{campaign_id}/manager/context/history")
    def manager_context_history(campaign_id: str):
        workspace.store.get(campaign_id, "campaign")
        return workspace.store.list("context_revision", campaign_id)

    @app.put("/api/campaigns/{campaign_id}/manager/context")
    def update_manager_context(campaign_id: str, body: MemoryInput, request: Request):
        outcome = compatibility_command(workspace, request, "context.edit", body.model_dump(mode="json"),
            campaign_id=campaign_id)["outcome"]
        return workspace.store.get(outcome["context_id"], "context_revision")

    @app.post("/api/manager/issues/{issue_id}/resolve")
    def resolve_issue(issue_id: str, body: IssueInput, request: Request):
        issue = workspace.store.get(issue_id, "manager_issue")
        return compatibility_command(workspace, request, "issue.resolve", {"issue_id": issue_id, **body.model_dump(mode="json")},
            campaign_id=issue["campaign_id"])["outcome"]["issue"]

    @app.post("/api/manager/messages", status_code=202)
    def manager_message(body: ResearchInput, request: Request):
        accepted = compatibility_command(workspace, request, "research.start", body.model_dump(mode="json"), campaign_id=body.campaign_id)
        return research_reply(accepted)

    @app.post("/api/research", status_code=202)
    def research(body: ResearchInput, request: Request):
        accepted = compatibility_command(workspace, request, "research.start", body.model_dump(mode="json"), campaign_id=body.campaign_id)
        return research_reply(accepted)

    def research_reply(accepted):
        outcome = accepted["outcome"]
        try:
            record = workspace.store.get(outcome["manager_command_id"], "manager_command")
        except KeyError:
            record = {"id": outcome["manager_command_id"], "status": "queued"}
        if record.get("research_run_id"):
            record = coordinator.public_run(workspace.store.get(record["research_run_id"], "research_run"))
        return {**record, "command_id": accepted["id"], "command_outcome": outcome}

    @app.post("/api/research_runs/{run_id}/control")
    def research_control(run_id: str, body: ResearchControl, request: Request):
        run = workspace.store.get(run_id, "research_run")
        accepted = compatibility_command(workspace, request, "research.control", {"run_id": run_id, "action": body.action}, campaign_id=run["campaign_id"])
        return accepted["outcome"]["research_run"]

    @app.post("/api/decisions/{decision_id}/resolve")
    def decision(decision_id: str, body: DecisionInput, request: Request):
        record = workspace.store.get(decision_id, "decision")
        accepted = compatibility_command(workspace, request, "decision.resolve", {"decision_id": decision_id, **body.model_dump(mode="json")}, campaign_id=record["campaign_id"])
        return compatibility_resource(workspace, accepted, "decision", "decision_id")

    @app.post("/api/sources", status_code=201)
    def source(body: SourceInput, request: Request):
        accepted = compatibility_command(workspace, request, "source.record", body.model_dump(mode="json", exclude={"campaign_id"}), campaign_id=body.campaign_id)
        return accepted["outcome"]["source"]

    @app.post("/api/sources/search")
    def search_sources(body: SearchInput, request: Request):
        accepted = compatibility_command(workspace, request, "literature.search", body.model_dump(mode="json", exclude={"campaign_id"}), campaign_id=body.campaign_id)
        return source_reply(accepted)

    @app.post("/api/sources/ingest")
    def ingest_source(body: IngestInput, request: Request):
        accepted = compatibility_command(workspace, request, "source.ingest", body.model_dump(mode="json", exclude={"campaign_id"}), campaign_id=body.campaign_id)
        return source_reply(accepted, single=True)

    def source_reply(accepted, single=False):
        thread = workspace.source_threads.get(accepted["outcome"]["effect_id"])
        if thread:
            thread.join(timeout=25)
        effect = workspace.store.get(accepted["outcome"]["effect_id"], "outbox")
        if effect["status"] == "failed":
            raise ValueError(effect["error"])
        if not effect.get("receipt_id"):
            return JSONResponse({**accepted["outcome"], "command_id": accepted["id"], "status": effect["status"]}, status_code=202)
        result = workspace.store.get(effect["receipt_id"], "source_retrieval")["result"]
        value = result["sources"][0] if single else result
        return {**value, "command_id": accepted["id"], "command_outcome": accepted["outcome"]}

    @app.get("/api/campaigns/{campaign_id}/analysis")
    def analysis(campaign_id: str):
        workspace.store.get(campaign_id, "campaign")
        from optimization_framework.analysis.comparison import analyze_trials
        trials = workspace.store.list("trial", campaign_id)
        return analyze_trials(trials, {t["id"]: workspace.metrics(t["id"]) for t in trials},
                              workspace.store.list("task", campaign_id))

    @app.get("/api/campaigns/{campaign_id}/export")
    def export(campaign_id: str):
        campaign = workspace.store.get(campaign_id, "campaign")
        markdown = export_markdown(workspace, campaign)
        return Response(markdown, media_type="text/markdown",
                        headers={"Content-Disposition": f'attachment; filename="grating-lab-{campaign_id}.md"'})

    dist = Path(frontend_directory).resolve() if frontend_directory is not None else Path(__file__).resolve().parents[3] / "frontend" / "dist"
    if dist.is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}")
    def frontend(path: str):
        if path.startswith("api/"):
            return JSONResponse({"detail": "API route not found"}, status_code=404)
        if (dist / "index.html").is_file():
            candidate = (dist / path).resolve()
            if candidate.is_relative_to(dist) and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(dist / "index.html")
        return Response("Grating Lab API is running. Build the dashboard with npm ci && npm run build in frontend/.", media_type="text/plain")

    return app


def public_trial(trial):
    return {k: v for k, v in trial.items() if k not in {"pid", "process_identity"}}


def compact_trial(trial):
    """Keep state polling small; full progress and masks remain in artifacts."""
    row = public_trial(trial)
    for field in ("progress", "result"):
        value = row.get(field)
        if not isinstance(value, dict):
            continue
        row[field] = {key: item for key, item in value.items()
                      if key not in {"archive", "best_candidate"}
                      and (field != "result" or key != "best_design" or not row.get("progress"))}
    return row


def export_markdown(workspace, campaign):
    campaign_id = campaign["id"]
    lines = [f"# {campaign['name']}", "", campaign["objective"], "",
        f"Charter version: {campaign['version']}. Exported: {now()}.", "",
        "## Experiment charter", "", "```json", json.dumps(campaign, indent=2), "```", "",
        "## Tasks", ""]
    for task in workspace.store.list("task", campaign_id):
        lines += [f"### {task['name']} ({task['split']})", "", "```json", json.dumps(task, indent=2), "```", ""]
    lines += ["## Hypotheses and evidence", ""]
    for h in workspace.store.list("hypothesis", campaign_id):
        lines += [f"### {h['title']}", "", f"Origin: {h.get('origin', 'unknown')}; status: {h.get('status', 'proposed')}.", "",
                  h.get("mechanism", ""), "", h.get("rationale", ""), "",
                  "```json", json.dumps(h, indent=2), "```", ""]
    lines += ["## Trials", "", "Partial and stopped trials are retained. Search scores are not automatically Fourier-converged.", ""]
    for trial in workspace.store.list("trial", campaign_id):
        lines += [f"### {trial['algorithm']} — {trial['id']}", "", "```json",
                  json.dumps(public_trial(trial), indent=2), "```", ""]
    lines += ["## Research decisions", ""]
    for d in workspace.store.list("decision", campaign_id):
        lines += [f"### {d['title']}", "", d.get("context", ""), "", "```json", json.dumps(d, indent=2), "```", ""]
    lines += ["## Research notebook", ""]
    for message in workspace.store.list("message", campaign_id):
        lines += [f"### {message['role']} · {message['created_at']}", "", message["content"], ""]
    lines += ["## Model execution and accounting", "", "Subscription allowance and paid API charges are recorded separately.", ""]
    for run in workspace.store.list("research_run", campaign_id):
        lines += [f"### {run['id']}", "", "```json", json.dumps({
            "status": run["status"], "provider": run.get("request", {}).get("provider_snapshot"),
            "usage": run.get("usage", {}), "trace": run.get("trace", [])}, indent=2), "```", ""]
    lines += ["## Durable campaign manager context", "", workspace.memory.sync(campaign_id)["document"], ""]
    for kind, title in (("manager_note", "Recorded findings and guidance"), ("manager_issue", "Manager issues"),
                        ("implementation_grant", "Implementation jobs and accounting")):
        lines += [f"## {title}", "", "```json", json.dumps(workspace.store.list(kind, campaign_id), indent=2), "```", ""]
    version_ids = {h.get("implementation_version_id") for h in workspace.store.list("hypothesis", campaign_id)}
    version_ids.update(t.get("implementation_version_id") for t in workspace.store.list("trial", campaign_id))
    versions = [v for v in workspace.store.list("implementation_cache") if v["id"] in version_ids]
    lines += ["## Referenced implementation versions", "", "Implementation validation is distinct from performance evidence.",
              "", "```json", json.dumps(versions, indent=2), "```", ""]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", default="runs/workspace")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workers", type=int, default=2, help="Maximum concurrent numerical trials")
    args = parser.parse_args(argv)
    import uvicorn
    uvicorn.run(create_app(args.directory, max_workers=args.workers), host=args.host, port=args.port,
                timeout_graceful_shutdown=3)


if __name__ == "__main__":
    main()
