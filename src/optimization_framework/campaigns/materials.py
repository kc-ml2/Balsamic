"""Documents attached to a campaign: the context the researcher gives for its problem.

They are kept as the problem importer keeps documents, with the same types and limits: the original
under ``materials/uploads``, and the text extracted on this machine under ``materials/visible/documents``,
which is all the campaign's agents can read (``file_read`` with root ``materials``). What a document
says is the researcher's material, read as data, never as instructions.
"""
import threading

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from optimization_framework.problem_import.api import body
from optimization_framework.problem_import.files import MAX_DOCUMENT_BYTES, ImportFolder, safe_name
from optimization_framework.storage.sqlite import identifier, now

KIND = "campaign_material"
_locks, _guard = {}, threading.Lock()


def folder(workspace, campaign_id):
    return ImportFolder(workspace.directory / "campaigns" / campaign_id / "materials")


def documents(workspace, campaign_id):
    """Where the campaign's agents read the documents' text."""
    return folder(workspace, campaign_id).documents


def listing(workspace, campaign_id):
    workspace.store.get(campaign_id, "campaign")
    return [record for record in workspace.store.list(KIND, campaign_id) if not record.get("removed")]


def _lock(campaign_id):
    with _guard:
        return _locks.setdefault(campaign_id, threading.Lock())


def add(workspace, campaign_id, name, raw):
    """Keep a document and its extracted text. Raises ValueError, safe to show, for a document that is refused."""
    name = safe_name(name)
    # Extracting a long PDF takes a while: it holds this campaign's documents, not the workspace.
    with _lock(campaign_id):
        if any(record["name"] == name for record in listing(workspace, campaign_id)):
            raise ValueError(f"{name} is already attached; remove it first to attach another version")
        described = folder(workspace, campaign_id).create().add_document(name, raw)
        record = {"id": identifier("material"), "schema_version": 1, "campaign_id": campaign_id, **described,
                  "read": {"root": "materials", "path": name + ".txt"}, "added_at": now()}
        workspace.store.put(KIND, record, "material.added")
        return record


def remove(workspace, campaign_id, name):
    with _lock(campaign_id):
        record = next((item for item in listing(workspace, campaign_id) if item["name"] == name), None)
        if record is None:
            raise KeyError(name)
        place = folder(workspace, campaign_id)
        for path in (place.uploads / ("document-" + record["name"]), place.documents / (record["name"] + ".txt")):
            path.unlink(missing_ok=True)
        workspace.store.put(KIND, {**record, "removed": True, "removed_at": now()}, "material.removed")
        return {"removed": record["name"]}


def install(app, workspace):
    def refused(error):
        # A document that is refused is the form's answer, not a campaign issue.
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.get("/api/campaigns/{campaign_id}/materials")
    def materials(campaign_id: str):
        return {"materials": listing(workspace, campaign_id), "limits": {"bytes": MAX_DOCUMENT_BYTES}}

    @app.put("/api/campaigns/{campaign_id}/materials/{name}")
    async def attach(campaign_id: str, name: str, request: Request):
        workspace.store.get(campaign_id, "campaign")
        raw = await body(request, MAX_DOCUMENT_BYTES)
        try:
            # Text extraction runs in a bounded subprocess; it waits here without holding up other requests.
            return await run_in_threadpool(add, workspace, campaign_id, name, raw)
        except ValueError as error:
            return refused(error)

    @app.delete("/api/campaigns/{campaign_id}/materials/{name}")
    def detach(campaign_id: str, name: str):
        return remove(workspace, campaign_id, name)
