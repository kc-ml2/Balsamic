"""Routes of the formulation notebook: reading it, running its code, and the values of its parameters.

Its cells and the description of its parameters change through the formulation.edit command. A
parameter's value changes here instead: the researcher moves a control many times a minute, and each
move is a new value, not a decision worth a command record.
"""
from fastapi import HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from optimization_framework.contracts.formulation import PARAMETERS, Value

from .service import Refusal

WAIT_SECONDS = 10.


class Values(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: dict[str, Value] = Field(min_length=1, max_length=PARAMETERS)
    run: bool = True
    wait: float = Field(default=3., ge=0, le=WAIT_SECONDS)


def _brief(run):
    return run and {key: run.get(key) for key in ("id", "status", "started_at", "finished_at", "seconds", "elapsed_seconds", "notebook_revision")}


def install(app, workspace):
    notebook = workspace.formulation

    @app.exception_handler(Refusal)
    async def refused(request, exc):
        # A stale revision or a broken notebook rule is an ordinary answer to an edit, not a campaign issue.
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.get("/api/campaigns/{campaign_id}/formulation")
    def formulation(campaign_id: str):
        return notebook.view(campaign_id)

    @app.get("/api/campaigns/{campaign_id}/formulation/cells/{cell_id}/history")
    def cell_history(campaign_id: str, cell_id: str):
        workspace.store.get(campaign_id, "campaign")
        return {"revisions": notebook.history(campaign_id, cell_id)}

    @app.post("/api/campaigns/{campaign_id}/formulation/runs")
    def run(campaign_id: str, wait: float = 0.):
        # The run continues in the background; a short one is answered finished, otherwise the page reads the notebook again while it is active.
        return _brief(notebook.run(campaign_id, background=True, wait=min(max(wait, 0.), WAIT_SECONDS)))

    @app.put("/api/campaigns/{campaign_id}/formulation/values")
    def set_values(campaign_id: str, request: Values):
        """The researcher's controls: save the values, then run the notebook, or once more after the run in progress."""
        outcome = notebook.set_values(campaign_id, request.values)
        started, queued = None, False
        if request.run and any(cell["kind"] == "code" for cell in notebook.view(campaign_id)["cells"]):
            started = notebook.run(campaign_id, background=True, wait=request.wait, queue=True)
            queued = started is None
        return {"outcome": outcome, "run": _brief(started), "queued": queued, "notebook": notebook.view(campaign_id)}

    @app.get("/api/campaigns/{campaign_id}/formulation/runs/{run_id}/files/{name}")
    def figure(campaign_id: str, run_id: str, name: str):
        # A picture drawn by a cell. It is served as an image and nothing else, whatever the file holds.
        return FileResponse(notebook.figure(campaign_id, run_id, name), media_type="image/png", headers={"X-Content-Type-Options": "nosniff"})

    @app.get("/api/campaigns/{campaign_id}/formulation/export")
    def export(campaign_id: str, format: str = "md"):
        if format not in {"md", "py"}:
            raise HTTPException(400, "The notebook exports as Markdown (format=md) or as a marimo notebook (format=py)")
        text = notebook.export_markdown(campaign_id) if format == "md" else notebook.export_marimo(campaign_id)
        return Response(text, media_type="text/markdown; charset=utf-8" if format == "md" else "text/x-python; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="formulation-{campaign_id}.{format}"'})
