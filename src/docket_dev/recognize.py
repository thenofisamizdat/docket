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
    """Drop a wrapping ```markdown / ``` code fence if the model added one —
    including the "Here is the file: ```…```" shape, where a short line of
    chatter precedes the fence. Only a fence that closes at the very end with
    ≤300 chars of preamble is treated as a wrapper, so documents that merely
    CONTAIN fenced code blocks pass through untouched."""
    t = (text or "").strip()
    if t.startswith("```"):
        lines = t.splitlines()
        lines = lines[1:]                       # drop opening ```lang
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]                  # drop closing ```
        return "\n".join(lines).strip()
    m = re.match(r"(?s)^.{0,300}?```[a-zA-Z]*\n(.*)\n```\s*$", t)
    if m:
        return m.group(1).strip()
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


def estimate_tickets(ids: Optional[List[int]] = None, *, on_activity=None) -> List[dict]:
    """Auto-estimate effort hours for tickets. `ids` limits the set; default is
    every unestimated open ticket (`estimate_hours IS NULL`). Reuses the read-only
    Claude + robust-JSON path, grounds on the repo profile, and writes each estimate
    via roadmap.set_ticket(). Returns [{id, ref, hours, rationale}]."""
    from docket_dev import roadmap as rm
    if ids:
        targets = [dk.get_ticket(i) for i in ids]
        targets = [t for t in targets if t]
    else:
        targets = dk.unestimated_tickets()
    if not targets:
        return []
    targets = targets[:40]                                  # cap a batch

    try:
        profile = CONFIG.profile_path.read_text()[:3000]
    except (OSError, FileNotFoundError):
        profile = ""

    lines = []
    for t in targets:
        lines.append(
            f'- id {t["id"]} [{t["type"]}, {t["priority"]}] {t["title"]}\n'
            f'    desc: {(t.get("description") or "")[:400]}\n'
            f'    acceptance: {(t.get("acceptance_criteria") or "")[:300]}')
    prompt = (
        "You are estimating engineering effort for these tickets, to be built by a "
        "capable coding agent on the repo below. For EACH ticket give a realistic "
        "effort estimate in HOURS (0.5–40; small/typical tasks are 1–6h). Consider "
        "the acceptance criteria and the codebase.\n\n"
        + (f"CODEBASE PROFILE:\n{profile}\n\n" if profile else "")
        + "TICKETS:\n" + "\n".join(lines) + "\n\n"
        "Return ONLY a JSON array (no prose, no code fences) of objects with keys: "
        '"id" (the ticket id), "hours" (number), "rationale" (one short sentence).'
    )
    res = _read_only_claude(prompt, max_turns=25, budget=3.0, on_activity=on_activity)
    items = _parse_ticket_array(res.get("text") or "")
    by_id = {t["id"]: t for t in targets}
    out = []
    for it in items:
        try:
            tid = int(it.get("id"))
            hours = round(max(0.0, float(it.get("hours"))), 1)
        except (TypeError, ValueError):
            continue
        if tid not in by_id or hours <= 0:
            continue
        try:
            rm.set_ticket(tid, estimate_hours=hours, actor="docket")
            rationale = str(it.get("rationale", ""))[:300]
            if rationale:
                dk.add_event(tid, "note", actor="docket",
                             summary=f"Auto-estimated {hours}h — {rationale}")
            out.append({"id": tid, "ref": by_id[tid]["ref"], "hours": hours,
                        "rationale": it.get("rationale", "")})
        except (ValueError, Exception):
            continue
    return out


def _parse_ticket_array(text: str) -> List[dict]:
    """Best-effort extraction of a JSON array of ticket dicts from model output.
    The groomed array is large, so tolerate a wrapping code fence, prose around the
    array, and trailing commas rather than aborting the whole batch (the bare
    `json.loads` in seed_tickets is too brittle for this)."""
    t = _strip_fence(text or "")
    start, end = t.find("["), t.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    blob = t[start:end + 1]
    candidates = [blob, re.sub(r",(\s*[\]}])", r"\1", blob)]  # 2nd: strip trailing commas
    for cand in candidates:
        try:
            v = json.loads(cand)
        except ValueError:
            continue
        if isinstance(v, list):
            return [it for it in v if isinstance(it, dict)]
    return []


def groom_brief(brief_text: str, *, cap: int = 40, on_activity=None) -> List[dict]:
    """Groom a completed PROJECT_BRIEF into a COMPLETE, ORDERED backlog that builds
    the ENTIRE project — scaffolding/setup tickets first, then features in
    dependency order. Each ticket gets a 1-based `build_seq`; all land in Discussion
    for review, then "Run Full Build" submits them in that order. Generalizes
    seed_tickets (repo-driven) to work purely from the brief (there's no code yet)."""
    prompt = (
        "You are the planning lead for a BRAND-NEW software project. Below is the "
        "completed project brief. Produce the COMPLETE backlog of tickets that, built "
        "in order by a coding agent, delivers the ENTIRE project described.\n\n"
        "RULES:\n"
        "- SCAFFOLDING/SETUP tickets FIRST: repo layout, tooling/dependencies, the "
        "base app skeleton, config, and (if relevant) CI — everything that must exist "
        "before features can be built.\n"
        "- THEN features in DEPENDENCY ORDER: a ticket must never depend on work that "
        "comes later in the list.\n"
        "- Each ticket is small, concrete, and independently buildable on a fresh "
        "checkout by an agent that reads the repo + the ticket.\n"
        "- Cover every Must-have fully; include Should-haves; skip Could-haves unless "
        "trivial.\n\n"
        f"Return ONLY a JSON array (no prose, no code fences) of up to {cap} objects, "
        "IN BUILD ORDER, each with keys:\n"
        '  "sequence" (1-based build order, ascending),\n'
        '  "title" (short, specific), "type" ("bug" or "feature"),\n'
        '  "description" (what to build + why, grounded in the brief),\n'
        '  "acceptance_criteria" (observable, testable outcome),\n'
        '  "priority" ("P0".."P3"; put scaffolding at P1 so it runs before P2 features).\n\n'
        "=== PROJECT BRIEF ===\n" + (brief_text or "").strip()[:16000]
    )
    res = _read_only_claude(prompt, max_turns=30, budget=4.0, on_activity=on_activity)
    items = _parse_ticket_array(res.get("text") or "")

    def _seq(it: dict, i: int) -> int:
        try:
            return int(it.get("sequence"))
        except (TypeError, ValueError):
            return 10_000 + i

    valid = [it for it in items if (it.get("title") or "").strip()]
    ordered = sorted(enumerate(valid), key=lambda p: _seq(p[1], p[0]))
    created = []
    for build_seq, (_, it) in enumerate(ordered[:cap], start=1):
        ttype = it.get("type") if it.get("type") in ("bug", "feature") else "feature"
        try:
            t = dk.create_ticket(
                title=str(it.get("title", ""))[:300],
                type=ttype,
                description=str(it.get("description", "")),
                acceptance_criteria=str(it.get("acceptance_criteria", "")),
                priority=str(it.get("priority", "P2")),
                created_by="docket",
                build_seq=build_seq,
                dev_optin=True,   # greenfield build tickets are meant for the pipeline
            )
            created.append(t)
        except ValueError:
            continue
    return created
