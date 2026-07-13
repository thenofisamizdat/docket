"""Docket control plane — the multi-project dashboard app.

A SECOND FastAPI app, separate from docket_dev.app (which binds to one project).
This one manages the project registry and shells out to the `docket` CLI; it must
NOT import any project-scoped module (config/storage/agent/app/auth) — those bind
to a single repo at import time. It imports only `service` (stdlib) + FastAPI + jose.

Auth is a separate admin login (~/.docket/service.toml), independent of any
project's testers. Bind to 127.0.0.1 by default (see cli.cmd_service).
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel

from docket_dev import service

_ALGO = "HS256"
_HUB = "service"
_TTL = timedelta(days=7)
_WEB = Path(__file__).resolve().parent / "web"

app = FastAPI(title="Docket Service", docs_url=None, redoc_url=None)


# ---------------------------------------------------------------------------
# admin auth
# ---------------------------------------------------------------------------

def _admin() -> dict:
    return service.ensure_service_config()


def _make_token(username: str) -> str:
    payload = {"sub": username, "hub": _HUB, "exp": datetime.utcnow() + _TTL}
    return jwt.encode(payload, _admin()["jwt_secret"], algorithm=_ALGO)


def _verify(token: str) -> Optional[dict]:
    if not token:
        return None
    try:
        data = jwt.decode(token, _admin()["jwt_secret"], algorithms=[_ALGO])
    except JWTError:
        return None
    if data.get("hub") != _HUB or data.get("sub") != _admin()["username"]:
        return None
    return {"username": data["sub"]}


_security = HTTPBearer(auto_error=False)


def require_admin(request: Request,
                  credentials: Optional[HTTPAuthorizationCredentials] = Depends(_security)) -> dict:
    token = None
    if credentials and credentials.credentials:
        token = credentials.credentials
    elif request is not None:
        token = request.cookies.get("service_token")
    admin = _verify(token) if token else None
    if not admin:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Sign in to the Docket service")
    return admin


class LoginIn(BaseModel):
    username: str
    password: str


@app.post("/api/service/login")
def login(body: LoginIn):
    a = _admin()
    if body.username.strip() != a["username"] or body.password != a["password"]:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid username or password")
    return {"token": _make_token(a["username"]), "username": a["username"]}


# ---------------------------------------------------------------------------
# projects
# ---------------------------------------------------------------------------

def _with_status(p: dict) -> dict:
    return {**p, "status": service.project_status(p),
            "url": f"http://localhost:{p.get('port')}"}


@app.get("/api/service/projects")
def list_projects(admin: dict = Depends(require_admin)):
    return {"projects": [_with_status(p) for p in service.load_projects()]}


class NewProjectIn(BaseModel):
    name: str
    path: str = ""
    dev_mode: str = "direct_main"


@app.post("/api/service/projects")
def create_project(body: NewProjectIn, admin: dict = Depends(require_admin)):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if body.dev_mode not in service.DEV_MODES:
        raise HTTPException(status_code=400, detail=f"dev_mode must be one of {service.DEV_MODES}")
    cmd = ["docket", "new", name, "--dev-mode", body.dev_mode]
    if body.path.strip():
        cmd += ["--path", body.path.strip()]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise HTTPException(status_code=500,
                            detail=(r.stderr or r.stdout or "docket new failed").strip()[:500])
    proj = service.get_project(service.slugify(name))
    if not proj:
        raise HTTPException(status_code=500, detail="project created but not found in registry")
    return {"project": _with_status(proj)}


def _project_or_404(project_id: str) -> dict:
    p = service.get_project(project_id)
    if not p:
        raise HTTPException(status_code=404, detail="unknown project")
    return p


@app.post("/api/service/projects/{project_id}/launch")
def launch(project_id: str, admin: dict = Depends(require_admin)):
    p = _project_or_404(project_id)
    r = service.launch_project(p)
    return {"ok": r.returncode == 0,
            "detail": (r.stdout or r.stderr or "").strip()[-500:],
            "project": _with_status(p)}


@app.post("/api/service/projects/{project_id}/stop")
def stop(project_id: str, admin: dict = Depends(require_admin)):
    p = _project_or_404(project_id)
    r = service.stop_project(p)
    return {"ok": r.returncode == 0,
            "detail": (r.stdout or r.stderr or "").strip()[-500:],
            "project": _with_status(p)}


# ---------------------------------------------------------------------------
# dashboard page
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def dashboard():
    page = _WEB / "service.html"
    if not page.is_file():
        return {"error": "service.html not found"}
    return FileResponse(str(page), media_type="text/html")
