"""`docket` — install and run Docket against any git repo.

Commands:
  docket init       detect the repo, write .docket/config.toml, init the DB,
                    and recognize the codebase (profile + CLAUDE.md + seed tickets)
  docket up         run the web UI + agent together (foreground; --daemon for systemd)
  docket serve      run just the web UI
  docket agent      run just the autonomous agent loop
  docket recognize  (re)generate the codebase profile + CLAUDE.md
  docket seed       (re)scan the repo and draft starter tickets
  docket status     show config + service/ticket status
"""

from __future__ import annotations

import argparse
import secrets
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

from docket_dev import config as cfgmod
from docket_dev.config import Config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git(args, cwd: Path) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def _repo_root(start: Path) -> Optional[Path]:
    top = _git(["rev-parse", "--show-toplevel"], start)
    return Path(top) if top else None


def _slug_from_remote(root: Path, remote: str) -> str:
    url = _git(["remote", "get-url", remote], root)
    if not url:
        return ""
    # git@github.com:owner/name.git  or  https://github.com/owner/name(.git)
    m = url.split("github.com")
    if len(m) < 2:
        return ""
    tail = m[1].lstrip(":/").removesuffix(".git")
    return tail


def _base_branch(root: Path, remote: str) -> str:
    ref = _git(["symbolic-ref", f"refs/remotes/{remote}/HEAD"], root)
    if ref:
        return ref.rsplit("/", 1)[-1]
    cur = _git(["rev-parse", "--abbrev-ref", "HEAD"], root)
    return cur or "main"


def _free_port(start: int = 8011) -> int:
    for port in range(start, start + 200):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start


def _warn_missing_tools() -> None:
    for tool in ("claude", "git", "msmtp"):
        if not shutil.which(tool):
            note = "(needed for the agent)" if tool == "claude" else \
                   "(needed for email notifications)" if tool == "msmtp" else ""
            print(f"  ! '{tool}' not found on PATH {note}".rstrip())


def _activity(msg: str) -> None:
    print(f"    · {msg}", flush=True)


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

def cmd_init(args) -> int:
    start = Path(args.path).resolve() if args.path else Path.cwd()
    root = _repo_root(start)
    if not root:
        print(f"error: {start} is not inside a git repository.", file=sys.stderr)
        return 2

    existing = root / ".docket" / "config.toml"
    if existing.exists() and not args.force:
        print(f"Docket is already initialized at {existing} (use --force to overwrite).")
        return 1

    remote = args.remote
    slug = args.slug or _slug_from_remote(root, remote)
    branch = args.base_branch or _base_branch(root, remote)
    port = args.port or _free_port()
    base_url = args.base_url or f"http://localhost:{port}"

    username = (args.user or _git(["config", "user.name"], root).split()[:1] or ["dev"])[0].lower()
    username = "".join(ch for ch in username if ch.isalnum()) or "dev"
    email = args.email or _git(["config", "user.email"], root)
    password = args.password or "testing"

    cfg = Config(
        project_root=root,
        repo_slug=slug,
        base_branch=branch,
        remote=remote,
        port=port,
        base_url=base_url,
        jwt_secret=secrets.token_urlsafe(48),
        testers=[{"username": username, "name": username.capitalize(),
                  "email": email, "password": password}],
        user_test_lead=username,
        default_recipient=username,
        agent_writes=not args.no_writes,
        agent_push=(not args.no_writes) and (not args.no_push),
        agent_auto_merge=(args.dev_mode == "auto_merge"),
        agent_dev_mode=args.dev_mode,
        agent_model=args.model,
    )
    cfgmod.set_config(cfg)
    out = cfgmod.save_config(cfg)

    # Keep .docket out of the target repo's history.
    gi = root / ".gitignore"
    line = ".docket/"
    if not gi.exists() or line not in gi.read_text().splitlines():
        with open(gi, "a") as fh:
            fh.write(("" if not gi.exists() or gi.read_text().endswith("\n") else "\n")
                     + line + "\n")

    from docket_dev import storage
    storage.init_db()

    # Register with the service control plane so this repo shows on the dashboard.
    try:
        from docket_dev import service
        service.register_project(
            id=service.unit_slug(slug or root.name), name=slug or root.name,
            kind="existing", root=str(root), port=port, dev_mode=args.dev_mode)
    except Exception as e:
        print(f"  (note: could not register with the service dashboard: {e})")

    print(f"Docket initialized for {slug or root.name}")
    print(f"  config:  {out}")
    print(f"  repo:    {root}  (branch {branch}, remote {remote})")
    print(f"  login:   {username} / {password}")
    print(f"  url:     {base_url}/docket  (port {port})")
    _warn_missing_tools()

    if not slug:
        print("  ! No GitHub slug detected — set [repo].slug in config.toml for PR/merge features.")

    if not args.no_recognize:
        if not shutil.which("claude"):
            print("  skipping recognition — 'claude' CLI not found.")
        else:
            _recognize(profile=True, claude_md=True, seed=True, seed_limit=args.seed_limit)

    print("\nNext:  docket up    # start the web UI + agent")
    return 0


