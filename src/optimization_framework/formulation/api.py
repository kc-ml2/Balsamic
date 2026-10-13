"""Routes of the formulation notebook: reading it and running its code. Its cells are changed through the formulation.edit command."""
from fastapi import HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response

from .service import Refusal


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
    def run(campaign_id: str):
        # The run continues in the background; the page reads the notebook again while it is active.
        started = notebook.run(campaign_id, background=True)
        return {key: started[key] for key in ("id", "status", "started_at", "seconds", "notebook_revision")}

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
