"""Public Pi projections and authenticated tool/library callbacks."""
import hmac
import time
from fastapi import Depends, Header, HTTPException
from pydantic import BaseModel, Field
from optimization_framework.contracts.base import content_hash
from .client import service_token
from .tools import PiTools, READ_KINDS
from optimization_framework.problem_import.service import is_import_agent


class Context(BaseModel):
    agent_id: str
    run_id: str


class ToolCall(Context):
    call_id: str
    name: str
    arguments: dict = Field(default_factory=dict)


class Notice(BaseModel):
    agent_ids: list[str] = Field(max_length=1000)


class ImplementationAssignment(BaseModel):
    role: str
    grant_id: str
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    instructions: str = Field(max_length=100000)
    context: dict
    output_schema: dict
    deadline_at: float


def active_implementation_grant(workspace, grant, lead_id):
    if grant["request"].get("agent_parent_id") != lead_id:
        return False
    if grant["status"] not in {"completed", "failed", "cancelled", "closed_uncertain"}:
        return True
    # The library can start a resumed job before its queued state is projected
    # into this workspace. Verify its current ownership and status directly.
    if not grant.get("job_id"):
        return False
    job = workspace.implementations.client.job(grant["job_id"])
    request = job.get("request", {})
    return (job.get("status") in {"queued", "building", "validating", "reviewing", "repairing"}
        and request.get("grant_id") == grant["id"]
        and request.get("workspace_id") == workspace.implementations.workspace_id
        and request.get("campaign_id") == grant["campaign_id"]
        and request.get("agent_parent_id") == lead_id)