# ---------------------------------------------------------------------------
# recognize / seed
# ---------------------------------------------------------------------------

def _recognize(*, profile: bool, claude_md: bool, seed: bool, seed_limit: int) -> None:
    from docket_dev import recognize
    if profile:
        print("  recognizing codebase (profile)...")
        p = recognize.profile_repo(on_activity=_activity)
        print(f"    → wrote {p}")
    if claude_md:
        print("  generating CLAUDE.md (if absent)...")
        wrote = recognize.ensure_claude_md(on_activity=_activity)
        print("    → wrote CLAUDE.md" if wrote else "    → CLAUDE.md already exists, skipped")
    if seed:
        print(f"  seeding starter tickets (up to {seed_limit})...")
        created = recognize.seed_tickets(limit=seed_limit, on_activity=_activity)
        print(f"    → drafted {len(created)} tickets into Discussion")
        for t in created:
            print(f"        {t['ref']}  {t['title']}")


def _load_or_die() -> Config:
    try:
        cfg = cfgmod.load_config()
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
    cfgmod.set_config(cfg)
    cfgmod.apply_env(cfg)
    return cfg


def cmd_recognize(args) -> int:
    _load_or_die()
    _recognize(profile=True, claude_md=True, seed=False, seed_limit=args.seed_limit)
    return 0


def cmd_seed(args) -> int:
    _load_or_die()
    _recognize(profile=False, claude_md=False, seed=True, seed_limit=args.seed_limit)
    return 0


# ---------------------------------------------------------------------------
# new (greenfield) + groom
# ---------------------------------------------------------------------------

PROJECT_BRIEF_TEMPLATE = """\
# Project Brief — {name}

<!--
  Fill in every "USER:" section below, then run `docket groom` (or click Groom on
  the dashboard). Docket feeds this brief to Claude, which grooms it into an
  ordered backlog that builds the whole project. The more concrete you are here,
  the better the tickets — vague briefs make vague tickets.
-->

## One-liner
<!-- USER: one sentence — what is this and who is it for? -->

## Problem / goal
<!-- USER: what problem does it solve? what does success look like? -->

## Target users
<!-- USER: who uses it, and in what context? -->

## Core features
<!-- USER: list them, grouped by importance. Be specific about behaviour. -->
### Must have
-
### Should have
-
### Could have
-

## Tech stack
<!-- USER: languages/frameworks/db/hosting. If unsure, say so and suggest a default
     (e.g. "Python + FastAPI + SQLite + a small React frontend"). -->

## Data / entities
<!-- USER: the main things the app stores and their key fields/relationships. -->

## Integrations
<!-- USER: external APIs/services/auth providers, if any. -->

## Constraints
<!-- USER: performance, security, compliance, budget, deadlines, must-use tech. -->

## Non-goals
<!-- USER: explicitly out of scope, so the agent doesn't build it. -->

## UX / design notes
<!-- USER: look & feel, key screens/flows, accessibility. -->

## Success criteria
<!-- USER: how you'll judge "done" — observable, testable outcomes. -->
"""


def _git_run(args, cwd: Path) -> bool:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    return r.returncode == 0


def _init_repo(path: Path) -> None:
    """git init a fresh repo on branch `main` (deterministic base branch)."""
    if not _git_run(["init", "-b", "main"], path):        # older git: no -b
        _git_run(["init"], path)
        _git_run(["symbolic-ref", "HEAD", "refs/heads/main"], path)


