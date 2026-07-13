"""Docket service control plane — a registry of projects and thin launcher.

This module is the ONLY part of Docket that spans multiple projects, and it does
so WITHOUT importing any project-scoped code. `config`/`storage`/`agent`/`app`
bind to exactly one repo at import time (process-global `CONFIG` singleton, env
read into `agent` module constants, `storage.init_db()` at import). So the control
plane never imports them — it keeps a small registry file and shells out to the
`docket` CLI (one OS process per project) for everything project-touching.

State lives under ~/.docket/:
  projects.toml   the registry (one [[projects]] table per project)
  service.toml    control-plane admin credentials (written on first `docket service`)

Import surface: stdlib only (+ nothing else). Safe to import anywhere.
"""

from __future__ import annotations

import datetime
import os
import re
import secrets
import socket
import sqlite3
import subprocess
import tomllib
from pathlib import Path
from typing import Any, Dict, List, Optional

SERVICE_DIR = Path(os.environ.get("DOCKET_SERVICE_DIR", str(Path.home() / ".docket")))
PROJECTS_TOML = SERVICE_DIR / "projects.toml"
SERVICE_TOML = SERVICE_DIR / "service.toml"

DASHBOARD_PORT = 8010          # control-plane dashboard
PROJECT_PORT_BASE = 8011       # per-project web ports start here
DEV_MODES = ("pr", "auto_merge", "direct_main")


# ---------------------------------------------------------------------------
# slug / ids
# ---------------------------------------------------------------------------

def slugify(name: str) -> str:
    """A filesystem/unit-safe id from a project name. Matches the systemd unit
    slug rule in cli._unit_names so project_status can find the right unit."""
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s or "project"


def unit_slug(project_id: str) -> str:
    """The `<slug>` in docket-<slug>-web/-agent.service. cli._unit_names derives it
    from (repo_slug or project_root.name).replace('/', '-'); we register projects
    whose id already equals that, so this is identity."""
    return project_id.replace("/", "-")


# ---------------------------------------------------------------------------
# registry read/write (hand-rolled TOML writer — no tomli-w dependency, matching
# config.to_toml's style)
# ---------------------------------------------------------------------------

_FIELDS = ("id", "name", "kind", "root", "port", "dev_mode", "created_at")


def _esc(s: Any) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def load_projects() -> List[Dict[str, Any]]:
    if not PROJECTS_TOML.is_file():
        return []
    with open(PROJECTS_TOML, "rb") as fh:
        data = tomllib.load(fh)
    return list(data.get("projects", []) or [])


def save_projects(projects: List[Dict[str, Any]]) -> Path:
    SERVICE_DIR.mkdir(parents=True, exist_ok=True)
    lines = ["# Docket service registry — managed by `docket new` / the dashboard.", ""]
    for p in projects:
        lines.append("[[projects]]")
        for k in _FIELDS:
            v = p.get(k, "")
            if k == "port":
                lines.append(f"port = {int(v or 0)}")
            else:
                lines.append(f'{k} = "{_esc(v)}"')
        lines.append("")
    PROJECTS_TOML.write_text("\n".join(lines))
    return PROJECTS_TOML


def get_project(project_id: str) -> Optional[Dict[str, Any]]:
    for p in load_projects():
        if p.get("id") == project_id:
            return p
    return None


def register_project(*, id: str, name: str, kind: str, root: str,
                     port: int, dev_mode: str = "pr") -> Dict[str, Any]:
    """Idempotent upsert by id. Returns the stored row."""
    projects = load_projects()
    row = {
        "id": id, "name": name, "kind": kind, "root": str(root),
        "port": int(port), "dev_mode": dev_mode,
        "created_at": datetime.datetime.now(datetime.timezone.utc)
                              .replace(microsecond=0).isoformat(),
    }
    existing = next((p for p in projects if p.get("id") == id), None)
    if existing:
        # Preserve original created_at on update.
        row["created_at"] = existing.get("created_at", row["created_at"])
        projects = [row if p.get("id") == id else p for p in projects]
    else:
        projects.append(row)
    save_projects(projects)
    return row


