"""Engine quota snapshots — how much claude / codex subscription budget is left.

Claude: the Claude Code OAuth token (~/.claude/.credentials.json of the user the
services run as) queries the official usage endpoint; utilization is live.
Codex: the CLI has no usage query, but every run appends `rate_limits` blocks to
its session logs under CODEX_HOME/sessions — we read the most recent one, so the
figure is "as of the last codex run" rather than live.

Availability is reported per engine as percent-left of the BINDING window (the
most-used of the session/weekly windows), plus a combined mean across engines.
Snapshots are cached in-process for TTL seconds — both the web endpoint and any
future router logic can poll freely.
"""
from __future__ import annotations

import glob
import json
import os
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

TTL = 60.0
_cache: dict = {"ts": 0.0, "data": None}


def _claude_quota() -> dict:
    cred = Path.home() / ".claude" / ".credentials.json"
    tok = json.loads(cred.read_text())["claudeAiOauth"]["accessToken"]
    req = urllib.request.Request(
        "https://api.anthropic.com/api/oauth/usage",
        headers={"Authorization": f"Bearer {tok}",
                 "anthropic-beta": "oauth-2025-04-20"})
    d = json.load(urllib.request.urlopen(req, timeout=10))
    fh = d.get("five_hour") or {}
    sd = d.get("seven_day") or {}
    used = max(float(fh.get("utilization") or 0), float(sd.get("utilization") or 0))
    return {
        "session_used_pct": fh.get("utilization"),
        "session_resets_at": fh.get("resets_at"),
        "weekly_used_pct": sd.get("utilization"),
        "weekly_resets_at": sd.get("resets_at"),
        "available_pct": round(max(0.0, 100.0 - used), 1),
    }


def _codex_home() -> str:
    env = os.environ.get("DOCKET_CODEX_HOME", "")
    if env:
        return env
    for cand in sorted(glob.glob("/home/*/.codex")) + ["/root/.codex"]:
        if os.path.isdir(os.path.join(cand, "sessions")):
            return cand
    return ""


def _codex_quota() -> dict:
    home = _codex_home()
    if not home:
        raise FileNotFoundError("no codex home with session logs found")
    files = sorted(glob.glob(os.path.join(home, "sessions", "*", "*", "*", "*.jsonl")),
                   key=os.path.getmtime, reverse=True)
    for f in files[:8]:
        found = None
        try:
            with open(f, errors="replace") as fh:
                for line in fh:
                    if '"rate_limits"' not in line:
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    rl = (ev.get("payload") or {}).get("rate_limits") or ev.get("rate_limits")
                    if rl:
                        found = (rl, os.path.getmtime(f))
        except OSError:
            continue
        if found:
            rl, mtime = found
            prim = rl.get("primary") or {}
            used = float(prim.get("used_percent") or 0)
            resets = prim.get("resets_at")
            return {
                "used_pct": used,
                "available_pct": round(max(0.0, 100.0 - used), 1),
                "window_minutes": prim.get("window_minutes"),
                "resets_at": (datetime.fromtimestamp(resets, tz=timezone.utc).isoformat()
                              if isinstance(resets, (int, float)) else resets),
                "plan": rl.get("plan_type"),
                "as_of": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
            }
    raise LookupError("no rate_limits found in recent codex session logs")


def get_quota(force: bool = False) -> dict:
    """Cached snapshot: {claude: {...}|{error}, codex: {...}|{error}, combined_available_pct}."""
    now = time.time()
    if not force and _cache["data"] is not None and now - _cache["ts"] < TTL:
        return _cache["data"]
    out: dict = {}
    for name, fn in (("claude", _claude_quota), ("codex", _codex_quota)):
        try:
            out[name] = fn()
        except Exception as e:  # per-engine: one side failing must not blank the other
            out[name] = {"error": f"{type(e).__name__}: {e}"[:200]}
    avail = [v["available_pct"] for v in out.values() if "available_pct" in v]
    out["combined_available_pct"] = round(sum(avail) / len(avail), 1) if avail else None
    out["fetched_at"] = datetime.now(tz=timezone.utc).isoformat()
    _cache.update(ts=now, data=out)
    return out