def cmd_new(args) -> int:
    from docket_dev import service
    name = args.name.strip()
    if not name:
        print("error: project name is required.", file=sys.stderr)
        return 2
    slug = service.slugify(name)
    path = Path(args.path).resolve() if args.path else (Path.cwd() / slug)
    if path.exists() and any(path.iterdir()):
        print(f"error: {path} exists and is not empty.", file=sys.stderr)
        return 2
    path.mkdir(parents=True, exist_ok=True)
    _init_repo(path)

    # Greenfield git identity — the agent's `git commit` fails without one.
    username = (args.user or _git(["config", "user.name"], path).split()[:1] or ["dev"])[0].lower()
    username = "".join(ch for ch in username if ch.isalnum()) or "dev"
    email = args.email or _git(["config", "user.email"], path) or "docket@localhost"
    _git_run(["config", "user.name", args.user or username], path)
    _git_run(["config", "user.email", email], path)
    password = args.password or "testing"

    port = args.port or service.allocate_port()
    cfg = Config(
        project_root=path,
        repo_slug="",                       # no GitHub remote yet
        base_branch="main",
        remote="origin",
        port=port,
        base_url=f"http://localhost:{port}",
        jwt_secret=secrets.token_urlsafe(48),
        testers=[{"username": username, "name": username.capitalize(),
                  "email": email, "password": password}],
        user_test_lead=username,
        default_recipient=username,
        agent_writes=True,
        agent_push=False,                   # nothing to push to (no remote)
        agent_dev_mode=args.dev_mode,       # default direct_main (see parser)
        agent_model=args.model,
    )
    cfgmod.set_config(cfg)
    out = cfgmod.save_config(cfg)

    gi = path / ".gitignore"
    gi.write_text(".docket/\n" + (gi.read_text() if gi.exists() else ""))

    from docket_dev import storage
    storage.init_db()

    # The brief is the starting point; a lightweight CLAUDE.md grounds the first
    # scaffolding ticket (profile.md is skipped — an empty repo yields nothing;
    # run `docket recognize` after scaffolding to generate it from real code).
    (path / "PROJECT_BRIEF.md").write_text(PROJECT_BRIEF_TEMPLATE.format(name=name))
    claude_md = path / "CLAUDE.md"
    if not claude_md.exists():
        claude_md.write_text(
            f"# {name}\n\nBrand-new project scaffolded by Docket. The authoritative "
            "spec is `PROJECT_BRIEF.md` — read it before building. Tickets are built "
            "one at a time on `main` (no PRs); keep changes small and self-contained.\n")

    # Initial commit — gives direct_main a real base SHA to diff against.
    _git_run(["add", "-A"], path)
    _git_run(["commit", "-m", "Docket: initial project brief + scaffolding stub"], path)

    service.register_project(id=slug, name=name, kind="greenfield",
                             root=str(path), port=port, dev_mode=args.dev_mode)

    print(f"Created greenfield project '{name}'")
    print(f"  path:     {path}")
    print(f"  config:   {out}  (dev_mode={args.dev_mode}, port {port})")
    print(f"  login:    {username} / {password}")
    print(f"\nNext:")
    print(f"  1. Edit {path / 'PROJECT_BRIEF.md'} — fill in every USER: section.")
    print(f"  2. cd {path} && docket groom     # groom the brief into an ordered backlog")
    print(f"  3. docket up                      # start the board + agent, then Run Full Build")
    return 0


def cmd_groom(args) -> int:
    cfg = _load_or_die()
    from docket_dev import recognize
    brief_path = cfg.project_root / "PROJECT_BRIEF.md"
    if not brief_path.is_file():
        print(f"error: no PROJECT_BRIEF.md at {brief_path}. Run `docket new` first.",
              file=sys.stderr)
        return 2
    if not shutil.which("claude"):
        print("error: 'claude' CLI not found — grooming needs it.", file=sys.stderr)
        return 2
    print("  grooming PROJECT_BRIEF.md into an ordered backlog...")
    created = recognize.groom_brief(brief_path.read_text(), cap=args.cap,
                                    on_activity=_activity)
    print(f"    → drafted {len(created)} tickets into Discussion (build order):")
    for t in created:
        print(f"        {t['ref']}  {t['title']}")
    if created:
        print("\nNext:  docket up, then open /build and click Run Full Build.")
    return 0


# ---------------------------------------------------------------------------
# serve / agent / up
# ---------------------------------------------------------------------------

def _run_web(cfg: Config) -> None:
    import uvicorn
    from docket_dev.app import app
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")


def _run_agent() -> None:
    from docket_dev import agent
    agent.main()


def cmd_serve(args) -> int:
    cfg = _load_or_die()
    print(f"Docket web at {cfg.base_url}/docket  (Ctrl-C to stop)")
    _run_web(cfg)
    return 0


