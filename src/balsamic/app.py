"""Balsamic's HTTP application: the chat-centred research workspace over the ported scientific core.

Run with ``optimization-lab --directory runs/workspace --port 8765 --workers 2`` (the entry point and flags that scripts/labctl
and deploy/*.json use). Reads are scoped per page with an ``after`` event cursor; writes go through POST /api/v1/commands.
"""
from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
import os
from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.middleware.gzip import GZipMiddleware

from balsamic.workspace import build_workspace


def create_app(directory=None, max_workers=2, start_workers=True, implementation_client=None, frontend_directory=None, gpu_workers=0):
    workspace = build_workspace(directory or os.environ.get("GRATING_WORKSPACE", "runs/workspace"), max_workers=max_workers,
                                implementation_client=implementation_client, gpu_workers=gpu_workers)
    from optimization_framework.analysis.tensorboard_view import ScalarExporter, tensorboard_app
    scalar_exporter = ScalarExporter(workspace)
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

    app = FastAPI(title="Balsamic", version="0.3.0", lifespan=lifespan)
    # zlib's own default level: level 9, Starlette's default, spends 35x longer on a design-heavy response for 12% less.
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)
    app.state.workspace = workspace
    app.state.scalar_exporter = scalar_exporter
    app.mount("/tensorboard", tensorboard_app(scalar_exporter.directory), name="tensorboard")

    @app.middleware("http")
    async def local_mutation_origin(request, call_next):
        # Local workspace writes must originate from this app or the Vite dev UI.
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and origin:
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
            if not campaign_id:
                try:
                    body = await request.json()
                except ValueError:
                    body = {}
                campaign_id = body.get("campaign_id") if isinstance(body, dict) else None
            if campaign_id:
                issue_id = workspace.memory.issue(campaign_id, "operation_blocked", str(exc), affected=request.url.path)["id"]
        except (KeyError, ValueError, RuntimeError):
            pass
        return JSONResponse({"detail": str(exc), "issue_id": issue_id}, status_code=409)

    from optimization_framework.agents.development_api import install as install_development
    install_development(app, workspace)
    from optimization_framework.problem_import.api import install as install_problem_import
    install_problem_import(app, workspace)
    from optimization_framework.problem_charter.api import install as install_problem_charters
    install_problem_charters(app, workspace)

    from balsamic.api import commands, events, campaigns, experiments, files, monitor, threads, notes, evolution, library, setup, telemetry, internal_pi, panels, frontend
    for module in (commands, events, campaigns, experiments, files, monitor, threads, notes, evolution, library, setup, telemetry, internal_pi, panels):
        module.install(app, workspace)
    frontend.install(app, workspace, frontend_directory)   # last: its catch-all serves the page for every address
    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", default="runs/workspace")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workers", type=int, default=2, help="Maximum concurrent numerical trials")
    parser.add_argument("--gpu-workers", type=int, default=0, help="How many of the workers compute on the GPU; the rest compute on CPU")
    parser.add_argument("--frontend-directory", default=None, help="A built frontend (frontend/dist by default)")
    args = parser.parse_args(argv)
    import uvicorn
    uvicorn.run(create_app(args.directory, max_workers=args.workers, frontend_directory=args.frontend_directory, gpu_workers=args.gpu_workers),
                host=args.host, port=args.port, timeout_graceful_shutdown=3)


if __name__ == "__main__":
    main()
