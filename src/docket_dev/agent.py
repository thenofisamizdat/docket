"""
Docket autonomous agent — the orchestrator that works tickets off the queue.

Design (see DOCKET.md): a thin orchestrator OWNS the lifecycle transitions and
invokes a headless Claude Code agent ONE PHASE AT A TIME. The agent supplies the
*content* (assessment, plan, code, review); the orchestrator drives state and
records everything on the ticket timeline so the board always shows what's
happening — including a live "currently working on" ticker fed by the agent's
tool activity.

Phases: Assessment → Planning → In Development → Self-Review → PR.
  - Assessment + Planning are READ-ONLY (Edit/Write disallowed) — always safe.
  - In Development / Self-Review / PR WRITE code + push a branch, so they are
    gated behind DOCKET_AGENT_WRITES (default off). With writes off, the agent
    grooms a ticket (assess + plan) and parks it at Planning with a note.

Guardrails: per-phase --max-turns + --max-budget-usd, a subprocess timeout, the
hybrid grooming gate (bounce vague P0/P1 asks to Needs Info; best-effort the
rest), and any failure → Stalled (never silently stuck). NEVER auto-merges.

Runs as root (has claude creds + the GitHub push key + the Neil B
<thenofisamizdat@gmail.com> commit identity). Lightweight: imports only
docket_storage (no Neo4j / embeddings).

Run a single pass (pick the top queued ticket, work it, exit):
    python -m services.docket_agent --once
Run the continuous loop:
    python -m services.docket_agent
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from docket_dev import storage as dk
from docket_dev.auth import tester_email
from docket_dev.config import CONFIG

# --- config (env-overridable; populated from .docket/config.toml via
# config.apply_env() before this module is imported by the CLI) ---
WRITES_ENABLED = os.environ.get("DOCKET_AGENT_WRITES", "0") == "1"
# Push gate: by default push when writes are on, but DOCKET_AGENT_PUSH=0 lets us
# run the full implement+review locally and HOLD the push for manual inspection.
PUSH_ENABLED = os.environ.get("DOCKET_AGENT_PUSH", "1" if WRITES_ENABLED else "0") == "1"
# Auto-merge: squash-merge the PR via the GitHub API once self-review passes, then
# mark the ticket done. Requires a real PR object (i.e. a GitHub token); without a
# token it safely no-ops and the ticket waits at PR for a manual merge.
AUTO_MERGE = os.environ.get("DOCKET_AGENT_AUTO_MERGE", "0") == "1"
# Delivery mode for finished work (see Config.agent_dev_mode):
#   "pr"          — branch + PR (the default; PUSH/AUTO_MERGE gates apply)
#   "auto_merge"  — branch + PR, squash-merged automatically
#   "direct_main" — no branch/PR: commit straight onto BASE_BRANCH, push to REMOTE
#                   only if a remote exists, then advance the ticket itself.
DEV_MODE = os.environ.get("DOCKET_AGENT_DEV_MODE", "pr")
# Fold the new mode into the legacy flag so the existing AUTO_MERGE code path is reused.
AUTO_MERGE = AUTO_MERGE or DEV_MODE == "auto_merge"
DIRECT_MAIN = DEV_MODE == "direct_main"
# Default model for EVERY phase. Docket defaults to the strongest model (Opus)
# because pipeline quality matters more than per-ticket cost/speed; override per
# install via [agent].model in .docket/config.toml or DOCKET_AGENT_MODEL.
MODEL = os.environ.get("DOCKET_AGENT_MODEL", "opus")
# A stronger model the recovery process escalates to when the default one ran
# out of room (SCOPE) or couldn't fix its own work (DEFECT after a corrective
# pass). Escalation is targeted, not default — applied only by the recovery router.
STRONG_MODEL = os.environ.get("DOCKET_AGENT_STRONG_MODEL", "opus")
MAIN_CHECKOUT = Path(os.environ.get("DOCKET_MAIN_CHECKOUT", str(Path.cwd())))
WORKTREE_DIR = Path(os.environ.get("DOCKET_WORKTREE_DIR", str(Path.cwd() / ".docket" / "worktrees")))
REPO_SLUG = os.environ.get("DOCKET_REPO_SLUG", "")
# Branch worktrees fork from / PRs target, and the remote to push to.
BASE_BRANCH = os.environ.get("DOCKET_BASE_BRANCH", "main")
REMOTE = os.environ.get("DOCKET_REMOTE", "origin")
POLL_SECS = int(os.environ.get("DOCKET_AGENT_POLL", "20"))
# GitHub token for real PR-object creation; without one we fall back to pushing
# the branch and recording a compare URL (Neil opens the PR by hand).
GITHUB_TOKEN = (os.environ.get("DOCKET_GITHUB_TOKEN")
                or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "")

# --- Second engine: OpenAI Codex CLI (optional). Config values are mirrored via
# DOCKET_CODEX_*; empty values auto-discover a codex binary + authenticated
# ~/.codex home (possibly under another OS user's home — run via runuser). ---
import glob as _glob
import pwd as _pwd
import shutil as _shutil


def _discover_codex() -> tuple[str, str, str]:
    """Find (bin, home, user) for codex. Explicit env/config wins; otherwise look
    on PATH and in /home/*/.local/bin, and pair the binary with the .codex home
    (containing auth.json) of the same user."""
    cbin = os.environ.get("DOCKET_CODEX_BIN", "")
    home = os.environ.get("DOCKET_CODEX_HOME", "")
    user = os.environ.get("DOCKET_CODEX_USER", "")
    if not cbin:
        cbin = _shutil.which("codex") or ""
    if not cbin:
        hits = sorted(_glob.glob("/home/*/.local/bin/codex") +
                      _glob.glob("/root/.local/bin/codex"))
        cbin = hits[0] if hits else ""
    if cbin and not home:
        # Prefer the .codex home beside the binary's owner, then the caller's
        # own, then ANY authenticated home on the box (the binary may live in
        # /usr/local/bin while the login lives under a user's home).
        cands = [Path(cbin).parent.parent.parent / ".codex", Path.home() / ".codex"]
        cands += [Path(p).parent for p in
                  sorted(_glob.glob("/home/*/.codex/auth.json") +
                         _glob.glob("/root/.codex/auth.json"))]
        for cand in cands:
            if (cand / "auth.json").is_file():
                home = str(cand)
                break
    if home and not user:
        try:
            owner = _pwd.getpwuid(os.stat(home).st_uid).pw_name
            me = _pwd.getpwuid(os.geteuid()).pw_name
            user = owner if owner != me else ""
        except Exception:
            user = ""
    return cbin, home, user


CODEX_BIN, CODEX_HOME, CODEX_USER = _discover_codex()
CODEX_MODEL = os.environ.get("DOCKET_CODEX_MODEL", "gpt-5.5")
CODEX_ENABLED = bool(CODEX_BIN and CODEX_HOME)
ENGINES = ("claude", "codex") if CODEX_ENABLED else ("claude",)
# Email sender identity for msmtp-delivered notifications.
MAIL_FROM = os.environ.get("DOCKET_MAIL_FROM", "Docket <docket@localhost>")


def _record_roadmap_done(tid: int) -> None:
    """Write pipeline results back onto the ticket's roadmap card (auto hours + a
    'Done by Docket pipeline' note). Lazy-imported + best-effort so it can never
    break the pipeline's terminal path."""
    try:
        from docket_dev import roadmap as rm
        rm.record_pipeline_done(tid)
    except Exception as e:
        log(f"  (roadmap write-back skipped: {e})")


def _notify_default() -> str:
    """Fallback notification recipient (stalled/pr_ready/needs_info) when a
    ticket has no creator — the configured default, else the first tester."""
    rec = (CONFIG.default_recipient or "").strip().lower()
    if rec:
        return rec
    return CONFIG.testers[0]["username"].lower() if CONFIG.testers else ""


READONLY_TOOLS = ["Read", "Grep", "Glob", "Bash(git log:*)", "Bash(git diff:*)",
                  "Bash(ls:*)", "Bash(cat:*)", "Bash(find:*)", "Bash(grep:*)"]
WRITE_TOOLS = ["Read", "Grep", "Glob", "Edit", "Write", "Bash"]

# --- Resilience knobs (the "dependable & failsafe" core) ---
# Transient infra failures (overloaded / rate-limit / timeout / network / CLI
# crash) are RETRIED with exponential backoff inside run_claude rather than
# stalling a ticket on the first hiccup. Only a genuine error or exhausted
# retries stalls — and that stall is tagged transient so a human knows a plain
# resubmit is all it needs. Docket must never park a ticket because the API blipped.
AGENT_RETRIES = int(os.environ.get("DOCKET_AGENT_RETRIES", "3"))
RETRY_BACKOFF_SECS = float(os.environ.get("DOCKET_AGENT_BACKOFF", "10"))
# Self-review corrective passes: how many implement->review cycles before giving
# up. The loop is REAL and bounded (the old code promised "one corrective loop"
# but never re-ran development — it just stalled on the first FAIL).
MAX_DEV_PASSES = int(os.environ.get("DOCKET_MAX_DEV_PASSES", "3"))
# Self-healing: a transient failure that survives the in-phase retries doesn't
# stall — it auto-requeues (the agent works other tickets meanwhile, giving the
# API time to recover) up to this many times before finally parking in Stalled.
MAX_AUTO_RECOVERIES = int(os.environ.get("DOCKET_AUTO_RECOVERIES", "5"))
AUTO_RETRY_MARK = "[auto-retry]"  # stable token used to count prior auto-requeues

# Substrings that mark a failure as transient/infra (case-insensitive). These are
# retryable; anything else is a genuine result and goes to reason-driven recovery.
_TRANSIENT_MARKERS = (
    "529", "overloaded", "rate limit", "rate_limit", "ratelimit",
    "timed out", "timeout", "phase timed out", "connection", "econnreset",
    "network", "temporarily", "try again", "500 internal", "502", "503", "504",
    "internal server error", "bad gateway", "service unavailable",
    "gateway timeout", "claude failed", "codex failed", "stream disconnected",
)


def _is_transient(text: str) -> bool:
    """Does this error look like a retryable infra blip rather than a real
    problem with the ticket?"""
    low = (text or "").lower()
    return any(m in low for m in _TRANSIENT_MARKERS)


# A SCOPE failure = the phase ran out of room (max turns / budget). The reason is
# "too big / too hard for the resources given", so the right correction is more
# capability — a stronger model + bigger limits — not a blind re-run.
_SCOPE_SUBTYPES = ("error_max_turns", "error_max_budget", "error_budget_exceeded")
_SCOPE_MARKERS = ("max turns", "maximum number of turns", "max-turns",
                  "max budget", "max-budget", "budget exceeded", "budget limit")


def _is_scope(out: dict) -> bool:
    """Did this failure come from exhausting the turn/budget ceiling?"""
    if (out.get("subtype") or "").lower() in _SCOPE_SUBTYPES:
        return True
    low = (out.get("text") or "").lower()
    return any(m in low for m in _SCOPE_MARKERS)