def cmd_agent(args) -> int:
    _load_or_die()
    from docket_dev import agent
    if getattr(args, "once", False):
        ran = agent.run_once()
        print("worked one ticket" if ran else "queue empty")
        return 0
    agent.main()
    return 0


def cmd_up(args) -> int:
    cfg = _load_or_die()
    if args.daemon:
        return _install_units(cfg)
    if not shutil.which("claude"):
        print("  ! 'claude' not found — the agent won't be able to work tickets.")
    print(f"Docket up (foreground): web {cfg.base_url}/docket + agent")
    print("  Ctrl-C to stop. To run in the background as a service: docket up --daemon")
    threading.Thread(target=_run_agent, daemon=True).start()
    _run_web(cfg)
    return 0


def cmd_down(args) -> int:
    cfg = _load_or_die()
    return _stop_units(cfg)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def cmd_status(args) -> int:
    cfg = _load_or_die()
    from docket_dev import storage
    print(f"Docket — {cfg.repo_slug or cfg.project_root.name}")
    print(f"  repo:    {cfg.project_root} (branch {cfg.base_branch}, remote {cfg.remote})")
    print(f"  url:     {cfg.base_url}/docket")
    print(f"  db:      {cfg.db_path}  ({'exists' if cfg.db_path.exists() else 'not created'})")
    print(f"  writes:  {'on' if cfg.agent_writes else 'off'}   model: {cfg.agent_model}")
    try:
        counts: dict = {}
        for t in storage.list_tickets():
            counts[t["status"]] = counts.get(t["status"], 0) + 1
        total = sum(counts.values())
        print(f"  tickets: {total} total" + (f"  ({', '.join(f'{k}:{v}' for k,v in sorted(counts.items()))})" if total else ""))
    except Exception as e:
        print(f"  tickets: (unreadable: {e})")
    return 0


# ---------------------------------------------------------------------------
# service control plane
# ---------------------------------------------------------------------------

def cmd_service(args) -> int:
    """Run the multi-project control-plane dashboard (does NOT load any single
    project — it manages the registry and shells out to per-project `docket`)."""
    import uvicorn
    from docket_dev import service
    from docket_dev.control_app import app
    host = args.host or "127.0.0.1"
    port = args.port or service.DASHBOARD_PORT
    if getattr(args, "daemon", False):
        return _install_service_unit(host, port)
    print(f"Docket service dashboard at http://{host}:{port}  (Ctrl-C to stop)")
    print(f"  registry: {service.PROJECTS_TOML}")
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


def _install_service_unit(host: str, port: int) -> int:
    """Install + start the hub as a systemd unit (docket-service.service), so
    :8010 survives reboots instead of living in a stray terminal."""
    import getpass
    exe = shutil.which("docket") or str(Path(sys.executable).parent / "docket")
    # The hub shells out to `docket` (init/groom/launch jobs), which in turn
    # shells out to `claude` and `git` — give the unit a PATH with all of them.
    claude = shutil.which("claude")
    pathparts = [str(Path(exe).parent)]
    if claude:
        pathparts.append(str(Path(claude).parent))
    pathparts += ["/usr/local/bin", "/usr/bin", "/bin"]
    path = ":".join(dict.fromkeys(pathparts))
    body = (f"[Unit]\nDescription=Docket hub — multi-project control plane\n"
            f"After=network.target\n\n"
            f"[Service]\nType=simple\nUser={getpass.getuser()}\n"
            f'Environment="HOME={Path.home()}"\nEnvironment="PATH={path}"\n'
            f"WorkingDirectory={Path.home()}\n"
            f"ExecStart={exe} service --host {host} --port {port}\n"
            f"Restart=on-failure\nRestartSec=5\n\n"
            f"[Install]\nWantedBy=multi-user.target\n")
    unit = "docket-service.service"
    try:
        (Path("/etc/systemd/system") / unit).write_text(body)
    except PermissionError:
        staged = Path.home() / ".docket" / unit
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text(body)
        print("  ! need root to install the hub service. Re-run with sudo:")
        print(f"      sudo {exe} service --daemon --host {host} --port {port}")
        print(f"    (unit file staged at {staged})")
        return 1
    _systemctl("daemon-reload")
    # enable + restart (not `enable --now`): --now is a no-op on an already-
    # running unit, which would leave a stale environment/ExecStart in place.
    ok = _systemctl("enable", unit) and _systemctl("restart", unit)
    print(f"  service: {unit}  {'started ✓' if ok else '(check status)'}")
    print(f"  Docket hub at http://{host}:{port}")
    print(f"  manage:  sudo systemctl {{status,restart,stop}} {unit}")
    return 0 if ok else 1