def remove_project(project_id: str) -> bool:
    projects = load_projects()
    kept = [p for p in projects if p.get("id") != project_id]
    if len(kept) == len(projects):
        return False
    save_projects(kept)
    return True


# ---------------------------------------------------------------------------
# ports
# ---------------------------------------------------------------------------

def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def allocate_port() -> int:
    """Next port above every registered project's port AND actually free. Consults
    the registry (not just a live-socket probe) so a stopped project's port isn't
    handed out to a second project."""
    used = {int(p.get("port") or 0) for p in load_projects()}
    start = max([PROJECT_PORT_BASE - 1, *used]) + 1
    for port in range(start, start + 500):
        if port not in used and _port_free(port):
            return port
    return start


# ---------------------------------------------------------------------------
# launch / stop / status (shell out to the docket CLI, one process per project)
# ---------------------------------------------------------------------------

def _systemctl_active(unit: str) -> Optional[bool]:
    """True/False if systemd knows the unit, None if systemd is unavailable."""
    try:
        r = subprocess.run(["systemctl", "is-active", unit],
                           capture_output=True, text=True)
    except FileNotFoundError:
        return None
    out = (r.stdout or "").strip()
    if out in ("active", "activating"):
        return True
    if out in ("inactive", "failed", "unknown", "deactivating"):
        return False
    return None


def project_status(project: Dict[str, Any]) -> str:
    """'running' | 'stopped' | 'unknown'. Prefers systemd; falls back to a socket
    probe on the project's port when systemd isn't available (dev/non-root)."""
    web_unit = f"docket-{unit_slug(project['id'])}-web.service"
    active = _systemctl_active(web_unit)
    if active is True:
        return "running"
    if active is False:
        # systemd knows it and it's down — but a foreground `docket up` might still
        # be serving, so confirm with a port probe before declaring it stopped.
        return "running" if not _port_free(int(project.get("port") or 0)) else "stopped"
    # systemd unavailable → port probe only.
    port = int(project.get("port") or 0)
    if not port:
        return "unknown"
    return "running" if not _port_free(port) else "stopped"


# ---------------------------------------------------------------------------
# read-only views into a project's own files (config.toml / docket.db)
#
# The control plane may READ per-project files — they're data — it just must
# never IMPORT per-project modules (those bind to one repo at import time).
# ---------------------------------------------------------------------------

def read_project_config(root: str | Path) -> Optional[Dict[str, Any]]:
    """The slice of <root>/.docket/config.toml the hub needs (auth handoff +
    display). None if the project has no package-style config (e.g. a legacy
    standalone install)."""
    path = Path(root) / ".docket" / "config.toml"
    if not path.is_file():
        return None
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except Exception:
        return None
    auth = data.get("auth", {}) or {}
    server = data.get("server", {}) or {}
    testers = [{"username": (t.get("username") or "").strip().lower(),
                "name": t.get("name") or ""}
               for t in (data.get("testers") or []) if t.get("username")]
    return {
        "jwt_secret": auth.get("jwt_secret", ""),
        "jwt_algorithm": auth.get("jwt_algorithm", "HS256"),
        "user_test_lead": (auth.get("user_test_lead") or "").strip().lower(),
        "testers": testers,
        "base_url": server.get("base_url", ""),
    }


_LANES = ("discussion", "queued", "in_progress", "self_review", "pr",
          "user_review", "needs_info", "done", "cancelled")


def project_summary(root: str | Path) -> Optional[Dict[str, Any]]:
    """Ticket lane counts + last activity from the project's docket.db,
    read-only. Falls back to the legacy standalone layout (<root>/data/) so
    pre-package installs still show real counts on the hub. None when no DB
    exists yet or it can't be read."""
    root = Path(root)
    db = root / ".docket" / "data" / "docket.db"
    if not db.is_file():
        db = root / "data" / "docket.db"
    if not db.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.5)
        try:
            rows = con.execute(
                "SELECT status, COUNT(*) FROM tickets GROUP BY status").fetchall()
            last = con.execute("SELECT MAX(updated_at) FROM tickets").fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        return None
    counts = {status: n for status, n in rows}
    return {"total": sum(counts.values()),
            "counts": {k: counts.get(k, 0) for k in _LANES if counts.get(k)},
            "last_activity": last or ""}