def log(msg: str) -> None:
    print(f"[docket-agent] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Headless Claude runner
# ---------------------------------------------------------------------------

def _short(p: str) -> str:
    return p.split("/")[-1] if p else ""


def _summarize_tool(block: dict) -> str:
    name = block.get("name", "")
    inp = block.get("input", {}) or {}
    if name == "Read":
        return f"Reading {_short(inp.get('file_path', ''))}"
    if name in ("Edit", "Write", "NotebookEdit"):
        return f"Editing {_short(inp.get('file_path', ''))}"
    if name == "Bash":
        return "Running: " + str(inp.get("command", ""))[:60]
    if name in ("Grep", "Glob"):
        return f"Searching {str(inp.get('pattern', ''))[:40]}"
    if name == "Task":
        return "Delegating to a sub-agent"
    if name == "TodoWrite":
        return "Updating its plan"
    return f"Using {name}"


def run_claude(prompt: str, cwd: Path, *, on_activity=None, **kw) -> dict:
    """Resilient wrapper around _run_claude_once: retry transient infra failures
    (overloaded / rate-limit / timeout / network / CLI crash) with exponential
    backoff. Returns is_error=True only on a non-transient error or once retries
    are exhausted (then the result carries 'retried': True so the caller can tag
    the stall as transient)."""
    out = None
    for attempt in range(1, AGENT_RETRIES + 1):
        out = _run_claude_once(prompt, cwd, on_activity=on_activity, **kw)
        if not out["is_error"] or not _is_transient(out["text"]):
            return out
        if attempt >= AGENT_RETRIES:
            out["retried"] = True
            return out
        wait = RETRY_BACKOFF_SECS * (2 ** (attempt - 1))
        msg = (f"transient error ({(out['text'] or '').strip()[:60]}…) — retrying "
               f"in {int(wait)}s (attempt {attempt + 1}/{AGENT_RETRIES})")
        log("  " + msg)
        if on_activity:
            try:
                on_activity(msg)
            except Exception:
                pass
        time.sleep(wait)
    return out


def run_phase(prompt: str, cwd: Path, *, max_turns: int, max_budget_usd: float,
              model=None, label: str = "", on_activity=None, engine: str = "claude",
              **kw) -> dict:
    """Run a pipeline phase resiliently, with SCOPE escalation. If the phase fails
    because it ran out of room (max turns / budget) — not an infra blip — retry it
    ONCE with a stronger model and doubled limits. This is the 'correct based on
    the reason' principle: a scope problem gets more capability, not a blind re-run.

    `engine="codex"` routes the phase to the Codex runner instead (no turn/budget
    flags there — the timeout guards it; SCOPE escalation is a Claude concept)."""
    if engine == "codex" and CODEX_ENABLED:
        out = run_codex(prompt, cwd, on_activity=on_activity,
                        timeout=kw.get("timeout", 900), model=None)
        out.setdefault("engine", "codex")
        return out
    out = run_claude(prompt, cwd, max_turns=max_turns, max_budget_usd=max_budget_usd,
                     model=model, on_activity=on_activity, **kw)
    out.setdefault("engine", "claude")
    if out["is_error"] and _is_scope(out):
        msg = (f"{label or 'phase'} ran out of room — escalating to {STRONG_MODEL} "
               f"with 2× turns/budget and retrying once")
        log("  " + msg)
        if on_activity:
            try:
                on_activity(msg)
            except Exception:
                pass
        out = run_claude(prompt, cwd, max_turns=max_turns * 2,
                         max_budget_usd=max_budget_usd * 2, model=STRONG_MODEL,
                         on_activity=on_activity, **kw)
        out["escalated"] = True
        out.setdefault("engine", "claude")
    return out


def _run_claude_once(prompt: str, cwd: Path, *, allowed_tools=None, disallowed_tools=None,
               permission_mode="default", max_turns=20, max_budget_usd=2.0,
               timeout=900, on_activity=None, model=None) -> dict:
    """Invoke Claude Code headless in `cwd`, streaming progress. Returns
    {text, is_error, cost, turns, session_id, subtype}."""
    cmd = ["claude", "-p", prompt,
           "--output-format", "stream-json", "--verbose",
           "--max-turns", str(max_turns),
           "--permission-mode", permission_mode,
           "--model", model or MODEL]
    if max_budget_usd:
        cmd += ["--max-budget-usd", str(max_budget_usd)]
    if allowed_tools:
        cmd += ["--allowedTools", *allowed_tools]
    if disallowed_tools:
        cmd += ["--disallowedTools", *disallowed_tools]

    out = {"text": "", "is_error": False, "cost": 0.0, "turns": 0, "session_id": "",
           "subtype": "", "engine": "claude", "model": model or MODEL}
    try:
        proc = subprocess.Popen(cmd, cwd=str(cwd), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, bufsize=1,
                                env=os.environ.copy())
    except FileNotFoundError:
        out["is_error"] = True
        out["text"] = "claude CLI not found on PATH"
        return out

    start = time.monotonic()
    try:
        for line in proc.stdout:
            if time.monotonic() - start > timeout:
                proc.kill()
                out["is_error"] = True
                out["text"] = out["text"] or "(phase timed out)"
                break
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            t = ev.get("type")
            if t == "assistant":
                for block in ev.get("message", {}).get("content", []):
                    if block.get("type") == "tool_use" and on_activity:
                        desc = _summarize_tool(block)
                        if desc:
                            on_activity(desc)
            elif t == "result":
                out["text"] = ev.get("result", "") or ""
                out["is_error"] = bool(ev.get("is_error"))
                out["cost"] = ev.get("total_cost_usd", 0.0) or 0.0
                out["turns"] = ev.get("num_turns", 0) or 0
                out["session_id"] = ev.get("session_id", "") or ""
                out["subtype"] = ev.get("subtype", "") or ""
                # A turn/budget-capped run reports success-ish but is really a
                # SCOPE failure; surface it as an error so recovery can escalate.
                if out["subtype"] in _SCOPE_SUBTYPES:
                    out["is_error"] = True
                    out["text"] = out["text"] or f"ran out of room ({out['subtype']})"
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        out["is_error"] = True
    if proc.returncode not in (0, None) and not out["text"]:
        out["is_error"] = True
        try:
            out["text"] = (proc.stderr.read() or "claude failed")[:2000]
        except Exception:
            out["text"] = "claude failed"
    out["duration"] = round(time.monotonic() - start, 1)
    return out


# ---------------------------------------------------------------------------
# Headless Codex runner (second engine)
# ---------------------------------------------------------------------------

def _codex_gitdir(cwd: Path) -> Path | None:
    """A worktree's real git dir (…/.git/worktrees/<name>) — codex needs to write
    the index there when it runs git itself."""
    gitfile = cwd / ".git"
    try:
        if gitfile.is_file():
            head = gitfile.read_text().strip()
            if head.startswith("gitdir:"):
                return Path(head.split(":", 1)[1].strip())
    except Exception:
        pass
    return None


def _own_tree_for_codex(cwd: Path) -> None:
    """Codex runs as CODEX_USER (the auth owner) while the agent runs as root, so
    hand the working tree (and its worktree git dir) to that user. Root git keeps
    working regardless of file ownership, so there's no chown-back."""
    if not CODEX_USER:
        return
    targets = [cwd]
    gd = _codex_gitdir(cwd)
    if gd and gd.exists():
        targets.append(gd)
    for p in targets:
        subprocess.run(["chown", "-R", CODEX_USER, str(p)], capture_output=True)


def _summarize_codex_item(item: dict) -> str:
    t = item.get("type", "")
    if t == "command_execution":
        return "Running: " + str(item.get("command", ""))[:60]
    if t in ("file_change", "patch_apply", "file_update"):
        return "Editing files"
    if t == "web_search":
        return "Searching the web"
    if t == "mcp_tool_call":
        return f"Using {item.get('tool', 'a tool')}"
    return ""


def run_codex(prompt: str, cwd: Path, *, on_activity=None, **kw) -> dict:
    """Resilient wrapper around _run_codex_once — same transient-retry contract
    as run_claude."""
    out = None
    for attempt in range(1, AGENT_RETRIES + 1):
        out = _run_codex_once(prompt, cwd, on_activity=on_activity, **kw)
        if not out["is_error"] or not _is_transient(out["text"]):
            return out
        if attempt >= AGENT_RETRIES:
            out["retried"] = True
            return out
        wait = RETRY_BACKOFF_SECS * (2 ** (attempt - 1))
        msg = (f"transient codex error ({(out['text'] or '').strip()[:60]}…) — retrying "
               f"in {int(wait)}s (attempt {attempt + 1}/{AGENT_RETRIES})")
        log("  " + msg)
        if on_activity:
            try:
                on_activity(msg)
            except Exception:
                pass
        time.sleep(wait)
    return out


def _run_codex_once(prompt: str, cwd: Path, *, timeout=900, on_activity=None,
                    model=None, **_ignored) -> dict:
    """Invoke Codex CLI headless in `cwd`, streaming JSONL events. Mirrors the
    claude runner's return shape: {text, is_error, cost, turns, ...}. Codex has
    no per-phase turn/budget flags — the subprocess timeout is the guardrail —
    and ChatGPT-plan auth reports no dollar cost (tokens are recorded instead).
    Tool policy flags (allowed/disallowed) are Claude-specific and ignored here;
    the read-only phases never run on codex."""
    out = {"text": "", "is_error": False, "cost": 0.0, "turns": 0, "session_id": "",
           "subtype": "", "engine": "codex", "model": model or CODEX_MODEL,
           "tokens": {"input": 0, "output": 0}}
    if not CODEX_ENABLED:
        out["is_error"] = True
        out["text"] = "codex engine not configured"
        return out
    _own_tree_for_codex(cwd)
    inner = [CODEX_BIN, "exec", "--json", "--skip-git-repo-check",
             "--dangerously-bypass-approvals-and-sandbox",
             "-C", str(cwd), "-m", model or CODEX_MODEL, prompt]
    if CODEX_USER:
        path = f"{Path(CODEX_BIN).parent}:{os.environ.get('PATH', '')}"
        cmd = ["runuser", "-u", CODEX_USER, "--", "env",
               f"CODEX_HOME={CODEX_HOME}", f"PATH={path}", *inner]
    else:
        cmd = inner
    env = os.environ.copy()
    env.setdefault("CODEX_HOME", CODEX_HOME)
    try:
        proc = subprocess.Popen(cmd, cwd=str(cwd), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, bufsize=1, env=env)
    except FileNotFoundError:
        out["is_error"] = True
        out["text"] = "codex CLI not found"
        return out
    start = time.monotonic()
    try:
        for line in proc.stdout:
            if time.monotonic() - start > timeout:
                proc.kill()
                out["is_error"] = True
                out["text"] = out["text"] or "(phase timed out)"
                break
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            t = ev.get("type", "")
            if t == "item.completed":
                item = ev.get("item") or {}
                if item.get("type") == "agent_message":
                    out["text"] = item.get("text", "") or out["text"]
                elif item.get("type") == "error":
                    out["is_error"] = True
                    out["text"] = item.get("message", "") or out["text"] or "codex failed"
                elif on_activity:
                    desc = _summarize_codex_item(item)
                    if desc:
                        on_activity(desc)
            elif t == "turn.completed":
                out["turns"] += 1
                u = ev.get("usage") or {}
                out["tokens"]["input"] += int(u.get("input_tokens") or 0)
                out["tokens"]["output"] += int(u.get("output_tokens") or 0)
            elif t == "turn.failed" or t == "error":
                out["is_error"] = True
                out["text"] = (str(ev.get("error", {}).get("message", "") if isinstance(ev.get("error"), dict)
                               else ev.get("message", "")) or out["text"] or "codex failed")
            elif t == "thread.started":
                out["session_id"] = ev.get("thread_id", "") or ""
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        out["is_error"] = True
    if proc.returncode not in (0, None) and not out["text"]:
        out["is_error"] = True
        try:
            out["text"] = (proc.stderr.read() or "codex failed")[:2000]
        except Exception:
            out["text"] = "codex failed"
    if not out["is_error"] and not out["text"].strip():
        out["is_error"] = True
        out["text"] = "codex produced no final message"
    out["duration"] = round(time.monotonic() - start, 1)
    return out


# ---------------------------------------------------------------------------
# Worktrees
# ---------------------------------------------------------------------------

def _ensure_base_checkout() -> tuple[Path, str]:
    """direct_main mode: no worktree. Work happens in the main checkout itself, on
    BASE_BRANCH, so commits land straight on the base branch. Returns
    (MAIN_CHECKOUT, BASE_BRANCH)."""
    subprocess.run(["git", "-C", str(MAIN_CHECKOUT), "checkout", BASE_BRANCH],
                   check=True, capture_output=True, text=True)
    return MAIN_CHECKOUT, BASE_BRANCH


def ensure_worktree(ticket: dict) -> tuple[Path, str]:
    """Create (or reuse) a per-ticket git worktree + branch off the base branch.
    In direct_main mode there is no worktree/branch — we operate on BASE_BRANCH in
    the main checkout so work commits directly to it."""
    if DIRECT_MAIN:
        return _ensure_base_checkout()
    tid = ticket["id"]
    branch = f"docket/DKT-{tid}"
    path = WORKTREE_DIR / f"DKT-{tid}"
    WORKTREE_DIR.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path, branch
    subprocess.run(
        ["git", "-C", str(MAIN_CHECKOUT), "worktree", "add", "-B", branch,
         str(path), BASE_BRANCH],
        check=True, capture_output=True, text=True,
    )
    return path, branch


def workdir_for(ticket: dict) -> tuple[Path, str | None]:
    """Where the agent runs. With writes on, a per-ticket worktree; otherwise the
    read-only main checkout (Edit/Write are disallowed in read-only phases anyway)."""
    if WRITES_ENABLED:
        return ensure_worktree(ticket)
    return MAIN_CHECKOUT, None


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

def _ctx(t: dict) -> str:
    base = (f"Ticket {t['ref']} ({t['type']}, priority {t['priority']}):\n"
            f"TITLE: {t['title']}\n"
            f"DESCRIPTION: {t.get('description') or '(none)'}\n"
            f"ACCEPTANCE CRITERIA: {t.get('acceptance_criteria') or '(none)'}\n")
    return base + _hierarchy_ctx(t) + _prior_rejections_ctx(t)


def _hierarchy_ctx(t: dict) -> str:
    """The ticket's place in the plan: its epic and (for nested tickets) the
    parent story + siblings. Scope decisions usually live in the story text —
    without this the assessor bounces questions the plan already answers
    ('which option did the parent story choose?')."""
    out = ""
    try:
        if t.get("epic_id"):
            ep = dk.epics_map().get(t["epic_id"])
            if ep:
                out += f"EPIC: {ep['name']}"
                if (ep.get("description") or "").strip():
                    out += f" — {ep['description'][:1200]}"
                out += "\n"
        if t.get("parent_id"):
            p = dk.get_ticket(t["parent_id"])
            if p:
                out += (f"PARENT STORY {p['ref']} [{p['status']}]: {p['title']}\n"
                        f"STORY DESCRIPTION: {(p.get('description') or '(none)')[:3000]}\n")
                if (p.get("acceptance_criteria") or "").strip():
                    out += f"STORY ACCEPTANCE CRITERIA: {p['acceptance_criteria'][:1500]}\n"
                sibs = [s for s in dk.children_of(p["id"]) if s["id"] != t["id"]]
                if sibs:
                    out += ("SIBLING TICKETS IN THIS STORY: " + "; ".join(
                        f"{s['ref']} [{s['status']}] {s['title']}" for s in sibs[:10]) + "\n")
    except Exception:
        return ""
    if out:
        out = ("\nPLAN HIERARCHY CONTEXT — scope and design decisions often live here; "
               "consult it (and the codebase) BEFORE bouncing for clarification:\n" + out)
    return out


def _prior_rejections_ctx(t: dict) -> str:
    """When a ticket has bounced (iteration > 0), the requester already tested a
    shipped fix and REJECTED it. Surface their rejection feedback so the agent
    stops re-shipping the same broken approach — the single biggest cause of the
    repeat-resubmit loop. We pull the requester's own words from the timeline
    (human comments + resubmit reasons), most recent last."""
    it = int(t.get("iteration") or 0)
    if it <= 0:
        return ""
    try:
        events = dk.get_events(t["id"])
    except Exception:
        events = []
    human = [e for e in events
             if e.get("kind") in ("comment", "resubmit")
             and (e.get("actor") or "").lower() not in ("agent", "system", "")]
    notes = "\n".join(f"  - {(e.get('summary') or '').strip()[:300]}" for e in human[-4:])
    return (
        f"\n⚠ THIS IS ATTEMPT #{it + 1}. Earlier agent attempts shipped a fix that the "
        f"requester TESTED and REJECTED. Repeating the same approach will fail again.\n"
        + (f"What the requester says is STILL wrong (their words):\n{notes}\n" if notes else "")
        + "Before coding: re-derive — from the requester's description — the EXACT UI "
          "surface/route/component they are using. A prior attempt may have changed the "
          "WRONG place. If your plan looks like the last one, change the approach.\n"
    )


def _repo_profile() -> str:
    """The stored codebase profile (written by `docket recognize`), injected as
    grounding so assessment/planning are repo-aware from the first ticket. Empty
    if no profile has been generated yet."""
    try:
        text = CONFIG.profile_path.read_text()
    except (OSError, FileNotFoundError):
        return ""
    text = text.strip()
    if not text:
        return ""
    return ("\nCODEBASE PROFILE (generated overview — verify against the live repo "
            "as needed):\n" + text[:4000] + "\n")


def _shipped_ctx() -> str:
    """Recently shipped tickets, so assessment can spot follow-ups: a new ask
    that's really 'the old fix didn't stick' should be linked, not treated as
    fresh work — it counts against the shipped ticket's post-ship health."""
    try:
        shipped = dk.shipped_tickets()
    except Exception:
        return ""
    if not shipped:
        return ""
    lines = "\n".join(f"  {s['ref']}: {s['title']}" for s in shipped[-20:])
    return (
        "\nPREVIOUSLY SHIPPED TICKETS:\n" + lines + "\n"
        "If this request is really a follow-up to one of those (the shipped "
        "solution didn't fully solve it, regressed it, or missed the point), add "
        "this line directly above the VERDICT line:\n"
        "  RELATED: DKT-<n> || <one sentence: why it's a follow-up>\n"
        "If none apply, omit the RELATED line entirely.\n"
    )


def assess_prompt(t: dict) -> str:
    return (
        "You are the assessment phase of an autonomous dev pipeline working on "
        "this codebase. Explore the repo (READ ONLY — do not edit anything) and "
        "assess the following request.\n\n" + _ctx(t) + _repo_profile() +
        "\nProduce a concise assessment (≈150-250 words) covering: what the change "
        "involves, the key files/areas it would touch, risks or unknowns, and "
        "whether the ask is clear enough to implement.\n"
        + _shipped_ctx() +
        "End your message with EXACTLY ONE final line, either:\n"
        "  VERDICT: PROCEED\n"
        "or, if the request is too vague/ambiguous to implement well:\n"
        "  VERDICT: NEEDS_INFO || <one sentence: the specific question(s) for the requester>"
    )


def plan_prompt(t: dict, assessment: str) -> str:
    return (
        "You are the planning phase of an autonomous dev pipeline. Based on the "
        "codebase (READ ONLY) and the assessment below, write a concrete, "
        "step-by-step implementation plan: the files to change, the approach for "
        "each, and how it will be tested/verified. Be specific and ordered.\n\n"
        + _ctx(t) + _repo_profile() + "\nASSESSMENT:\n" + assessment[:2000]
    )


def implement_prompt(t: dict, plan: str) -> str:
    return (
        "You are the implementation phase of an autonomous dev pipeline. Implement "
        "the plan below in this worktree. Make focused, correct changes; follow the "
        "surrounding code's style. Do not commit or push — just edit files. When "
        "done, briefly summarise what you changed.\n\n"
        + _ctx(t) + "\nPLAN:\n" + plan[:4000]
    )


def reimplement_prompt(t: dict, plan: str, fix: str) -> str:
    return (
        "You are the implementation phase of an autonomous dev pipeline, on a "
        "CORRECTIVE pass. Your previous changes are already in this worktree but "
        "self-review found problems. FIX exactly those problems — keep what works, "
        "change what's broken. Do not commit or push; just edit files. When done, "
        "briefly summarise what you changed and why it resolves the feedback.\n\n"
        + _ctx(t) +
        "\nSELF-REVIEW FEEDBACK TO ADDRESS:\n" + (fix or "(see prior review)")[:1500] +
        "\n\nORIGINAL PLAN (for reference):\n" + plan[:2500]
    )


def test_instructions_prompt(t: dict) -> str:
    return (
        "You are writing test instructions for a NON-TECHNICAL tester to verify the change "
        "just implemented in this worktree. Look at the diff (`git diff main`) and the "
        "acceptance criteria below, then write SHORT, numbered, plain-language steps: what to "
        "do and exactly what they should see if it works. No code, no jargon, no file paths — "
        "write it for someone who only uses the app's UI.\n\n" + _ctx(t)
    )


def review_prompt(t: dict) -> str:
    return (
        "You are an INDEPENDENT reviewer — NOT the engineer who wrote this code, "
        "and you do not trust their summary. Your DEFAULT verdict is FAIL. You may "
        "only PASS work you have PROVEN meets the acceptance criteria by EXECUTING "
        "a check that reproduces the ticket's ACTUAL scenario. Reading the diff and "
        "reasoning that it 'should' work, or 'verifying the logic by hand', is NOT "
        "acceptable and must be UNVERIFIED, never PASS.\n\n"
        "Do this, in order:\n"
        "1. Restate the acceptance criteria as concrete, observable outcomes.\n"
        "2. If this is a BUG: first REPRODUCE the reported broken behaviour — write "
        "and RUN a small script/test that exercises the exact inputs in the "
        "description (e.g. the specific dates/values the requester gave). Show it "
        "now produces the correct result, and that a neighbouring correct case "
        "still works. Quote the command(s) you ran and their real output.\n"
        "3. Also run whatever compile/lint/build/tests exist — but treat those as "
        "NECESSARY, NOT SUFFICIENT: they prove the code runs, not that the bug is "
        "fixed.\n"
        "4. Sanity-check you changed the RIGHT surface — does the file you edited "
        "actually back the UI/flow the requester described? If you can't tell, that "
        "is a reason to NOT pass.\n"
        "5. If the fix genuinely cannot be exercised in this worktree (needs a live "
        "service/data you don't have), say so plainly — that is UNVERIFIED.\n\n"
        + _ctx(t) +
        "\nEnd with EXACTLY ONE final line:\n"
        "  REVIEW: PASS  — ONLY if you executed a check reproducing the ticket "
        "scenario and observed the acceptance criteria met (command + output shown above).\n"
        "  REVIEW: UNVERIFIED || <why it couldn't be executed here, AND the exact "
        "step-by-step a human must run to test it> — change looks plausible but is NOT proven.\n"
        "  REVIEW: FAIL || <the specific defect to fix> — you found a concrete problem."
    )


def triage_prompt(t: dict, phase: str, failure: str, diff_summary: str = "") -> str:
    return (
        "You are the RECOVERY-TRIAGE step of an autonomous dev pipeline. A ticket "
        "could not be completed automatically. Your job is to diagnose WHY and "
        "choose the smartest next action — not to fix it now, and never to just "
        "blindly retry. Explore the repo READ-ONLY as needed to judge.\n\n"
        + _ctx(t) +
        f"\nFAILED PHASE: {phase}\nWHAT WENT WRONG:\n{(failure or '(no detail)')[:1800]}\n"
        + (f"\nCHANGES MADE SO FAR:\n{diff_summary[:1000]}\n" if diff_summary else "")
        + "\nDecide the single best recovery and end with EXACTLY ONE final line:\n"
        "  TRIAGE: RETRY  — a transient/infra blip (overload, timeout, flaky); just run it again later\n"
        "  TRIAGE: NEEDS_INFO || <the specific question(s) the requester must answer> "
        "— the ask is ambiguous, contradictory, or missing a detail/repro needed to implement it correctly\n"
        "  TRIAGE: BLOCKED || <why no automated fix can proceed and what a human must do> "
        "— a design decision, external access, or data the agent cannot obtain is required\n\n"
        "Prefer NEEDS_INFO whenever a human *answer* would unblock automated work. "
        "Use BLOCKED only when human *action* (not just an answer) is required. "
        "Make the question/reason concrete and specific to THIS ticket."
    )


import re as _re


def _strip_control(text: str) -> str:
    """Remove the trailing machine-readable 'VERDICT:'/'REVIEW:' control line so
    the stored/displayed body is clean prose."""
    return _re.sub(r"\n*\b(VERDICT|REVIEW|RELATED)\s*:.*$", "", text or "",
                   flags=_re.IGNORECASE | _re.DOTALL).strip()


def _pr_summary(t: dict, impl_text: str = "", files_stat: str = "") -> str:
    """A detailed, human-readable description of the work, built from the ticket
    (the *why*) and what the agent actually implemented (the *what*). Used for
    BOTH the commit-message body and the create_pr() body so they tell the same
    story — and because, with no PAT, GitHub prefills the PR from the commit body
    when Neil opens the compare URL. Replaces the old 'Autonomous Docket
    implementation.' boilerplate."""
    ref = t.get("ref") or f"DKT-{t['id']}"
    parts: list[str] = []
    desc = (t.get("description") or "").strip()
    if desc:
        parts.append(f"## Why\n{desc}")
    impl = _strip_control(impl_text or "").strip()
    if impl:
        parts.append(f"## What changed\n{impl[:1800]}")
    if files_stat.strip():
        parts.append(f"## Files\n```\n{files_stat.strip()}\n```")
    ac = (t.get("acceptance_criteria") or "").strip()
    if ac:
        parts.append(f"## Acceptance criteria\n{ac}")
    parts.append(f"_Opened by the Docket agent for {ref}. Never auto-merged — "
                 f"this PR is the human review gate._")
    return "\n\n".join(parts)


def parse_verdict(text: str, key: str) -> tuple[str, str]:
    """Return (verdict, detail) from a trailing 'KEY: ...' line. verdict is the
    first token (e.g. PROCEED / NEEDS_INFO / PASS / FAIL)."""
    verdict, detail = "", ""
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.upper().startswith(key.upper() + ":"):
            rest = line.split(":", 1)[1].strip()
            if "||" in rest:
                v, d = rest.split("||", 1)
                verdict, detail = v.strip().upper(), d.strip()
            else:
                verdict = rest.strip().upper()
            break
    return verdict, detail


# ---------------------------------------------------------------------------
# Phase driver
# ---------------------------------------------------------------------------

def _stall(tid: int, why: str, *, transient: bool = False) -> None:
    log(f"DKT-{tid} STALLED{' [transient]' if transient else ''}: {why}")
    try:
        note = f"Stalled: {why}"
        if transient:
            note += ("\n\n_(Transient infrastructure error — the agent/API blipped "
                     "and automatic retries were exhausted. Resubmit the ticket as-is; "
                     "no change to the request is needed.)_")
        dk.add_event(tid, "note", summary=note, actor="agent")
        dk.transition(tid, "stalled", actor="agent", summary=why[:120])
        t = dk.get_ticket(tid)
        subj = f"Docket {t['ref']}: stalled" + (" (transient — just resubmit)" if transient else "")
        dk.enqueue_notification(tid, _notify_default(), "stalled", subject=subj, body=why[:500])
    except Exception as e:
        log(f"  (failed to record stall: {e})")


def _auto_retry_count(tid: int) -> int:
    """How many times this ticket has already been auto-requeued for a transient
    failure (counted from the timeline so it survives agent restarts)."""
    try:
        return sum(1 for e in dk.get_events(tid)
                   if e.get("actor") == "agent" and AUTO_RETRY_MARK in (e.get("summary") or ""))
    except Exception:
        return 0


def _recover_or_stall(tid: int, why: str, *, transient: bool) -> None:
    """Failsafe failure handler. A transient/infra failure self-heals: the ticket
    is auto-requeued (bounded by MAX_AUTO_RECOVERIES) so it retries once capacity
    frees up, instead of stranding in Stalled and needing a human to resubmit.
    Only a genuine problem — or a transient one that persists past the cap —
    becomes a human-gated Stall."""
    if transient and _auto_retry_count(tid) < MAX_AUTO_RECOVERIES:
        n = _auto_retry_count(tid) + 1
        log(f"DKT-{tid} transient failure — auto-requeue {n}/{MAX_AUTO_RECOVERIES}: {why[:80]}")
        try:
            dk.add_event(tid, "note", actor="agent",
                         summary=(f"{AUTO_RETRY_MARK} Transient infra failure "
                                  f"(attempt {n}/{MAX_AUTO_RECOVERIES}) — auto-requeued; "
                                  f"will retry when capacity frees up. No human action "
                                  f"needed.\n\n{why[:300]}"))
            dk.transition(tid, "queued", actor="agent",
                          summary=f"Auto-requeue after transient failure ({n}/{MAX_AUTO_RECOVERIES})")
            return
        except Exception as e:
            log(f"  (auto-requeue failed, stalling instead: {e})")
    _stall(tid, why, transient=transient)


def _to_needs_info(t: dict, question: str, *, phase: str = "") -> None:
    """Bounce a ticket to the REQUESTER for a specific answer — a productive
    correction (the ask gets clarified and re-enters the pipeline) rather than a
    dead-end Stall aimed at the maintainer."""
    tid = t["id"]
    q = (question or "").strip() or ("The agent needs clarification to proceed — "
                                     "please add detail or a concrete repro.")
    try:
        dk.add_event(tid, "comment", actor="agent", summary=f"Needs clarification: {q}")
        dk.transition(tid, "needs_info", actor="agent",
                      summary=f"Agent needs input to proceed{(' (' + phase + ')') if phase else ''}")
        dk.enqueue_notification(tid, t.get("created_by") or _notify_default(), "needs_info",
                                subject=f"Docket {t['ref']}: needs your input", body=q)
        log(f"  → Needs Info: {q[:100]}")
    except Exception as e:
        log(f"  (needs_info routing failed, stalling instead: {e})")
        _stall(tid, f"{phase}: needs clarification — {q[:200]}")


def recover(t: dict, phase: str, failure: str, *, wt=None, diff_summary: str = "") -> None:
    """Reason-driven recovery router. Instead of blindly retrying, classify the
    failure and take the matching corrective action:

      INFRA (deterministic)  → self-heal: auto-requeue (bounded), then transient stall
      RETRY (triage)         → treat as transient (auto-requeue)
      NEEDS_INFO (triage)    → bounce to the requester with a specific question
      BLOCKED (triage)       → human-gated stall, with the classified reason

    SCOPE failures are handled upstream by run_phase (escalate model+budget) and
    DEFECT failures by the in-dev corrective loop (re-implement, model-escalated);
    this router is the terminal decision once those avenues are spent."""
    tid = t["id"]
    # Fast deterministic path — an obvious infra blip needs no LLM to diagnose.
    if _is_transient(failure):
        return _recover_or_stall(tid, f"{phase} failed: {failure[:160]}", transient=True)
    act = lambda d: dk.set_activity(tid, d)
    act("Diagnosing the failure to choose the right recovery")
    tr = run_claude(triage_prompt(t, phase, failure, diff_summary), wt or MAIN_CHECKOUT,
                    allowed_tools=READONLY_TOOLS, disallowed_tools=["Edit", "Write"],
                    permission_mode="default", max_turns=10, max_budget_usd=1.0,
                    on_activity=act)
    if tr["is_error"]:
        # Couldn't even diagnose — give it another whole pass rather than stalling.
        return _recover_or_stall(tid, f"{phase} failed (triage unavailable): {failure[:160]}",
                                 transient=True)
    verdict, detail = parse_verdict(tr["text"], "TRIAGE")
    dk.add_event(tid, "note", actor="agent",
                 summary=(f"**Recovery triage** — failed at *{phase}*. "
                          f"Diagnosis: **{verdict or 'BLOCKED'}**"
                          + (f"\n\n{detail}" if detail else "")
                          + f"\n\n_Reasoning:_ {_strip_control(tr['text'])[:600]}"),
                 payload={"cost_usd": tr["cost"], "turns": tr["turns"]})
    if verdict == "RETRY":
        return _recover_or_stall(tid, f"{phase} failed: {failure[:160]}", transient=True)
    if verdict == "NEEDS_INFO":
        return _to_needs_info(t, detail, phase=phase)
    # BLOCKED, or a garbled/empty verdict → human-gated stall with the diagnosis.
    return _stall(tid, f"{phase} blocked: {(detail or failure)[:300]}")


def choose_engine(t: dict) -> tuple[str, str]:
    """Pick the build engine for a ticket: (engine, rationale).

    A hand-pinned engine always wins. Otherwise route by cheap, explainable
    heuristics: Codex takes the volume lane (small, well-specified tasks/bugs);
    Claude keeps the judgment lane (P0s, stories, low-clarity asks, big
    estimates, resubmits, and anything Codex already struggled on)."""
    # Only a HUMAN-pinned engine is binding — the router's own earlier stamp
    # (engine set, pinned=0) is re-evaluated every pickup, so resubmits and
    # retries can still escalate across engines.
    pinned = (t.get("engine") or "").strip().lower() if t.get("engine_pinned") else ""
    if pinned == "claude":
        return "claude", "pinned"
    if pinned == "codex":
        if CODEX_ENABLED:
            return "codex", "pinned"
        return "claude", "pinned to codex, but codex is not available here"
    if not CODEX_ENABLED:
        return "claude", ""
    if int(t.get("iteration") or 0) >= 1:
        return "claude", "resubmitted work goes to the judgment lane"
    if _auto_retry_count(t["id"]) >= 2:
        return "claude", "repeated auto-retries — escalating engine"
    if t["priority"] == "P0":
        return "claude", "P0 — critical path stays on claude"
    if t["type"] == "story":
        return "claude", "story-sized work needs the judgment lane"
    if (t.get("clarity_level") or "") == "low":
        return "claude", "low-clarity ask needs interpretation"
    est = t.get("estimate_hours")
    if est is not None and float(est) > 6:
        return "claude", f"estimate {est}h is above the codex threshold (6h)"
    return "codex", (f"small, well-specified {t['type']}"
                     + (f" (~{est}h)" if est is not None else ""))


def _review_engine(build_engine: str) -> str:
    """Cross-engine review: the reviewer should not share the builder's blind
    spots, so run it on the opposite engine whenever one is available."""
    if build_engine == "codex":
        return "claude"
    return "codex" if CODEX_ENABLED else "claude"


def _engine_trailer(engine: str) -> str:
    return ("Co-Authored-By: Codex <noreply@openai.com>" if engine == "codex"
            else "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>")


def _mirror_agents_md(workdir: Path) -> None:
    """Codex reads AGENTS.md where Claude reads CLAUDE.md — keep ONE canonical
    guidance file by symlinking AGENTS.md → CLAUDE.md in the working tree, and
    keep the mirror out of commits via .git/info/exclude. Best-effort."""
    if not CODEX_ENABLED:
        return
    try:
        cl, ag = workdir / "CLAUDE.md", workdir / "AGENTS.md"
        if cl.is_file() and not ag.exists():
            ag.symlink_to("CLAUDE.md")
            excl = MAIN_CHECKOUT / ".git" / "info" / "exclude"
            if excl.parent.is_dir():
                lines = excl.read_text().splitlines() if excl.is_file() else []
                if "AGENTS.md" not in lines:
                    with open(excl, "a") as fh:
                        fh.write("\nAGENTS.md\n")
    except Exception as e:
        log(f"  (AGENTS.md mirror skipped: {e})")


def process_ticket(t: dict) -> None:
    tid = t["id"]
    log(f"Picking up {t['ref']} — {t['title']!r} (priority {t['priority']})")
    act = lambda d: dk.set_activity(tid, d)

    try:
        workdir, _branch = workdir_for(t)
    except subprocess.CalledProcessError as e:
        return _stall(tid, f"worktree setup failed: {e.stderr or e}")

    # --- Engine routing FIRST, so the board shows the engine (and why) from
    # the moment the ticket is picked up, not only once development starts.
    # choose_engine needs nothing from assessment — it reads the ticket itself.
    engine, route_why = choose_engine(t)
    if (t.get("engine") or "") != engine:
        try:
            dk.update_ticket(tid, engine=engine)
        except Exception:
            pass
    if CODEX_ENABLED:
        dk.add_event(tid, "note", actor="agent",
                     summary=f"🔀 Build engine: **{engine}**"
                             + (f" — {route_why}" if route_why else "")
                             + f". Review runs on **{_review_engine(engine)}**.",
                     payload={"engine": engine})
        log(f"  engine → {engine}" + (f" ({route_why})" if route_why else ""))

    # --- Assessment (read-only) ---
    dk.transition(tid, "assessment", actor="agent", summary="Picked up by the agent")
    act("Reading the codebase to assess the request")
    a = run_phase(assess_prompt(t), workdir, allowed_tools=READONLY_TOOLS,
                  disallowed_tools=["Edit", "Write"], permission_mode="default",
                  max_turns=15, max_budget_usd=1.5, label="assessment", on_activity=act)
    if a["is_error"]:
        return recover(t, "assessment", a["text"], wt=workdir)
    verdict, questions = parse_verdict(a["text"], "VERDICT")
    dk.add_event(tid, "assessment", summary=_strip_control(a["text"]), actor="agent",
                 payload={"cost_usd": a["cost"], "turns": a["turns"], "duration_secs": a["duration"],
                          "engine": a.get("engine", "claude"), "model": a.get("model", "")})
    log(f"  assessment done (verdict={verdict or 'PROCEED'}, ${a['cost']:.3f}, {a['turns']} turns)")

    # Follow-up detection: the agent explored the codebase, so its RELATED call
    # is trusted (confirmed link) — the shipped ticket's fix didn't stick.
    rel, rel_why = parse_verdict(a["text"], "RELATED")
    rel_m = _re.search(r"(\d+)", rel or "")
    if rel_m:
        tgt = int(rel_m.group(1))
        try:
            if any(s["id"] == tgt for s in dk.shipped_tickets()):
                ln = dk.add_link(tid, tgt, source="agent", status="confirmed",
                                 note=(rel_why or "")[:300])
                if ln:
                    dk.add_event(tid, "note", actor="agent",
                                 summary=f"Assessment links this to shipped DKT-{tgt}"
                                         f"{': ' + rel_why if rel_why else ''} — counts "
                                         f"against DKT-{tgt}'s post-ship health.")
                    log(f"  related → DKT-{tgt} ({rel_why[:80] if rel_why else 'follow-up'})")
        except Exception as e:
            log(f"  (related-link failed: {e})")

    # Hybrid grooming gate: bounce vague P0/P1; best-effort the rest.
    if verdict == "NEEDS_INFO" and t["priority"] in ("P0", "P1"):
        q = questions or "The requester needs to clarify the ask before work can start."
        dk.add_event(tid, "comment", summary=f"Needs clarification: {q}", actor="agent")
        dk.transition(tid, "needs_info", actor="agent",
                      summary="Bounced for clarification (grooming gate)")
        dk.enqueue_notification(tid, t.get("created_by") or _notify_default(), "needs_info",
                                subject=f"Docket {t['ref']}: needs your input",
                                body=q)
        log(f"  → Needs Info (bounced): {q}")
        return
    if verdict == "NEEDS_INFO":
        dk.add_event(tid, "note", actor="agent",
                     summary="Ask is a bit vague but low-priority — proceeding best-effort "
                             f"with assumptions. Open question: {questions or 'n/a'}")

    # --- Planning (read-only) ---
    dk.transition(tid, "planning", actor="agent")
    act("Drafting an implementation plan")
    p = run_phase(plan_prompt(t, a["text"]), workdir, allowed_tools=READONLY_TOOLS,
                  disallowed_tools=["Edit", "Write"], permission_mode="default",
                  max_turns=20, max_budget_usd=1.5, label="planning", on_activity=act)
    if p["is_error"]:
        return recover(t, "planning", p["text"], wt=workdir)
    dk.add_event(tid, "plan", summary=p["text"], actor="agent",
                 payload={"cost_usd": p["cost"], "turns": p["turns"], "duration_secs": p["duration"],
                          "engine": p.get("engine", "claude"), "model": p.get("model", "")})
    log(f"  plan done (${p['cost']:.3f}, {p['turns']} turns)")

    if not WRITES_ENABLED:
        act("Plan ready — autonomous code-gen is disabled")
        dk.add_event(tid, "note", actor="agent",
                     summary="Assessment + plan complete. Autonomous code generation is "
                             "disabled (set DOCKET_AGENT_WRITES=1 to let the agent "
                             "implement, self-review and open a PR). Parked at Planning.")
        log(f"  writes disabled — parked {t['ref']} at Planning")
        return

    # --- In Development + Self-Review: a REAL, bounded corrective loop ---
    # The agent implements, reviews its own work, and — crucially — gets up to
    # MAX_DEV_PASSES attempts to ACT on what the review found before giving up.
    # The old code promised "one corrective loop" but stalled on the first FAIL
    # without ever re-running development; that single bug stalled most tickets.
    wt, branch = ensure_worktree(t)
    _mirror_agents_md(wt)
    dk.transition(tid, "in_development", actor="agent")
    # What this ticket changed is measured against `base_ref`. For branch work
    # that's BASE_BRANCH; in direct_main we ARE on BASE_BRANCH, so diffing against
    # it is always empty — capture the pre-work tip and diff against that instead.
    base_ref = BASE_BRANCH
    if DIRECT_MAIN:
        base_ref = (subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"],
                                    capture_output=True, text=True).stdout.strip()
                    or BASE_BRANCH)
    iteration = int(t.get("iteration") or 0)
    fix_feedback = ""
    pr_body = ""
    passed = False
    verified = False          # True only on an EXECUTED, evidence-backed PASS
    unverified_reason = ""    # set when the reviewer returns UNVERIFIED
    for attempt in range(1, MAX_DEV_PASSES + 1):
        # DEFECT escalation: if the default model couldn't pass its own review,
        # corrective passes use more capability aimed exactly at the bug rather
        # than re-running what just failed — for claude that's the stronger
        # model; for codex the escalation is CROSS-ENGINE (claude takes over).
        # A resubmit (iteration > 0) means a prior shipped fix was rejected, so
        # we start escalated immediately rather than re-earning the bug.
        escalated_pass = attempt >= 2 or iteration >= 1
        pass_engine = "claude" if (engine == "codex" and escalated_pass) else engine
        pass_model = STRONG_MODEL if (escalated_pass and pass_engine == "claude") else None
        if attempt == 1:
            act(f"Implementing the change ({pass_engine})")
            impl_prompt = implement_prompt(t, p["text"])
        else:
            act(f"Addressing self-review feedback (pass {attempt}/{MAX_DEV_PASSES}, "
                f"{pass_model or pass_engine})")
            impl_prompt = reimplement_prompt(t, p["text"], fix_feedback)
        if pass_engine != engine:
            dk.add_event(tid, "note", actor="agent",
                         summary=f"🔀 Engine escalation: corrective pass {attempt} runs on "
                                 f"**claude** ({STRONG_MODEL}) after codex couldn't clear review.",
                         payload={"engine": pass_engine})
        i = run_phase(impl_prompt, wt, allowed_tools=WRITE_TOOLS,
                      permission_mode="acceptEdits", max_turns=40, max_budget_usd=5.0,
                      model=pass_model, label="implementation", on_activity=act,
                      engine=pass_engine)
        if i["is_error"]:
            return recover(t, "implementation", i["text"], wt=wt)
        label = "**Implemented**" if attempt == 1 else f"**Revised (pass {attempt})**"
        dk.add_event(tid, "note", summary=f"{label}\n\n" + i["text"][:1500], actor="agent",
                     payload={"cost_usd": i["cost"], "turns": i["turns"],
                              "duration_secs": i["duration"], "engine": i.get("engine", pass_engine),
                              "model": i.get("model", ""),
                              **({"tokens": i["tokens"]} if i.get("tokens") else {})})
        # Commit anything this pass produced.
        _git(wt, ["add", "-A"])
        if subprocess.run(["git", "-C", str(wt), "diff", "--cached", "--quiet"]).returncode != 0:
            staged_stat = subprocess.run(
                ["git", "-C", str(wt), "diff", "--cached", "--stat"],
                capture_output=True, text=True).stdout
            _git(wt, ["commit", "-m", f"DKT-{tid}: {t['title']}",
                      "-m", _pr_summary(t, i["text"], staged_stat),
                      "-m", _engine_trailer(pass_engine)])
        # The real "did this ticket produce work?" measure is the whole branch vs
        # the base branch — NOT just this run's staged edits. A requeued ticket
        # whose fix was already committed on its branch in a prior pass makes no
        # *new* edits yet is NOT a no-op. Only an empty branch is.
        files_stat = subprocess.run(
            ["git", "-C", str(wt), "diff", "--stat", base_ref, "HEAD"],
            capture_output=True, text=True).stdout
        if not files_stat.strip():
            # Truly nothing on the branch. Route through triage: a real no-op
            # usually means the agent couldn't locate the change or the ask is
            # underspecified → NEEDS_INFO, not a dead-end stall.
            why = ("the implementation phase produced no changes — the branch is identical "
                   f"to {BASE_BRANCH}. The agent could not determine what to change — typically "
                   "the ask lacks a concrete repro, exact location, or expected behaviour."
                   if attempt == 1 else
                   f"the corrective pass made no edits despite self-review feedback: {fix_feedback[:200]}")
            return recover(t, "implementation (no changes produced)", why, wt=wt)
        pr_body = _pr_summary(t, i["text"], files_stat)

        # --- Self-Review (writes on, so it can also make small fixes itself).
        # Cross-engine when possible: a reviewer from the OTHER model family
        # doesn't share the builder's blind spots. ---
        rev_engine = _review_engine(pass_engine)
        dk.transition(tid, "self_review", actor="agent")
        act(f"Reviewing the work + running checks ({rev_engine})")
        r = run_phase(review_prompt(t), wt, allowed_tools=WRITE_TOOLS,
                      permission_mode="acceptEdits", max_turns=25, max_budget_usd=3.0,
                      label="self-review", on_activity=act, engine=rev_engine)
        if r["is_error"] and rev_engine == "codex":
            # A broken review engine must not sink a finished build — fall back
            # to a same-engine (claude) review rather than recovering the ticket.
            log(f"  codex review failed ({r['text'][:80]}) — falling back to claude review")
            r = run_phase(review_prompt(t), wt, allowed_tools=WRITE_TOOLS,
                          permission_mode="acceptEdits", max_turns=25, max_budget_usd=3.0,
                          label="self-review", on_activity=act)
            rev_engine = "claude"
        if r["is_error"]:
            return recover(t, "self-review", r["text"], wt=wt, diff_summary=files_stat)
        # The reviewer may have edited files; fold any such fixes into the commit.
        _git(wt, ["add", "-A"])
        if subprocess.run(["git", "-C", str(wt), "diff", "--cached", "--quiet"]).returncode != 0:
            _git(wt, ["commit", "--amend", "--no-edit"])
        rev_label = ("**Self-review**" if rev_engine == pass_engine
                     else f"**Cross-review** ({rev_engine} reviewing {pass_engine}'s work)")
        dk.add_event(tid, "note", summary=rev_label + "\n\n" + _strip_control(r["text"])[:1500],
                     actor="agent", payload={"cost_usd": r["cost"], "turns": r["turns"],
                                             "duration_secs": r["duration"],
                                             "engine": r.get("engine", rev_engine),
                                             "model": r.get("model", ""),
                                             **({"tokens": r["tokens"]} if r.get("tokens") else {})})
        rv, fix = parse_verdict(r["text"], "REVIEW")
        if rv == "PASS":
            # Cleared with executed, evidence-backed verification.
            passed = True
            verified = True
            break
        if rv != "FAIL":
            # UNVERIFIED (or any non-FAIL, non-PASS verdict): the change is
            # plausible but the reviewer could NOT prove it here. Ship it for a
            # human test, but HONESTLY — flagged unverified, never claimed done.
            passed = True
            verified = False
            unverified_reason = fix or _strip_control(r["text"])[:600]
            break
        fix_feedback = fix or _strip_control(r["text"])[:600]
        if attempt < MAX_DEV_PASSES:
            dk.transition(tid, "in_development", actor="agent",
                          summary=f"Self-review found issues — iterating (pass {attempt + 1}/{MAX_DEV_PASSES})")
            log(f"  self-review FAIL, iterating (pass {attempt + 1}): {fix_feedback[:100]}")

    if not passed:
        # Exhausted the model-escalated corrective loop. Don't just stall —
        # triage decides whether this is really an underspecified ask (bounce to
        # the requester) or a genuine blocker (human-gated stall).
        files_stat = subprocess.run(["git", "-C", str(wt), "diff", "--stat", base_ref],
                                    capture_output=True, text=True).stdout
        return recover(t, f"self-review (still failing after {MAX_DEV_PASSES} dev passes)",
                       fix_feedback, wt=wt, diff_summary=files_stat)

    # Record what the change touched — the join key for post-ship telemetry.
    try:
        paths, routes = extract_touched(wt, base_ref)
        dk.update_ticket(tid, touched_paths=json.dumps(paths),
                         touched_routes=json.dumps(routes))
        if routes:
            log(f"  touched routes: {', '.join(routes[:5])}")
    except Exception as e:
        log(f"  (touched extraction failed: {e})")

    # --- Verification status: be HONEST about whether the agent proved the fix.
    # The whole point of the hardened gate is that "done" must mean verified;
    # an unproven change still goes for human test, but clearly labelled so the
    # reviewer knows to reproduce the original problem themselves. ---
    if verified:
        dk.add_event(tid, "note", actor="agent",
                     summary="✅ Verified — the reviewer reproduced the ticket scenario "
                             "and observed the acceptance criteria met.")
    else:
        dk.add_event(tid, "note", actor="agent",
                     summary="⚠️ NOT verified by the agent — the fix could not be "
                             "reproduced in the worktree. Needs a human test against the "
                             "real app." + (f" Reason: {unverified_reason[:400]}"
                                            if unverified_reason else ""))

    # --- Test instructions for the human reviewer (User Review phase) ---
    act("Writing test instructions for the reviewer")
    ti = run_claude(test_instructions_prompt(t), wt, allowed_tools=READONLY_TOOLS,
                    disallowed_tools=["Edit", "Write"], permission_mode="default",
                    max_turns=10, max_budget_usd=1.0, on_activity=act)
    if not ti["is_error"] and ti["text"].strip():
        instructions = _strip_control(ti["text"])
        if not verified:
            banner = (
                "> ⚠️ **The agent could NOT verify this fix** — it was not reproduced "
                "in an automated check. Please reproduce the ORIGINAL problem first, "
                "then confirm these steps actually resolve it.\n"
                + (f">\n> _Why it couldn't be auto-verified:_ {unverified_reason[:300]}\n"
                   if unverified_reason else "")
                + "\n---\n\n"
            )
            instructions = banner + instructions
        dk.update_ticket(tid, test_instructions=instructions)

    # --- direct_main: no branch, no PR. Work is already committed on BASE_BRANCH
    # in the main checkout; push to the remote if one exists, then advance the
    # ticket itself (self_review → user_review → done) so a full build flows
    # ticket-by-ticket without a PR gate. ---
    if DIRECT_MAIN:
        ref = dk.get_ticket(tid)["ref"]
        head = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        pushed_note = ""
        if PUSH_ENABLED and _has_remote():
            if _git(wt, ["push", REMOTE, BASE_BRANCH]):
                pushed_note = f" and pushed to {REMOTE}/{BASE_BRANCH}"
            else:
                log(f"  ⚠ DKT-{tid}: push to {REMOTE}/{BASE_BRANCH} failed — committed locally only")
        vstatus = ("✅ Agent-verified (reproduced the fix)." if verified
                   else "⚠️ NOT agent-verified — reproduce the original problem when testing.")
        dk.update_ticket(tid, branch=BASE_BRANCH)
        dk.add_event(tid, "note", actor="agent",
                     summary=f"Committed directly to `{BASE_BRANCH}` ({head[:7]}){pushed_note} — "
                             f"no PR (direct_main mode). {vstatus}")
        dk.transition(tid, "user_review", actor="agent",
                      summary=f"Committed to {BASE_BRANCH} ({head[:7]}) — no PR (direct_main)")
        dk.transition(tid, "done", actor="agent",
                      summary=f"Shipped to {BASE_BRANCH}{pushed_note}")
        _record_roadmap_done(tid)
        log(f"  → DKT-{tid}: shipped to {BASE_BRANCH} ({head[:7]}){pushed_note} "
            f"[{'verified' if verified else 'UNVERIFIED'}]")
        return

    # --- PR (push branch + record compare URL; never auto-merge) ---
    if not PUSH_ENABLED:
        dk.update_ticket(tid, branch=branch)
        dk.add_event(tid, "note", actor="agent",
                     summary=f"Local branch '{branch}' ready in {wt} — push is disabled "
                             "(DOCKET_AGENT_PUSH=0). Inspect the diff, then push manually "
                             "to open a PR.")
        dk.transition(tid, "pr", actor="agent", summary="Local branch ready (push held for review)")
        log(f"  → local branch ready (push held): {branch} @ {wt}")
        return
    act("Pushing the branch + opening a PR")
    # --force-with-lease: docket/* branches are agent-owned. A resubmit after a
    # base-branch change rebuilds the branch on a new root, which the remote's
    # stale copy from the previous attempt rejects as non-fast-forward (real
    # case: DKT-9 re-shipped from main onto the reunion branch). The lease
    # still refuses to overwrite pushes the agent hasn't seen.
    pushed = _git(wt, ["push", "-u", "--force-with-lease", REMOTE, branch])
    if not pushed:
        return recover(t, "push",
                       "git push failed (stale lease on the docket/* branch, network, or auth)",
                       wt=wt)
    ref = dk.get_ticket(tid)["ref"]
    pr_url = create_pr(branch, f"{ref}: {t['title']}", pr_body)
    real_pr = bool(pr_url)
    if not pr_url:  # no token / API failure → compare URL, Neil opens it by hand
        pr_url = f"https://github.com/{REPO_SLUG}/compare/{BASE_BRANCH}...{branch}?expand=1"
    dk.update_ticket(tid, branch=branch, pr_url=pr_url)
    dk.transition(tid, "pr", actor="agent",
                  summary="Branch pushed; PR opened" if real_pr
                          else "Branch pushed; PR ready for review")
    vstatus = ("✅ Agent-verified (reproduced the fix)." if verified
               else "⚠️ NOT agent-verified — reproduce the original problem when testing.")
    dk.enqueue_notification(tid, _notify_default(), "pr_ready",
                            subject=f"Docket {ref}: PR ready"
                                    + ("" if verified else " (unverified)"),
                            body=f"{vstatus}\n\n{pr_url}")
    log(f"  → PR ready ({'verified' if verified else 'UNVERIFIED'}): {pr_url}")

    # Auto-merge (opt-in): squash-merge the real PR, then mark the ticket shipped.
    # Requires a real PR object (token); with only a compare URL it waits at PR.
    if AUTO_MERGE:
        num = _pr_number(pr_url)
        if real_pr and num and merge_pr(num):
            dk.update_ticket(tid, pr_url=pr_url.split("#")[0])
            dk.transition(tid, "user_review", actor="agent",
                          summary=f"PR #{num} auto-merged")
            dk.transition(tid, "done", actor="agent",
                          summary=f"Auto-merged & shipped (PR #{num})")
            _record_roadmap_done(tid)
            log(f"  → DKT-{tid}: PR #{num} auto-merged & shipped")
        else:
            log(f"  → DKT-{tid}: auto-merge skipped "
                f"({'no real PR — needs a GitHub token' if not real_pr else 'merge not allowed/conflicting'})"
                f"; left at PR for manual merge")


