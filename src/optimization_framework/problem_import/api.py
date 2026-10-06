"""HTTP routes for problem examples and the problem importer."""
from __future__ import annotations

from typing import Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from .files import MAX_ARCHIVE_BYTES, MAX_DOCUMENT_BYTES
from .service import ImportCreate, ImportMessage, ProblemImports


class CodeSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["git", "folder"]
    url: str = Field(default="", max_length=1000)
    ref: str = Field(default="", max_length=200)
    path: str = Field(default="", max_length=1000)


class SaveExample(BaseModel):
    model_config = ConfigDict(extra="forbid")
    import_id: str | None = None
    example: dict | None = None
    name: str | None = Field(default=None, max_length=200)
    summary: str | None = Field(default=None, max_length=2000)


async def save_body(request: Request, path, limit: int):
    """Stream a large upload to disk instead of holding it in memory."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, f"Upload exceeds the {limit // 1024**2} MB limit")
    size = 0
    with open(path, "wb") as handle:
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                handle.close()
                path.unlink(missing_ok=True)
                raise HTTPException(413, f"Upload exceeds the {limit // 1024**2} MB limit")
            handle.write(chunk)


async def body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, f"Upload exceeds the {limit // 1024**2} MB limit")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, f"Upload exceeds the {limit // 1024**2} MB limit")
        chunks.append(chunk)
    return b"".join(chunks)


def install(app, workspace):
    service = ProblemImports(workspace)
    workspace.problem_imports = service

    @app.get("/api/v1/problem-examples")
    def problem_examples():
        return {"examples": service.examples()}

    @app.post("/api/v1/problem-examples")
    def save_problem_example(values: SaveExample):
        return service.save_example(values.import_id, values.example, values.name, values.summary)

    @app.delete("/api/v1/problem-examples/{example_id}")
    def archive_problem_example(example_id: str):
        return service.archive_example(example_id)

    @app.get("/api/v1/problem-imports/models")
    def import_models():
        return service.models()

    @app.get("/api/v1/problem-imports")
    def problem_imports():
        return {"imports": service.list()}

    @app.post("/api/v1/problem-imports")
    def create_import(values: ImportCreate):
        return service.create(values)

    @app.get("/api/v1/problem-imports/{import_id}")
    def problem_import(import_id: str):
        return service.sync(import_id)

    @app.put("/api/v1/problem-imports/{import_id}/documents/{name}")
    async def upload_document(import_id: str, name: str, request: Request):
        raw = await body(request, MAX_DOCUMENT_BYTES)
        return await run_in_threadpool(service.add_document, import_id, name, raw)

    @app.put("/api/v1/problem-imports/{import_id}/code-archive/{name}")
    async def upload_code_archive(import_id: str, name: str, request: Request):
        upload = service.archive_upload(import_id, name)
        await save_body(request, upload, MAX_ARCHIVE_BYTES)
        return await run_in_threadpool(service.add_code, import_id, "archive", name=name, upload=upload)

    @app.post("/api/v1/problem-imports/{import_id}/code")
    def add_code(import_id: str, source: CodeSource):
        return service.add_code(import_id, source.kind, url=source.url, ref=source.ref, path=source.path)

    @app.post("/api/v1/problem-imports/{import_id}/start")
    def start_import(import_id: str):
        return service.start(import_id)

    @app.post("/api/v1/problem-imports/{import_id}/messages")
    def message_import(import_id: str, values: ImportMessage):
        return service.message(import_id, values)

    @app.post("/api/v1/problem-imports/{import_id}/stop")
    def stop_import(import_id: str):
        return service.stop(import_id)