def inspect_path(path: str) -> Dict[str, Any]:
    """Pre-flight checks for installing Docket into an existing folder."""
    p = Path(path).expanduser()
    exists = p.is_dir()
    is_git = exists and (p / ".git").exists()
    has_docket = exists and (p / ".docket" / "config.toml").is_file()
    registered = any(Path(pr.get("root", "")) == p.resolve()
                     for pr in load_projects()) if exists else False
    return {"path": str(p), "exists": exists, "is_git": is_git,
            "has_docket": has_docket, "registered": registered}


# ---------------------------------------------------------------------------
# control-plane admin config (~/.docket/service.toml)
# ---------------------------------------------------------------------------

def load_service_config() -> Optional[Dict[str, Any]]:
    """Hub admin accounts. The primary lives in [admin] (back-compat, also holds
    the jwt_secret); extra admins are [[admins]] tables. Returns
    {jwt_secret, admins: [{username, password}, ...]} plus legacy
    username/password keys mirroring the primary admin."""
    if not SERVICE_TOML.is_file():
        return None
    with open(SERVICE_TOML, "rb") as fh:
        data = tomllib.load(fh)
    admin = data.get("admin", {}) or {}
    if not admin.get("username"):
        return None
    admins = [{"username": admin["username"].strip().lower(),
               "password": admin.get("password", "")}]
    admins += [{"username": (a.get("username") or "").strip().lower(),
                "password": a.get("password", "")}
               for a in (data.get("admins") or []) if a.get("username")]
    return {"jwt_secret": admin.get("jwt_secret", ""), "admins": admins,
            "username": admins[0]["username"], "password": admins[0]["password"]}


def _write_service_config(jwt_secret: str, admins: List[Dict[str, str]]) -> None:
    primary, extras = admins[0], admins[1:]
    lines = ["# Docket hub admins — managed by `docket admin add` / first `docket service`.",
             "[admin]",
             f'username = "{_esc(primary["username"])}"',
             f'password = "{_esc(primary["password"])}"',
             f'jwt_secret = "{_esc(jwt_secret)}"', ""]
    for a in extras:
        lines += ["[[admins]]",
                  f'username = "{_esc(a["username"])}"',
                  f'password = "{_esc(a["password"])}"', ""]
    SERVICE_DIR.mkdir(parents=True, exist_ok=True)
    SERVICE_TOML.write_text("\n".join(lines))


def ensure_service_config() -> Dict[str, Any]:
    """Return the control-plane admin credentials, creating them on first run.
    Passwords + secret are generated; stored plaintext in ~/.docket/service.toml
    (localhost admin, mirroring how project configs store tester passwords)."""
    existing = load_service_config()
    if existing and existing.get("jwt_secret"):
        return existing
    _write_service_config(secrets.token_urlsafe(48),
                          [{"username": "admin", "password": secrets.token_urlsafe(9)}])
    return load_service_config()


def add_admin(username: str, password: Optional[str] = None) -> Dict[str, str]:
    """Add a hub admin (or reset an existing one's password). Returns the
    stored {username, password}."""
    cfg = ensure_service_config()
    username = username.strip().lower()
    if not username:
        raise ValueError("username is required")
    password = password or secrets.token_urlsafe(9)
    admins = [a for a in cfg["admins"] if a["username"] != username]
    admins.append({"username": username, "password": password})
    _write_service_config(cfg["jwt_secret"], admins)
    return {"username": username, "password": password}


def launch_project(project: Dict[str, Any]) -> subprocess.CompletedProcess:
    """Start the project's web+agent as background systemd units (reuses the
    existing per-repo unit machinery via `docket up --daemon`)."""
    return subprocess.run(["docket", "up", "--daemon"], cwd=project["root"],
                          capture_output=True, text=True)


def stop_project(project: Dict[str, Any]) -> subprocess.CompletedProcess:
    return subprocess.run(["docket", "down"], cwd=project["root"],
                          capture_output=True, text=True)