_ROUTE_DECOR_RE = _re.compile(
    r'@(?:router|app)\.(?:get|post|put|patch|delete)\(\s*["\']([^"\']*)["\']')
_PREFIX_RE = _re.compile(r'APIRouter\([^)]*prefix\s*=\s*["\']([^"\']+)["\']', _re.S)


def extract_touched(wt: Path, base: str = "") -> tuple[list, list]:
    """What the implementation touched: changed files, plus a best-effort list of
    API route templates (from @router decorators in the changed hunks, resolved
    against the file's APIRouter prefix). This is the join key that lets platform
    telemetry measure the shipped feature's real traffic and error rate. `base` is
    the ref to diff against — BASE_BRANCH for branch work, or the pre-work SHA in
    direct_main mode (where the work IS on BASE_BRANCH)."""
    base = base or BASE_BRANCH
    r = subprocess.run(["git", "-C", str(wt), "diff", "--name-only", base],
                       capture_output=True, text=True)
    paths = [p for p in r.stdout.split() if p]
    routes: set = set()
    for p in paths:
        if not (p.startswith("backend/") and p.endswith(".py")):
            continue
        f = wt / p
        if not f.exists():
            continue
        src = f.read_text(errors="ignore")
        m = _PREFIX_RE.search(src)
        prefix = m.group(1) if m else ""
        diff = subprocess.run(["git", "-C", str(wt), "diff", "-U2", base, "--", p],
                              capture_output=True, text=True).stdout
        found = {prefix + dm.group(1)
                 for dm in _ROUTE_DECOR_RE.finditer(diff) if prefix + dm.group(1)}
        if not found:
            # The change was inside a handler body (decorator outside the diff
            # context). A bug anywhere in the file can break any of its routes,
            # so attribute the whole file's routes — capped to stay sane.
            found = {prefix + dm.group(1)
                     for dm in _ROUTE_DECOR_RE.finditer(src) if prefix + dm.group(1)}
            if len(found) > 12:
                found = set()
        routes |= found
    return paths[:100], sorted(routes)[:50]


