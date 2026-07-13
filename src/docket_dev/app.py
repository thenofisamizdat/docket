"""Standalone FastAPI app for Docket.

Serves ONLY the Docket surface — the ticket API, the hub login, the telemetry
middleware, and the built React bundle. It imports none of any host application
(no Neo4j / embeddings / etc.), so it boots fast and runs anywhere the package
is installed.
"""

from __future__ import annotations

from importlib.resources import files

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from docket_dev import storage, telemetry
from docket_dev.auth import build_login_router
from docket_dev.roadmap_routes import router as roadmap_router
from docket_dev.routes import router as docket_router


def _dist_dir() -> str:
    return str(files("docket_dev") / "web" / "dist")


def _roadmap_page() -> str:
    return str(files("docket_dev") / "web" / "roadmap.html")


def _build_page() -> str:
    return str(files("docket_dev") / "web" / "build.html")


def create_app() -> FastAPI:
    app = FastAPI(title="Docket", docs_url=None, redoc_url=None)
    storage.init_db()
    app.include_router(docket_router)          # /api/tickets/*
    app.include_router(roadmap_router)         # /api/roadmap/*
    app.include_router(build_login_router())   # /api/testing/login, /api/testing/me
    telemetry.install(app)                     # per-route traffic/error capture (graceful)

    # The roadmap board is a self-contained page (no frontend toolchain needed,
    # same as shipping the prebuilt SPA). Same-origin, so it shares the
    # `testing_token` login cookie with the main board.
    from pathlib import Path as _P
    if _P(_roadmap_page()).is_file():
        @app.get("/roadmap", include_in_schema=False)
        def roadmap_page():
            return FileResponse(_roadmap_page(), media_type="text/html")

    # Run Full Build page — self-contained, same-origin (shares the login cookie).
    if _P(_build_page()).is_file():
        @app.get("/build", include_in_schema=False)
        def build_page():
            return FileResponse(_build_page(), media_type="text/html")

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
