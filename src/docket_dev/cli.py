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
    pi.add_argument("--model", default="sonnet", help="agent model (default: sonnet)")
    pi.add_argument("--no-writes", action="store_true", help="grooming only (no code-gen)")
    pi.add_argument("--no-push", action="store_true", help="implement + commit locally but hold the push for inspection")
    pi.add_argument("--no-recognize", action="store_true", help="skip codebase recognition")
    pi.add_argument("--seed-limit", type=int, default=8)
    pi.add_argument("--force", action="store_true", help="overwrite existing config")
    pi.set_defaults(func=cmd_init)

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