def cmd_projects(args) -> int:
    from docket_dev import service
    projects = service.load_projects()
    if not projects:
        print("No projects registered. Create one with `docket new <name>` or "
              "`docket init` in a repo.")
        return 0
    print(f"{'ID':<24} {'KIND':<10} {'PORT':<6} {'MODE':<12} STATUS")
    for p in projects:
        print(f"{p.get('id',''):<24} {p.get('kind',''):<10} "
              f"{str(p.get('port','')):<6} {p.get('dev_mode',''):<12} "
              f"{service.project_status(p)}")
    return 0


# ---------------------------------------------------------------------------
# systemd units (--daemon)
# ---------------------------------------------------------------------------

def _unit_names(cfg: Config) -> tuple[str, str]:
    slug = (cfg.repo_slug or cfg.project_root.name).replace("/", "-")
    return f"docket-{slug}-web.service", f"docket-{slug}-agent.service"


def _unit_bodies(cfg: Config) -> dict:
    import getpass
    web, agent = _unit_names(cfg)
    runas = getpass.getuser()
    exe = shutil.which("docket") or str(Path(sys.executable).parent / "docket")
    # The agent shells out to `claude`; give the unit a HOME (for ~/.claude creds)
    # and a PATH that includes both the docket entrypoint and the claude binary.
    home = str(Path.home())
    claude = shutil.which("claude")
    pathparts = [str(Path(exe).parent)]
    if claude:
        pathparts.append(str(Path(claude).parent))
    pathparts += ["/usr/local/bin", "/usr/bin", "/bin"]
    path = ":".join(dict.fromkeys(pathparts))

    def body(desc, cmd, restart_secs):
        return (f"[Unit]\nDescription={desc}\nAfter=network.target\n\n"
                f"[Service]\nType=simple\nUser={runas}\n"
                f'Environment="HOME={home}"\nEnvironment="PATH={path}"\n'
                f"WorkingDirectory={cfg.project_root}\nExecStart={exe} {cmd}\n"
                f"Restart=on-failure\nRestartSec={restart_secs}\n\n"
                f"[Install]\nWantedBy=multi-user.target\n")

    return {
        web: body(f"Docket web — {cfg.repo_slug or cfg.project_root.name}", "serve", 5),
        agent: body(f"Docket agent — {cfg.repo_slug or cfg.project_root.name}", "agent", 10),
    }


def _systemctl(*args) -> bool:
    try:
        r = subprocess.run(["systemctl", *args], capture_output=True, text=True)
        if r.returncode != 0 and r.stderr.strip():
            print(f"    systemctl {' '.join(args)}: {r.stderr.strip().splitlines()[-1]}")
        return r.returncode == 0
    except FileNotFoundError:
        return False


def _install_units(cfg: Config) -> int:
    units = _unit_bodies(cfg)
    target_dir = Path("/etc/systemd/system")
    wrote, no_perm = [], False
    for name, bod in units.items():
        try:
            (target_dir / name).write_text(bod)
            wrote.append(name)
        except PermissionError:
            no_perm = True
            (cfg.docket_dir / name).write_text(bod)

    if no_perm:
        print("  ! need root to install system services. Re-run with sudo:")
        print(f"      sudo {shutil.which('docket') or 'docket'} up --daemon")
        print(f"    (unit files staged under {cfg.docket_dir})")
        return 1

    _systemctl("daemon-reload")
    ok = all(_systemctl("enable", "--now", n) for n in units)
    web, agent = _unit_names(cfg)
    print(f"  services: {web}, {agent}")
    print(f"  {'started ✓' if ok else 'installed (check status below)'}")
    _systemctl("--no-pager", "--lines=0", "status", web, agent)
    print(f"\n  Docket is running at {cfg.base_url}/docket")
    if cfg.base_url.startswith("http://localhost") or cfg.base_url.startswith("http://127."):
        print(f"  ! base_url is local — for remote access set [server].base_url to your host's"
              f" external URL (e.g. http://<EXTERNAL_IP>:{cfg.port}) and open the firewall for port {cfg.port}.")
    print(f"  manage:  sudo systemctl {{status,restart,stop}} {web} {agent}")
    print(f"  logs:    journalctl -u {agent} -f")
    return 0 if ok else 1


