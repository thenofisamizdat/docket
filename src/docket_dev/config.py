"""Per-project configuration for a Docket install.

Everything Docket needs to run against a specific repo lives in that repo's
`.docket/config.toml` (written by `docket init`). This module loads it into a
frozen `Config`, exposes a lazy module-level `CONFIG` proxy that the storage /
auth / telemetry layers read at call time, and `apply_env()` which mirrors the
relevant values into the `DOCKET_*` environment variables the agent already
reads (so `agent.py` stays a near-verbatim copy of the in-repo original).

Resolution order for `CONFIG`:
  1. an explicit object injected via `set_config()` (used by `docket init` before
     the file exists, and by tests), else
  2. `.docket/config.toml` found by walking up from the current directory.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class Config:
    # --- identity / location ---
    project_root: Path            # the target git repo Docket works on
    repo_slug: str                # "owner/name" for GitHub API + compare URLs
    base_branch: str = "main"     # branch worktrees fork from / PRs target
    remote: str = "origin"        # git remote to push branches to

    # --- server ---
    port: int = 8011
    host: str = "0.0.0.0"
    base_url: str = "http://localhost:8011"   # for notification links (no trailing /docket)

    # --- auth ---
    jwt_secret: str = "change-me"
    jwt_algorithm: str = "HS256"
    testers: List[Dict[str, str]] = field(default_factory=list)  # {username,name,email,password}
    user_test_lead: str = ""      # always CC'd on user-review (falls back to first tester)
    default_recipient: str = ""   # fallback notification recipient

    # --- agent ---
    agent_writes: bool = True
    agent_push: bool = True
    agent_auto_merge: bool = False   # squash-merge the PR via API once self-review passes
    # How finished work reaches the base branch:
    #   "pr"          — push a docket/DKT-<id> branch, open a PR, merge on GitHub (default)
    #   "auto_merge"  — same, but squash-merge the PR automatically once self-review passes
    #   "direct_main" — no branch/PR: commit straight onto base_branch (push to the remote
    #                   if one exists, else stay local). Required for repos with no GitHub
    #                   remote (e.g. greenfield projects) and for "build on main anyway".
    agent_dev_mode: str = "pr"
    agent_model: str = "opus"        # default to the strongest model — quality > cost
    agent_strong_model: str = "opus" # model the recovery router escalates to (scope/defect)
    agent_poll_secs: int = 20
    merge_poll_secs: int = 90
    github_token: str = ""

    # --- mail / telemetry ---
    mail_from: str = "Docket <docket@localhost>"
    telemetry_read_extra: str = ""

    # --- derived paths (under .docket/) ---
    @property
    def docket_dir(self) -> Path:
        return self.project_root / ".docket"

    @property
    def db_path(self) -> Path:
        return self.docket_dir / "data" / "docket.db"

    @property
    def telemetry_db(self) -> Path:
        return self.docket_dir / "data" / "telemetry.db"

    @property
    def profile_path(self) -> Path:
        return self.docket_dir / "profile.md"

    @property
    def worktree_dir(self) -> Path:
        return self.docket_dir / "worktrees"

    @property
    def main_checkout(self) -> Path:
        # The agent forks worktrees from the project repo itself.
        return self.project_root


# ---------------------------------------------------------------------------
# Loading / saving
# ---------------------------------------------------------------------------

CONFIG_RELPATH = Path(".docket") / "config.toml"


def find_config_file(start: Optional[Path] = None) -> Optional[Path]:
    """Walk up from `start` (default cwd) looking for .docket/config.toml."""
    cur = (start or Path.cwd()).resolve()
    for d in [cur, *cur.parents]:
        candidate = d / CONFIG_RELPATH
        if candidate.is_file():
            return candidate
    return None


def _from_dict(project_root: Path, data: Dict[str, Any]) -> Config:
    repo = data.get("repo", {})
    server = data.get("server", {})
    auth = data.get("auth", {})
    agent = data.get("agent", {})
    mail = data.get("mail", {})
    tel = data.get("telemetry", {})
    testers = data.get("testers", []) or []
    return Config(
        project_root=project_root,
        repo_slug=repo.get("slug", ""),
        base_branch=repo.get("base_branch", "main"),
        remote=repo.get("remote", "origin"),
        port=int(server.get("port", 8011)),
        host=server.get("host", "0.0.0.0"),
        base_url=server.get("base_url", f"http://localhost:{server.get('port', 8011)}"),
        jwt_secret=auth.get("jwt_secret", "change-me"),
        jwt_algorithm=auth.get("jwt_algorithm", "HS256"),
        testers=list(testers),
        user_test_lead=auth.get("user_test_lead", ""),
        default_recipient=auth.get("default_recipient", ""),
        agent_writes=bool(agent.get("writes", True)),
        agent_push=bool(agent.get("push", True)),
        agent_auto_merge=bool(agent.get("auto_merge", False)),
        # Back-compat: infer the mode from the legacy auto_merge flag when unset.
        agent_dev_mode=agent.get("dev_mode",
                                 "auto_merge" if agent.get("auto_merge") else "pr"),
        agent_model=agent.get("model", "opus"),
        agent_strong_model=agent.get("strong_model", "opus"),
        agent_poll_secs=int(agent.get("poll_secs", 20)),
        merge_poll_secs=int(agent.get("merge_poll_secs", 90)),
        github_token=agent.get("github_token", "") or os.environ.get("DOCKET_GITHUB_TOKEN", ""),
        mail_from=mail.get("from", "Docket <docket@localhost>"),
        telemetry_read_extra=tel.get("read_extra", ""),
    )


def load_config(start: Optional[Path] = None) -> Config:
    path = find_config_file(start)
    if not path:
        raise FileNotFoundError(
            "No .docket/config.toml found. Run `docket init` inside the target repo first."
        )
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    return _from_dict(path.parent.parent, data)


def to_toml(cfg: Config) -> str:
    """Serialize a Config to TOML text (hand-rolled — no tomli-w dependency)."""
    def esc(s: str) -> str:
        return str(s).replace("\\", "\\\\").replace('"', '\\"')

    lines = [
        "# Docket project config — generated by `docket init`. Safe to edit.",
        "",
        "[repo]",
        f'slug = "{esc(cfg.repo_slug)}"',
        f'base_branch = "{esc(cfg.base_branch)}"',
        f'remote = "{esc(cfg.remote)}"',
        "",
        "[server]",
        f"port = {cfg.port}",
        f'host = "{esc(cfg.host)}"',
        f'base_url = "{esc(cfg.base_url)}"',
        "",
        "[auth]",
        f'jwt_secret = "{esc(cfg.jwt_secret)}"',
        f'jwt_algorithm = "{esc(cfg.jwt_algorithm)}"',
        f'user_test_lead = "{esc(cfg.user_test_lead)}"',
        f'default_recipient = "{esc(cfg.default_recipient)}"',
        "",
        "[agent]",
        f"writes = {str(cfg.agent_writes).lower()}",
        f"push = {str(cfg.agent_push).lower()}",
        f"auto_merge = {str(cfg.agent_auto_merge).lower()}",
        f'dev_mode = "{esc(cfg.agent_dev_mode)}"',
        f'model = "{esc(cfg.agent_model)}"',
        f'strong_model = "{esc(cfg.agent_strong_model)}"',
        f"poll_secs = {cfg.agent_poll_secs}",
        f"merge_poll_secs = {cfg.merge_poll_secs}",
        f'github_token = "{esc(cfg.github_token)}"',
        "",
        "[mail]",
        f'from = "{esc(cfg.mail_from)}"',
        "",
        "[telemetry]",
        f'read_extra = "{esc(cfg.telemetry_read_extra)}"',
        "",
    ]
    for t in cfg.testers:
        lines += [
            "[[testers]]",
            f'username = "{esc(t.get("username", ""))}"',
            f'name = "{esc(t.get("name", ""))}"',
            f'email = "{esc(t.get("email", ""))}"',
            f'password = "{esc(t.get("password", ""))}"',
            "",
        ]
    return "\n".join(lines)


def save_config(cfg: Config) -> Path:
    out = cfg.docket_dir / "config.toml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(to_toml(cfg))
    return out


# ---------------------------------------------------------------------------
# Lazy CONFIG proxy + env bridge
# ---------------------------------------------------------------------------

class _LazyConfig:
    """Resolves to an injected Config or the on-disk one on first attribute
    access, then caches. Lets storage/auth/telemetry write `CONFIG.db_path`
    without forcing config to exist at import time."""

    _cfg: Optional[Config] = None

    def _resolve(self) -> Config:
        if _LazyConfig._cfg is None:
            _LazyConfig._cfg = load_config()
        return _LazyConfig._cfg

    def __getattr__(self, name: str) -> Any:
        return getattr(self._resolve(), name)


CONFIG = _LazyConfig()


def set_config(cfg: Config) -> None:
    """Inject the active Config explicitly (used by `docket init` and tests)."""
    _LazyConfig._cfg = cfg


def get_config() -> Config:
    return CONFIG._resolve()


def apply_env(cfg: Config) -> None:
    """Mirror config into the DOCKET_*/TELEMETRY_* env vars the agent reads, so
    `agent.py` can stay a near-verbatim copy of the in-repo module. Call BEFORE
    importing docket_dev.agent."""
    env = {
        "DOCKET_MAIN_CHECKOUT": str(cfg.main_checkout),
        "DOCKET_WORKTREE_DIR": str(cfg.worktree_dir),
        "DOCKET_REPO_SLUG": cfg.repo_slug,
        "DOCKET_AGENT_WRITES": "1" if cfg.agent_writes else "0",
        "DOCKET_AGENT_PUSH": "1" if cfg.agent_push else "0",
        "DOCKET_AGENT_AUTO_MERGE": "1" if cfg.agent_auto_merge else "0",
        "DOCKET_AGENT_DEV_MODE": cfg.agent_dev_mode,
        "DOCKET_AGENT_MODEL": cfg.agent_model,
        "DOCKET_AGENT_STRONG_MODEL": cfg.agent_strong_model,
        "DOCKET_AGENT_POLL": str(cfg.agent_poll_secs),
        "DOCKET_MERGE_POLL": str(cfg.merge_poll_secs),
        "DOCKET_MAIL_FROM": cfg.mail_from,
        "DOCKET_BASE_BRANCH": cfg.base_branch,
        "DOCKET_REMOTE": cfg.remote,
    }
    if cfg.github_token:
        env["DOCKET_GITHUB_TOKEN"] = cfg.github_token
    if cfg.telemetry_read_extra:
        env["TELEMETRY_READ_EXTRA"] = cfg.telemetry_read_extra
    os.environ.update(env)