def _git(cwd: Path, args: list) -> bool:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if r.returncode != 0:
        log(f"  git {' '.join(args[:2])} failed: {r.stderr.strip()[:200]}")
        return False
    return True


def _has_remote() -> bool:
    """True if the configured REMOTE exists. direct_main only pushes to a remote
    when one is present; greenfield projects have none and stay local."""
    r = subprocess.run(["git", "-C", str(MAIN_CHECKOUT), "remote", "get-url", REMOTE],
                       capture_output=True, text=True)
    return r.returncode == 0 and bool(r.stdout.strip())


def create_pr(branch: str, title: str, body: str) -> str:
    """Create a real PR object via the GitHub API when a token is configured.
    Returns the PR html_url, or '' on any failure / no token (caller falls back
    to the compare URL). 422 'already exists' resolves to the existing PR."""
    if not GITHUB_TOKEN:
        return ""
    api = f"https://api.github.com/repos/{REPO_SLUG}/pulls"
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}",
               "Accept": "application/vnd.github+json",
               "Content-Type": "application/json"}
    payload = json.dumps({"title": title, "head": branch, "base": BASE_BRANCH,
                          "body": body}).encode()
    try:
        req = urllib.request.Request(api, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp).get("html_url", "")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        if e.code == 422 and "already exists" in detail:
            try:  # find the existing open PR for this head
                q = f"{api}?head={REPO_SLUG.split('/')[0]}:{branch}&state=open"
                req = urllib.request.Request(q, headers=headers)
                with urllib.request.urlopen(req, timeout=30) as resp:
                    prs = json.load(resp)
                    if prs:
                        return prs[0].get("html_url", "")
            except Exception:
                pass
        log(f"  PR creation failed ({e.code}): {detail}")
    except Exception as e:
        log(f"  PR creation failed: {e}")
    return ""


