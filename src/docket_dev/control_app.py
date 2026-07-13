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
import threading
import uuid
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
    a = _admin()
    try:
        data = jwt.decode(token, a["jwt_secret"], algorithms=[_ALGO])
    except JWTError:
        return None
    if data.get("hub") != _HUB or data.get("sub") not in {x["username"] for x in a["admins"]}:
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
    uname = body.username.strip().lower()
    match = next((x for x in _admin()["admins"]
                  if x["username"] == uname and x["password"] == body.password), None)
    if not match:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid username or password")
    return {"token": _make_token(uname), "username": uname}


# ---------------------------------------------------------------------------
# background jobs (init / groom run for minutes — the hub polls their logs)
# ---------------------------------------------------------------------------

_JOBS: dict = {}
_JOBS_LOCK = threading.Lock()
_JOB_LOG_CAP = 400


def _job_public(job: dict) -> dict:
    with _JOBS_LOCK:
        return {k: (list(v) if k == "lines" else v) for k, v in job.items()}


def _start_job(kind: str, cmd: list, *, cwd: str | None = None,
               project_id: str = "") -> dict:
    job = {"id": uuid.uuid4().hex[:12], "kind": kind, "project_id": project_id,
           "cmd": " ".join(cmd), "status": "running", "rc": None, "lines": [],
           "started_at": datetime.utcnow().isoformat(timespec="seconds")}
    with _JOBS_LOCK:
        _JOBS[job["id"]] = job

    def run():
        try:
            proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1)
            for line in proc.stdout:
                with _JOBS_LOCK:
                    job["lines"].append(line.rstrip())
                    del job["lines"][:-_JOB_LOG_CAP]
            rc = proc.wait()
            with _JOBS_LOCK:
                job["rc"] = rc
                job["status"] = "done" if rc == 0 else "failed"
        except Exception as e:
            with _JOBS_LOCK:
                job["lines"].append(f"error: {e}")
                job["status"] = "failed"

    threading.Thread(target=run, daemon=True).start()
    return _job_public(job)


@app.get("/api/service/jobs")
def list_jobs(admin: dict = Depends(require_admin)):
    with _JOBS_LOCK:
        jobs = sorted(_JOBS.values(), key=lambda j: j["started_at"], reverse=True)[:20]
    return {"jobs": [_job_public(j) for j in jobs]}


@app.get("/api/service/jobs/{job_id}")
def get_job(job_id: str, admin: dict = Depends(require_admin)):
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="unknown job")
    return _job_public(job)


# ---------------------------------------------------------------------------
# projects
# ---------------------------------------------------------------------------

def _with_status(p: dict) -> dict:
    root = p.get("root", "")
    cfg = service.read_project_config(root)
    return {**p, "status": service.project_status(p),
            "url": f"http://localhost:{p.get('port')}",
            "has_config": bool(cfg and cfg.get("jwt_secret")),
            "brief_exists": (Path(root) / "PROJECT_BRIEF.md").is_file(),
            "summary": service.project_summary(root)}


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


class PathIn(BaseModel):
    path: str


@app.post("/api/service/validate-path")
def validate_path(body: PathIn, admin: dict = Depends(require_admin)):
    if not body.path.strip():
        raise HTTPException(status_code=400, detail="path is required")
    return service.inspect_path(body.path.strip())


class InitProjectIn(BaseModel):
    path: str
    dev_mode: str = "pr"
    git_init: bool = False   # `git init` a plain work folder before installing