def _stop_units(cfg: Config) -> int:
    web, agent = _unit_names(cfg)
    for n in (web, agent):
        _systemctl("disable", "--now", n)
    print(f"  stopped {web}, {agent}")
    return 0


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="docket", description="Portable ticket pipeline + autonomous dev agent.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("init", help="initialize Docket in the current repo")
    pi.add_argument("path", nargs="?", help="repo path (default: cwd)")
    pi.add_argument("--slug", help="GitHub owner/name (default: detect from remote)")
    pi.add_argument("--remote", default="origin")
    pi.add_argument("--base-branch", dest="base_branch", help="default: detect")
    pi.add_argument("--port", type=int, help="default: first free port from 8011")
    pi.add_argument("--base-url", dest="base_url", help="public URL for links (default http://localhost:PORT)")
    pi.add_argument("--user", help="login username (default: git user.name)")
    pi.add_argument("--email", help="login email (default: git user.email)")
    pi.add_argument("--password", help="login password (default: testing)")
    pi.add_argument("--model", default="opus", help="agent model (default: opus)")
    pi.add_argument("--dev-mode", choices=("pr", "auto_merge", "direct_main"), default="pr",
                    help="how finished work ships: pr (branch+PR), auto_merge, or "
                         "direct_main (commit straight to base branch, no PR)")
    pi.add_argument("--no-writes", action="store_true", help="grooming only (no code-gen)")
    pi.add_argument("--no-push", action="store_true", help="implement + commit locally but hold the push for inspection")
    pi.add_argument("--no-recognize", action="store_true", help="skip codebase recognition")
    pi.add_argument("--seed-limit", type=int, default=8)
    pi.add_argument("--force", action="store_true", help="overwrite existing config")
    pi.set_defaults(func=cmd_init)

    pn = sub.add_parser("new", help="create a new (greenfield) project from scratch")
    pn.add_argument("name", help="project name")
    pn.add_argument("--path", help="target folder (default: ./<slug>)")
    pn.add_argument("--port", type=int, help="web port (default: auto-allocate)")
    pn.add_argument("--dev-mode", choices=("pr", "auto_merge", "direct_main"),
                    default="direct_main",
                    help="default direct_main — greenfield has no remote, so no PR")
    pn.add_argument("--model", default="opus", help="agent model (default: opus)")
    pn.add_argument("--user", help="login username (default: git user.name)")
    pn.add_argument("--email", help="login email")
    pn.add_argument("--password", help="login password (default: testing)")
    pn.set_defaults(func=cmd_new)

    pg = sub.add_parser("groom", help="groom PROJECT_BRIEF.md into an ordered backlog")
    pg.add_argument("--cap", type=int, default=40, help="max tickets to draft")
    pg.set_defaults(func=cmd_groom)

    psvc = sub.add_parser("service", help="run the multi-project control-plane dashboard (the hub)")
    psvc.add_argument("--host", help="bind host (default: 127.0.0.1)")
    psvc.add_argument("--port", type=int, help="dashboard port (default: 8010)")
    psvc.add_argument("--daemon", action="store_true",
                      help="install + start a docket-service systemd unit instead of foreground")
    psvc.set_defaults(func=cmd_service)

    sub.add_parser("projects", help="list registered projects + status").set_defaults(func=cmd_projects)

    pu = sub.add_parser("up", help="run web UI + agent")
    pu.add_argument("--daemon", action="store_true",
                    help="install + start background systemd services instead of foreground")
    pu.set_defaults(func=cmd_up)

    sub.add_parser("down", help="stop the background services").set_defaults(func=cmd_down)

    sub.add_parser("serve", help="run just the web UI").set_defaults(func=cmd_serve)
    pa = sub.add_parser("agent", help="run just the agent loop")
    pa.add_argument("--once", action="store_true", help="work one ticket then exit")
    pa.set_defaults(func=cmd_agent)

    pr = sub.add_parser("recognize", help="regenerate profile + CLAUDE.md")
    pr.add_argument("--seed-limit", type=int, default=8)
    pr.set_defaults(func=cmd_recognize)

    ps = sub.add_parser("seed", help="draft starter tickets from the repo")
    ps.add_argument("--seed-limit", type=int, default=8)
    ps.set_defaults(func=cmd_seed)

    sub.add_parser("status", help="show config + ticket status").set_defaults(func=cmd_status)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