def install(app, workspace):
    gateway = PiTools(workspace.pi)

    def authenticate(authorization: str = Header(default="")):
        try:
            expected = "Bearer " + service_token()
        except (ValueError, OSError):
            raise HTTPException(503, "Pi connection is not configured") from None
        if not hmac.compare_digest(authorization, expected):
            raise HTTPException(401, "Pi service authentication required")

    @app.get("/api/campaigns/{campaign_id}/agents")
    def agents(campaign_id: str):
        workspace.store.get(campaign_id, "campaign")
        return workspace.pi.view(campaign_id)

    def harness_models():
        from .families import family_of
        try:
            status = workspace.pi.client.status() or {}
            workspace.pi.harness_status = status
        except (ValueError, OSError):
            status = workspace.pi.harness_status or {}
        models = [{**model, "family": family_of(model["provider"], model["id"])} for model in status.get("models", [])
                  if isinstance(model, dict)]
        return status, models

    @app.get("/api/campaigns/{campaign_id}/agents/models")
    def agent_models(campaign_id: str):
        """Models a researcher may choose in dev mode. An agent may switch only within its own family."""
        from .controller import dev_profile
        workspace.store.get(campaign_id, "campaign")
        status, models = harness_models()
        config = workspace.pi.configuration(campaign_id) or {}
        return {"mode": "dev" if dev_profile() else "locked", "default": (config.get("models") or {}).get("default"),
                "providers": status.get("providers", {}), "models": models}

    def tier_view(status=None, models=None):
        from .controller import dev_profile, profile_defaults
        from .tiers import ASSIGNABLE, load
        if models is None:
            status, models = harness_models()
        return {"mode": "dev" if dev_profile() else "locked", **load(workspace.store, profile_defaults()),
                "assignable": list(ASSIGNABLE), "providers": (status or {}).get("providers", {}), "models": models}

    @app.get("/api/v1/model-tiers")
    def model_tiers():
        return tier_view()

    @app.put("/api/v1/model-tiers")
    def save_model_tiers(values: dict):
        from .controller import dev_profile
        from .tiers import TierSettings, save
        if not dev_profile():
            raise ValueError("Model tiers can be changed only when the Pi harness runs a dev profile")
        settings = TierSettings.model_validate(values)
        status, models = harness_models()
        workspace.pi.harness_status = status
        for tier in settings.tiers:
            workspace.pi._check_available(tier.model.model_dump(exclude={"schema_version"}))
        with workspace.lock, workspace.store.transaction():
            record = save(workspace.store, settings)
            applied = workspace.pi.apply_tiers(record)
        return {**tier_view(status, models), "applied": applied}

    @app.get("/api/v1/llm-usage")
    def llm_usage(campaign_id: str, since: str | None = None, bucket: str = "hour"):
        from . import usage
        campaign = workspace.store.get(campaign_id, "campaign")
        if bucket not in usage.BUCKETS:
            raise HTTPException(422, "bucket must be hour or day")
        config = workspace.pi.configuration(campaign_id) or {}
        summary = usage.summary(workspace.store, campaign_id, since=since, bucket=bucket)
        agent_charged = usage.charged(workspace.store, campaign_id)
        implementation = workspace.implementations.api_committed(campaign_id)
        summary["budget"] = {"cap_usd": campaign.get("llm_budget_usd"), "agent_charged_usd": agent_charged,
            "implementation_committed_usd": implementation, "spent_usd": agent_charged + implementation}
        summary["family"] = config.get("llm_family")
        summary["agents"] = {a["id"]: {"role": a["role"], "status": a["status"], "provider": a.get("provider"),
            "model": a["model"], "effort": a.get("reasoning_effort")} for a in workspace.store.list("agent_session", campaign_id)}
        return summary

    @app.get("/api/campaigns/{campaign_id}/agents/records/{record_id}")
    def agent_record(campaign_id: str, record_id: str):
        entry = workspace.store.get_entry(record_id)
        if entry["kind"] not in READ_KINDS or entry["data"].get("campaign_id") != campaign_id:
            raise HTTPException(404, "Agent evidence is not in this campaign")
        return entry

    @app.post("/api/internal/pi/manifest", dependencies=[Depends(authenticate)])
    def manifest(body: Context):
        # Problem importers are Pi agents without a campaign; they have their own tools.
        if is_import_agent(body.agent_id):
            return workspace.problem_imports.manifest(body.agent_id, body.run_id)
        return gateway.manifest(body.agent_id, body.run_id)

    @app.post("/api/campaigns/{campaign_id}/agents/login")
    def login(campaign_id: str):
        workspace.store.get(campaign_id, "campaign")
        return workspace.pi.client.request("POST", "/v1/auth/start", {})

    @app.post("/api/internal/pi/notify", dependencies=[Depends(authenticate)])
    def notify(body: Notice):
        # A hint only: the controller pulls the agents' authoritative state.
        imports = [agent_id for agent_id in body.agent_ids if is_import_agent(agent_id)]
        if imports:
            workspace.problem_imports.notify(imports)
        return workspace.pi.notify([agent_id for agent_id in body.agent_ids if not is_import_agent(agent_id)])

    @app.post("/api/internal/pi/tool", dependencies=[Depends(authenticate)])
    def tool(body: ToolCall):
        if is_import_agent(body.agent_id):
            return workspace.problem_imports.call(**body.model_dump())
        return gateway.call(**body.model_dump())

    @app.post("/api/internal/pi/implementation", dependencies=[Depends(authenticate)])
    def implementation(request: ImplementationAssignment):
        body = request.model_dump()
        role = body["role"]
        if role not in {"implementation_builder", "implementation_test_designer", "implementation_validator"}:
            raise ValueError("Unsupported implementation role")
        grant = workspace.store.get(body["grant_id"], "implementation_grant")
        campaign_id = grant["campaign_id"]
        config = workspace.pi.configuration(campaign_id)
        if not config or not config["enabled"] or config["status"] != "running":
            raise ValueError("The campaign lead agent is not active")
        if body["deadline_at"] <= time.time():
            raise ValueError("Implementation allocation has expired")
        if not active_implementation_grant(workspace, grant, config["lead_id"]):
            raise ValueError("Implementation assignment is outside the active lead agent's grant")
        frozen_spec = grant["request"]["spec"]
        if body["context"].get("spec", {}).get("mechanism") != frozen_spec.get("mechanism"):
            raise ValueError("Implementation mechanism differs from the frozen grant")
        identity = "agent_impl_" + content_hash([grant["id"], role,
            None if role == "implementation_builder" else body["request_id"]])[:28]
        with workspace.lock, workspace.store.transaction():
            agent = workspace.pi.create_agent(campaign_id, role, body["instructions"], identity,
                parent_id=config["lead_id"], grant_id=grant["id"], output_schema=body["output_schema"])
            # Submitted source was developed in a separate durable Pi session.
            # Its independent design/review calls do not consume numerical
            # execution time; the library enforces that allocation itself.
            review_window = 86400 if grant["request"].get("accounting_mode") == "execution_v1" else grant["request"]["compute_seconds"]
            agent["deadline_at"] = min(body["deadline_at"], time.time() + review_window)
            workspace.store.put("agent_session", agent)
            import json
            run = workspace.pi.enqueue(agent, "pi_impl_run_" + body["request_id"], json.dumps(body["context"]), mode="follow_up")
        return {"agent_id": identity, "run_id": run["id"]}

    @app.get("/api/internal/pi/implementation/{run_id}", dependencies=[Depends(authenticate)])
    def implementation_result(run_id: str):
        run = workspace.store.get(run_id, "agent_run")
        agent = workspace.store.get(run["agent_id"], "agent_session")
        if not agent.get("grant_id"):
            raise ValueError("Not an implementation assignment")
        return {"agent_id": agent["id"], "status": run["status"], "output": run.get("output"), "error": run.get("error"), "usage": agent["usage"]}