@app.post("/api/service/projects/init")
def init_existing(body: InitProjectIn, admin: dict = Depends(require_admin)):
    """Install Docket into an existing folder — `docket init` as a background
    job (recognition takes minutes; the card appears as soon as init registers
    the project, before recognition finishes)."""
    if body.dev_mode not in service.DEV_MODES:
        raise HTTPException(status_code=400, detail=f"dev_mode must be one of {service.DEV_MODES}")
    info = service.inspect_path(body.path.strip())
    if not info["exists"]:
        raise HTTPException(status_code=400, detail=f"{info['path']} is not a folder on this machine")
    if info["has_docket"]:
        raise HTTPException(status_code=409, detail="Docket is already installed there")
    if not info["is_git"]:
        if not body.git_init:
            raise HTTPException(status_code=409,
                                detail="not a git repository — tick “initialize git” to set one up")
        r = subprocess.run(["git", "init", "-b", "main"], cwd=info["path"],
                           capture_output=True, text=True)
        if r.returncode != 0:   # older git without -b
            r = subprocess.run(["git", "init"], cwd=info["path"],
                               capture_output=True, text=True)
        if r.returncode != 0:
            raise HTTPException(status_code=500,
                                detail=(r.stderr or "git init failed").strip()[:300])
    job = _start_job("init", ["docket", "init", info["path"], "--dev-mode", body.dev_mode])
    return {"job": job}


def _project_or_404(project_id: str) -> dict:
    p = service.get_project(project_id)
    if not p:
        raise HTTPException(status_code=404, detail="unknown project")
    return p


class SsoIn(BaseModel):
    username: str = ""


@app.post("/api/service/projects/{project_id}/sso")
def sso(project_id: str, body: SsoIn = None, admin: dict = Depends(require_admin)):
    """Mint a tester token for the project so the admin can switch straight into
    its board. Signed with the project's own jwt_secret (read from its
    .docket/config.toml — data, not code); same claim shape as auth.make_token."""
    p = _project_or_404(project_id)
    cfg = service.read_project_config(p["root"])
    if not cfg or not cfg.get("jwt_secret"):
        raise HTTPException(status_code=409,
                            detail="project has no .docket config (legacy install) — open its board and sign in directly")
    username = ((body.username if body else "") or cfg["user_test_lead"] or
                (cfg["testers"][0]["username"] if cfg["testers"] else "")).strip().lower()
    rec = next((t for t in cfg["testers"] if t["username"] == username), None)
    if not rec:
        raise HTTPException(status_code=409, detail="project has no matching tester to sign in as")
    name = rec.get("name") or username.capitalize()
    token = jwt.encode({"sub": username, "name": name, "hub": "testing",
                        "exp": datetime.utcnow() + timedelta(days=7)},
                       cfg["jwt_secret"], algorithm=cfg.get("jwt_algorithm") or "HS256")
    return {"token": token, "username": username, "name": name, "port": p["port"]}


@app.get("/api/service/projects/{project_id}/brief")
def get_brief(project_id: str, admin: dict = Depends(require_admin)):
    p = _project_or_404(project_id)
    f = Path(p["root"]) / "PROJECT_BRIEF.md"
    if not f.is_file():
        raise HTTPException(status_code=404, detail="no PROJECT_BRIEF.md in this project")
    return {"content": f.read_text()}


class BriefIn(BaseModel):
    content: str


@app.put("/api/service/projects/{project_id}/brief")
def put_brief(project_id: str, body: BriefIn, admin: dict = Depends(require_admin)):
    p = _project_or_404(project_id)
    (Path(p["root"]) / "PROJECT_BRIEF.md").write_text(body.content)
    return {"ok": True}


class GroomIn(BaseModel):
    cap: int = 40


@app.post("/api/service/projects/{project_id}/groom")
def groom(project_id: str, body: GroomIn = None, admin: dict = Depends(require_admin)):
    p = _project_or_404(project_id)
    cap = max(1, min((body.cap if body else 40) or 40, 100))
    job = _start_job("groom", ["docket", "groom", "--cap", str(cap)],
                     cwd=p["root"], project_id=p["id"])
    return {"job": job}


@app.delete("/api/service/projects/{project_id}")
def remove(project_id: str, admin: dict = Depends(require_admin)):
    """Unregister from the hub (stops its services first). Never touches the
    project's files — .docket/, tickets and code all stay on disk."""
    p = _project_or_404(project_id)
    service.stop_project(p)
    return {"ok": service.remove_project(project_id)}


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
