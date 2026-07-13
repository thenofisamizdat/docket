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
# control-plane admin config (~/.docket/service.toml)
# ---------------------------------------------------------------------------

def load_service_config() -> Optional[Dict[str, str]]:
    if not SERVICE_TOML.is_file():
        return None
    with open(SERVICE_TOML, "rb") as fh:
        data = tomllib.load(fh)
    admin = data.get("admin", {}) or {}
    if not admin.get("username"):
        return None
    return {"username": admin["username"],
            "password": admin.get("password", ""),
            "jwt_secret": admin.get("jwt_secret", "")}


def ensure_service_config() -> Dict[str, str]:
    """Return the control-plane admin credentials, creating them on first run.
    Password + secret are generated; stored plaintext in ~/.docket/service.toml
    (localhost admin, mirroring how project configs store tester passwords)."""
    existing = load_service_config()
    if existing and existing.get("jwt_secret"):
        return existing
    SERVICE_DIR.mkdir(parents=True, exist_ok=True)
    cfg = {"username": "admin",
           "password": secrets.token_urlsafe(9),
           "jwt_secret": secrets.token_urlsafe(48)}
    SERVICE_TOML.write_text(
        "# Docket control-plane admin — generated on first `docket service`.\n"
        "[admin]\n"
        f'username = "{_esc(cfg["username"])}"\n'
        f'password = "{_esc(cfg["password"])}"\n'
        f'jwt_secret = "{_esc(cfg["jwt_secret"])}"\n')
    return cfg


def launch_project(project: Dict[str, Any]) -> subprocess.CompletedProcess:
    """Start the project's web+agent as background systemd units (reuses the
    existing per-repo unit machinery via `docket up --daemon`)."""
    return subprocess.run(["docket", "up", "--daemon"], cwd=project["root"],
                          capture_output=True, text=True)


def stop_project(project: Dict[str, Any]) -> subprocess.CompletedProcess:
    return subprocess.run(["docket", "down"], cwd=project["root"],
                          capture_output=True, text=True)