def _pr_number(pr_url: str) -> int:
    """Extract the PR number from a .../pull/<n> URL, or 0 if not a real PR URL
    (e.g. a compare URL)."""
    m = _re.search(r"/pull/(\d+)", pr_url or "")
    return int(m.group(1)) if m else 0


def merge_pr(number: int, method: str = "squash") -> bool:
    """Merge a PR via the GitHub API. Requires a token. Returns True on success;
    False (with a log line) if not mergeable / conflicting / no token."""
    if not GITHUB_TOKEN or not number:
        return False
    api = f"https://api.github.com/repos/{REPO_SLUG}/pulls/{number}/merge"
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}",
               "Accept": "application/vnd.github+json",
               "Content-Type": "application/json"}
    payload = json.dumps({"merge_method": method}).encode()
    try:
        req = urllib.request.Request(api, data=payload, headers=headers, method="PUT")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return bool(json.load(resp).get("merged"))
    except urllib.error.HTTPError as e:
        log(f"  PR #{number} merge failed ({e.code}): {e.read().decode(errors='replace')[:200]}")
    except Exception as e:
        log(f"  PR #{number} merge failed: {e}")
    return False


# ---------------------------------------------------------------------------
# Notification delivery (msmtp) — drains the queue the storage layer fills.
# Stays a graceful no-op until msmtp + an SMTP credential are configured, so
# notifications simply wait in 'pending' rather than getting lost.
# ---------------------------------------------------------------------------

