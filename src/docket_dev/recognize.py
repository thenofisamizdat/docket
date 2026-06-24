"""Codebase recognition — run at `docket init` (or `docket recognize`).

Three headless-Claude passes that make a fresh install repo-aware:
  - profile_repo:    write `.docket/profile.md` (stack, build/test/run, layout) —
                     injected into the agent's assess/plan prompts as grounding.
  - ensure_claude_md: generate a CLAUDE.md at the repo root if absent.
  - seed_tickets:    scan for TODO/FIXME, missing tests, and obvious gaps, and
                     draft starter tickets into the Discussion zone for triage.

All passes are READ-ONLY explorations of the repo (except writing profile.md /
CLAUDE.md, which the functions do themselves — the agent only emits text).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List

from docket_dev import storage as dk
from docket_dev.agent import READONLY_TOOLS, run_claude
from docket_dev.config import CONFIG


def _strip_fence(text: str) -> str:
    """Drop a wrapping ```markdown / ``` code fence if the model added one."""
    t = (text or "").strip()
    if t.startswith("```"):
        lines = t.splitlines()
        lines = lines[1:]                       # drop opening ```lang
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]                  # drop closing ```
        t = "\n".join(lines).strip()
    return t


def _read_only_claude(prompt: str, *, max_turns=25, budget=2.0, on_activity=None) -> dict:
    return run_claude(
        prompt, CONFIG.project_root,
        allowed_tools=READONLY_TOOLS, disallowed_tools=["Edit", "Write"],
        permission_mode="default", max_turns=max_turns, max_budget_usd=budget,
        on_activity=on_activity,
    )


def profile_repo(on_activity=None) -> Path:
    """Generate and store a concise codebase profile at .docket/profile.md."""
    prompt = (
        "You are profiling a code repository so an autonomous dev agent can work "
        "on it effectively. Explore the repo (READ ONLY) and write a concise "
        "profile in Markdown covering:\n"
        "1. Languages, frameworks, and key dependencies.\n"
        "2. How to install deps, build, run, and test (exact commands if you can find them).\n"
        "3. The top-level directory layout and what each main area is for.\n"
        "4. Notable conventions (code style, testing approach, how features are structured).\n"
        "5. Entry points (where the app/CLI/service starts).\n\n"
        "Keep it under ~600 words. Output ONLY the Markdown profile — no preamble."
    )
    res = _read_only_claude(prompt, max_turns=30, budget=2.5, on_activity=on_activity)
    text = _strip_fence(res.get("text") or "")
    out = CONFIG.profile_path
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text or "# Codebase profile\n\n(Profile generation produced no output.)\n")
    return out


def ensure_claude_md(on_activity=None) -> bool:
    """Generate a CLAUDE.md at the repo root if one doesn't already exist.
    Returns True if a file was written. We write it but never auto-commit —
    the user decides whether to commit it."""
    target = CONFIG.project_root / "CLAUDE.md"
    if target.exists():
        return False
    prompt = (
        "Explore this repository (READ ONLY) and write a CLAUDE.md file's contents "
        "to help an AI coding agent work here: the build/test/run commands, the "
        "architecture in brief, important conventions, and any gotchas. Be concrete "
        "and concise. Output ONLY the file contents in Markdown — no preamble."
    )
    res = _read_only_claude(prompt, max_turns=30, budget=2.5, on_activity=on_activity)
    text = _strip_fence(res.get("text") or "")
    if not text:
        return False
    target.write_text(text + "\n")
    return True


_JSON_BLOCK = re.compile(r"\[.*\]", re.DOTALL)


def seed_tickets(limit: int = 8, on_activity=None) -> List[dict]:
    """Scan the repo and draft up to `limit` starter tickets into Discussion.
    Returns the created tickets."""
    prompt = (
        "You are seeding a ticket tracker for this repository. Explore the repo "
        "(READ ONLY) and identify up to "
        f"{limit} concrete, valuable, well-scoped pieces of work: TODO/FIXME "
        "comments worth doing, missing tests for important code, obvious bugs, "
        "small UX/DX gaps, or documentation holes. Avoid vague 'improve X' items.\n\n"
        "Output ONLY a JSON array (no prose, no code fences) of objects with keys:\n"
        '  "title" (short, specific), "type" ("bug" or "feature"),\n'
        '  "description" (1-3 sentences incl. file/area), '
        '"acceptance_criteria" (observable outcome),\n'
        '  "priority" (one of "P0","P1","P2","P3").\n'
        f"Return at most {limit} items, best first."
    )
    res = _read_only_claude(prompt, max_turns=30, budget=3.0, on_activity=on_activity)
    text = res.get("text") or ""
    m = _JSON_BLOCK.search(text)
    if not m:
        return []
    try:
        items = json.loads(m.group(0))
    except ValueError:
        return []
    created = []
    for it in items[:limit]:
        if not isinstance(it, dict) or not (it.get("title") or "").strip():
            continue
        ttype = it.get("type") if it.get("type") in ("bug", "feature") else "feature"
        try:
            t = dk.create_ticket(
                title=str(it.get("title", ""))[:300],
                type=ttype,
                description=str(it.get("description", "")),
                acceptance_criteria=str(it.get("acceptance_criteria", "")),
                priority=str(it.get("priority", "P2")),
                created_by="docket",
            )
            created.append(t)
        except ValueError:
            continue
    return created
