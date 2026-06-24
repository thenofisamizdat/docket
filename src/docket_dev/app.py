"""Standalone FastAPI app for Docket.

Serves ONLY the Docket surface — the ticket API, the hub login, the telemetry
middleware, and the built React bundle. It imports none of any host application
(no Neo4j / embeddings / etc.), so it boots fast and runs anywhere the package
is installed.
"""

from __future__ import annotations

from importlib.resources import files

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from docket_dev import storage, telemetry
from docket_dev.auth import build_login_router
from docket_dev.routes import router as docket_router


def _dist_dir() -> str:
    return str(files("docket_dev") / "web" / "dist")


def create_app() -> FastAPI:
    app = FastAPI(title="Docket", docs_url=None, redoc_url=None)
    storage.init_db()
    app.include_router(docket_router)          # /api/tickets/*
    app.include_router(build_login_router())   # /api/testing/login, /api/testing/me
    telemetry.install(app)                     # per-route traffic/error capture (graceful)

    # Mounted last so the API routes above take precedence. html=True serves
    # index.html for the SPA; the bundle is built with base="/docket/". Guarded
    # so the API still boots if the bundle hasn't been built into the package.
    from pathlib import Path
    dist = _dist_dir()
    if Path(dist).is_dir():
        app.mount("/docket", StaticFiles(directory=dist, html=True), name="docket")
    return app


# Module-level app for `uvicorn docket_dev.app:app`.
app = create_app()