def _msmtp_ready() -> bool:
    return bool(shutil.which("msmtp")) and (
        Path("/etc/msmtprc").exists() or Path.home().joinpath(".msmtprc").exists())


def _send_email(to_addr: str, subject: str, body: str) -> bool:
    msg = (f"From: {MAIL_FROM}\nTo: {to_addr}\nSubject: {subject}\n"
           f"Content-Type: text/plain; charset=utf-8\n\n{body}\n")
    r = subprocess.run(["msmtp", to_addr], input=msg, capture_output=True,
                       text=True, timeout=60)
    if r.returncode != 0:
        log(f"  msmtp failed: {r.stderr.strip()[:200]}")
    return r.returncode == 0


def drain_notifications() -> None:
    """Send pending notifications via msmtp. Recipients without an email on
    file are marked 'skipped' (the in-app badge still covers them)."""
    pending = dk.pending_notifications()
    if not pending:
        return
    if not _msmtp_ready():
        return  # leave queued; they deliver once msmtp + the SMTP cred land
    for n in pending:
        addr = tester_email(n["recipient"])
        if not addr:
            dk.mark_notification(n["id"], "skipped")
            continue
        subject = n["subject"] or f"Docket: {n['event'].replace('_', ' ')}"
        ok = _send_email(addr, subject, n["body"] or subject)
        dk.mark_notification(n["id"], "sent" if ok else "failed")
        log(f"  notification #{n['id']} → {n['recipient']}: {'sent' if ok else 'FAILED'}")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Merge reconciler — advance a ticket out of 'pr' once its PR is merged.
# The agent only pushes a branch + compare URL (no PAT needed); Neil opens and
# merges the real PR on GitHub. This poll closes the loop: it spots the merge and
# moves the ticket pr -> user_review so it doesn't sit in PR forever.
# ---------------------------------------------------------------------------

GH_API = "https://api.github.com"
# Throttle the GitHub poll: unauthenticated REST is 60 req/hr per IP. 90s = 40/hr
# worst case (only while a ticket actually sits in 'pr'), leaving headroom while
# keeping post-merge latency low. With a PAT the limit is 5000/hr — drop this to
# ~20s via DOCKET_MERGE_POLL for near-instant detection.
MERGE_POLL_SECS = int(os.environ.get("DOCKET_MERGE_POLL", "90"))
_last_merge_check = 0.0


def _gh_get(path: str):
    """GitHub REST GET. Unauthenticated works on the public repo; uses
    GITHUB_TOKEN automatically when present (also lifts the rate limit)."""
    req = urllib.request.Request(
        f"{GH_API}{path}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "docket-agent"})
    if GITHUB_TOKEN:
        req.add_header("Authorization", f"Bearer {GITHUB_TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except (urllib.error.URLError, json.JSONDecodeError, OSError, ValueError) as e:
        log(f"  github poll failed: {e}")
        return None


def _branch_head_sha(branch: str) -> str:
    """Current tip SHA of `branch` on origin, via the GitHub API. Returns '' if
    the branch is gone (deleted after merge) or the lookup fails — callers treat
    an unknown tip as 'nothing to compare against', i.e. no post-merge drift."""
    data = _gh_get(f"/repos/{REPO_SLUG}/branches/"
                   f"{urllib.parse.quote(branch, safe='/')}")
    if isinstance(data, dict):
        return (data.get("commit") or {}).get("sha") or ""
    return ""


def _flagged_incomplete_tip(tid: int) -> str:
    """The branch tip we've already warned about for this ticket's incomplete
    merge, if any — so the poll alerts once per new tip, not every cycle."""
    for e in reversed(dk.get_events(tid)):
        p = e.get("payload")
        if isinstance(p, dict) and p.get("merge_incomplete"):
            return p.get("branch_tip") or ""
    return ""


# ---------------------------------------------------------------------------
# Auto-deploy — keep the deployed working tree on the tip of BASE_BRANCH
# ---------------------------------------------------------------------------

AUTO_DEPLOY = os.environ.get("DOCKET_AUTO_DEPLOY", "0") == "1"
DEPLOY_CMD = os.environ.get("DOCKET_DEPLOY_CMD", "")
_DEPLOY_STATE = MAIN_CHECKOUT / ".docket" / "data" / "deploy_state.json"
_DEPLOY_LOG = MAIN_CHECKOUT / ".docket" / "data" / "deploys.jsonl"
_last_deploy_check = 0.0
_deploy_warned = ""     # last condition already logged, to avoid spamming


def _sh_out(args, cwd) -> str:
    r = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True)
    return (r.stdout or "").strip()


def _deploy_record(entry: dict) -> None:
    try:
        _DEPLOY_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_DEPLOY_LOG, "a") as fh:
            fh.write(json.dumps({"ts": dk.utcnow_iso(), **entry}) + "\n")
    except OSError:
        pass


def _deploy_state() -> dict:
    try:
        return json.loads(_DEPLOY_STATE.read_text())
    except (OSError, ValueError):
        return {}


def _save_deploy_state(state: dict) -> None:
    try:
        _DEPLOY_STATE.parent.mkdir(parents=True, exist_ok=True)
        _DEPLOY_STATE.write_text(json.dumps(state))
    except OSError:
        pass


def auto_deploy_tick() -> None:
    """Opt-in ([deploy] auto=true in config): watch BASE_BRANCH and keep the
    project's working tree — the deployment — on its tip. When a merge lands:
    fetch, ff-only update (only while the tree is actually checked out on
    BASE_BRANCH; otherwise hold and say so), then run the configured deploy
    command. Every action lands in .docket/data/deploys.jsonl. Also covers
    direct_main: a local commit advances HEAD, which triggers the deploy cmd."""
    global _last_deploy_check, _deploy_warned
    if not AUTO_DEPLOY:
        return
    now = time.monotonic()
    if now - _last_deploy_check < MERGE_POLL_SECS:
        return
    _last_deploy_check = now
    root = MAIN_CHECKOUT

    has_remote = bool(_sh_out(["git", "remote"], root))
    if has_remote:
        subprocess.run(["git", "fetch", REMOTE, BASE_BRANCH], cwd=str(root),
                       capture_output=True, text=True)

    cur = _sh_out(["git", "branch", "--show-current"], root)
    if cur != BASE_BRANCH:
        tip = _sh_out(["git", "rev-parse", f"{REMOTE}/{BASE_BRANCH}"], root) if has_remote else ""
        key = f"held:{cur}:{tip}"
        if key != _deploy_warned:
            _deploy_warned = key
            log(f"auto-deploy HELD: working tree is on '{cur}' but the merge "
                f"branch is '{BASE_BRANCH}' — check out {BASE_BRANCH} here to deploy.")
            _deploy_record({"ok": False, "held": True,
                            "reason": f"tree on '{cur}', target '{BASE_BRANCH}'",
                            "target_tip": tip})
        return

    pull_err = ""
    if has_remote:
        r = subprocess.run(["git", "merge", "--ff-only", f"{REMOTE}/{BASE_BRANCH}"],
                           cwd=str(root), capture_output=True, text=True)
        if r.returncode != 0:
            pull_err = (r.stderr or r.stdout or "").strip()[-400:]
    after = _sh_out(["git", "rev-parse", "HEAD"], root)

    state = _deploy_state()
    if not state.get("last_sha"):
        # First tick after enabling: baseline on the current tree, don't deploy.
        _save_deploy_state({"last_sha": after})
        log(f"auto-deploy armed at {after[:7]} on {BASE_BRANCH}")
        return
    if pull_err:
        key = f"pullfail:{after}"
        if key != _deploy_warned:
            _deploy_warned = key
            log(f"auto-deploy: ff-only update failed (diverged/dirty tree?): {pull_err}")
            _deploy_record({"ok": False, "pull_error": pull_err, "head": after})
        return
    if after == state.get("last_sha"):
        return

    entry = {"from": state["last_sha"], "to": after}
    if DEPLOY_CMD:
        log(f"auto-deploy: {state['last_sha'][:7]} → {after[:7]}, running deploy cmd")
        try:
            # Real deploy scripts install deps / rebuild containers / run
            # migrations / health-check — give them 30 minutes, not 10.
            r = subprocess.run(DEPLOY_CMD, shell=True, cwd=str(root),
                               capture_output=True, text=True, timeout=1800)
            entry["cmd_rc"] = r.returncode
            entry["cmd_tail"] = ((r.stdout or "") + (r.stderr or ""))[-600:]
            entry["ok"] = r.returncode == 0
        except subprocess.TimeoutExpired:
            entry["ok"] = False
            entry["cmd_tail"] = "deploy command timed out after 1800s"
    else:
        entry["ok"] = True   # pull-only deploy (e.g. a dev server watching the tree)
        log(f"auto-deploy: updated {state['last_sha'][:7]} → {after[:7]} (no deploy cmd)")
    _save_deploy_state({"last_sha": after})
    _deploy_record(entry)
    if not entry.get("ok"):
        log(f"auto-deploy: deploy cmd FAILED (rc={entry.get('cmd_rc')}) — see deploys.jsonl")


def reconcile_merged_prs() -> None:
    """Detect PRs merged on GitHub and advance their tickets pr -> user_review."""
    global _last_merge_check
    waiting = [t for t in dk.list_tickets("pr") if t.get("branch")]
    if not waiting:
        return
    now = time.monotonic()
    if now - _last_merge_check < MERGE_POLL_SECS:
        return
    _last_merge_check = now
    data = _gh_get(f"/repos/{REPO_SLUG}/pulls?state=closed&base={BASE_BRANCH}"
                   "&per_page=50&sort=updated&direction=desc")
    if not isinstance(data, list):
        return
    # Most-recently-merged PR per head branch (a branch can be reused across a
    # user-review bounce, opening a second PR — take the latest merge).
    merged: dict = {}
    for pr in data:
        ref = (pr.get("head") or {}).get("ref")
        if not (pr.get("merged_at") and ref):
            continue
        prev = merged.get(ref)
        if prev is None or pr["merged_at"] > prev["merged_at"]:
            merged[ref] = pr
    for t in waiting:
        pr = merged.get(t["branch"])
        if not pr:
            continue
        tid = t["id"]

        # Guard: did the branch keep moving AFTER the PR merged? GitHub freezes a
        # merged PR's head.sha at merge time; if the branch's live tip differs,
        # commit(s) landed post-merge and are NOT on main — the classic "PR merged
        # before the real fix commit landed" (DKT-42). Don't advance to
        # user_review over an incomplete merge: hold at the PR gate and alert so a
        # human re-merges, rather than testing a main that lacks the fix. This is
        # a SHA-identity check (not ancestry), so it's correct for squash/rebase/
        # merge alike.
        merged_sha = (pr.get("head") or {}).get("sha") or ""
        tip = _branch_head_sha(t["branch"])
        if merged_sha and tip and tip != merged_sha:
            if _flagged_incomplete_tip(tid) != tip:
                dk.add_event(
                    tid, "note", actor="github",
                    summary=(f"⚠ Incomplete merge: PR #{pr['number']} merged at "
                             f"{merged_sha[:7]}, but branch `{t['branch']}` has since "
                             f"advanced to {tip[:7]} — those commit(s) are NOT on main. "
                             f"Re-merge the branch (or open a fresh PR) before testing; "
                             f"auto-deploy will then ship it."),
                    payload={"merge_incomplete": True, "branch_tip": tip,
                             "merged_sha": merged_sha, "pr": pr["number"]})
                dk.enqueue_notification(
                    tid, _notify_default(), "pr_ready",
                    subject=f"Docket DKT-{tid}: merge INCOMPLETE — branch moved after merge",
                    body=(f"PR #{pr['number']} was merged at {merged_sha[:7]}, but branch "
                          f"`{t['branch']}` is now at {tip[:7]}. Commit(s) pushed after the "
                          f"merge are not on main, so the ticket is NOT actually shipped. "
                          f"Re-merge the branch before this goes to testing.\n\n"
                          f"{pr.get('html_url','')}"))
                log(f"  ⚠ DKT-{tid}: incomplete merge (merged {merged_sha[:7]} "
                    f"!= tip {tip[:7]}) — held at PR gate")
            continue  # stay in 'pr' until the branch is fully merged

        try:
            dk.update_ticket(tid, pr_url=pr.get("html_url") or t.get("pr_url"))
            dk.transition(tid, "user_review", actor="github",
                          summary=f"PR #{pr['number']} merged on GitHub — ready to test")
            dk.enqueue_user_review_notification(dk.get_ticket(tid))
            log(f"  → DKT-{tid}: PR #{pr['number']} merged → user_review")
        except ValueError as e:
            log(f"  merge reconcile skipped DKT-{tid}: {e}")


def run_once() -> bool:
    """Work the single highest-priority queued ticket. Returns True if one ran."""
    t = dk.next_in_queue()
    if not t:
        return False
    try:
        process_ticket(t)
    except Exception as e:
        _stall(t["id"], f"unexpected error: {e}")
    return True


def main() -> int:
    once = "--once" in sys.argv
    log(f"starting (writes={'ON' if WRITES_ENABLED else 'OFF'}, model={MODEL}, "
        f"engines={'+'.join(ENGINES)}"
        + (f" [codex={CODEX_MODEL} as {CODEX_USER or 'self'}]" if CODEX_ENABLED else "")
        + f", once={once})")
    for r in dk.requeue_stuck_agent_tickets():
        log(f"resumed DKT-{r['id']} (was {r['from']}) -> queued")
    if once:
        ran = run_once()
        log("worked one ticket" if ran else "queue empty")
        return 0
    while True:
        try:
            reconcile_merged_prs()
            auto_deploy_tick()
            drain_notifications()
            if not run_once():
                time.sleep(POLL_SECS)
        except KeyboardInterrupt:
            log("stopping")
            return 0
        except Exception as e:
            log(f"loop error: {e}")
            time.sleep(POLL_SECS)


if __name__ == "__main__":
    sys.exit(main())
