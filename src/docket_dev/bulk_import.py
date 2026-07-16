"""Markdown bulk import — flesh out a whole project from one document.

The input is a plain markdown roadmap, typically authored by an LLM working
from a gap-analysis/spec (see docs/GAP_ANALYSIS_TICKET_PLAYBOOK.md for the
authoring guide). The heading hierarchy IS the work hierarchy:

    ## Epic: <name>            → an epic (created if it doesn't exist yet)
    Color: #10b981             (optional; palette auto-assign otherwise)
    <description paragraphs>

    ### Story: <title>         → a ticket under the current epic
    Priority: P1               (optional; default P2)
    Estimate: 16h              (optional; hours)
    <description paragraphs>
    Acceptance criteria:
    - observable outcome …

    #### Task: <title>         → a child of the story above (parent_id)
    #### Bug: <title>          → likewise

`### Task:` / `### Bug:` / `### Feature:` are also legal directly under an
epic (top-level work that needs no story). Metadata keys are case-insensitive
and may be bolded (`**Priority:** P1`).

Import is two-phase: parse() builds a plan + warnings without touching the DB
(the dry-run preview), apply() creates epics + tickets in document order and
stamps sequential build_seq so "Run Full Build" works the document top-down.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from docket_dev import storage

_HEADING = re.compile(
    r"^(#{2,4})\s*(epic|story|task|bug|feature|decision)\s*[:\-–—]\s*(.+?)\s*$",
    re.IGNORECASE)
# Titles that READ as a decision even without the explicit `### Decision:` type.
# A leading decision verb means the ticket's output is an answer, not a diff —
# the agent can't build it, so import it human-owned rather than letting it
# churn through (and bounce out of) the automated pipeline.
_DECISION_VERBS = re.compile(
    r"^(define|confirm|choose|decide|select|approve|user-test)\b", re.IGNORECASE)
_META = re.compile(
    r"^\**\s*(priority|estimate|color)\s*:?\**\s*:?\s*(.+?)\s*$",
    re.IGNORECASE)
_AC = re.compile(r"^\**\s*acceptance criteria\s*:?\**\s*:?\s*$", re.IGNORECASE)
_EST = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hours?)?\s*$", re.IGNORECASE)
_COLOR = re.compile(r"^#[0-9a-fA-F]{3,8}$")


def _parse_estimate(raw: str) -> Optional[float]:
    m = _EST.match(raw or "")
    return float(m.group(1)) if m else None


def parse(markdown: str) -> Dict[str, Any]:
    """Parse a roadmap document into an import plan. Never touches the DB.

    Returns {"epics": [...], "tickets": [...], "warnings": [...], "counts": {...}}
    where each ticket carries `epic` (name or None) and `parent_idx` (index into
    the tickets list of its story, or None).
    """
    epics: List[Dict[str, Any]] = []           # {name, color, description}
    tickets: List[Dict[str, Any]] = []
    warnings: List[str] = []

    epic_by_name: Dict[str, int] = {}          # lower name -> index into epics
    cur: Optional[Dict[str, Any]] = None       # section whose body we're filling
    cur_epic: Optional[str] = None             # current epic name
    cur_story_idx: Optional[int] = None        # tickets[] index of the last ### ticket
    in_ac = False

    def close_section():
        nonlocal in_ac
        if cur is not None:
            cur["description"] = "\n".join(cur.pop("_desc")).strip()
            cur["acceptance_criteria"] = "\n".join(cur.pop("_ac")).strip()
        in_ac = False

    lines = (markdown or "").replace("\r\n", "\n").split("\n")
    for ln_no, line in enumerate(lines, 1):
        m = _HEADING.match(line)
        if m:
            close_section()
            level, kind, title = len(m.group(1)), m.group(2).lower(), m.group(3).strip()
            if kind == "epic":
                if level != 2:
                    warnings.append(f"line {ln_no}: 'Epic:' heading should be '##' — treated as an epic anyway")
                key = title.lower()
                if key in epic_by_name:
                    warnings.append(f"line {ln_no}: epic '{title}' appears twice — sections merged")
                    cur = epics[epic_by_name[key]]
                else:
                    cur = {"name": title, "color": "", "_desc": [], "_ac": []}
                    epic_by_name[key] = len(epics)
                    epics.append(cur)
                cur_epic = title
                cur_story_idx = None
                continue
            if level == 2:
                warnings.append(f"line {ln_no}: '{kind}' at '##' level — expected '###'; imported as top-level")
            parent_idx = None
            if level >= 4:
                if cur_story_idx is None:
                    warnings.append(f"line {ln_no}: '#### {kind}' has no story above it — imported at epic level")
                else:
                    parent_idx = cur_story_idx
            if cur_epic is None:
                warnings.append(f"line {ln_no}: '{kind}: {title}' appears before any '## Epic:' — imported without an epic")
            # `### Decision:` imports as a human-owned task: its output is an
            # answer from a person, not a diff — the agent never picks it up,
            # and its story's implementation children wait for it.
            human_only = kind == "decision"
            if human_only:
                kind = "task"
            cur = {"title": title, "type": kind, "epic": cur_epic,
                   "parent_idx": parent_idx, "priority": "", "estimate_hours": None,
                   "human_only": human_only, "_desc": [], "_ac": []}
            tickets.append(cur)
            if level <= 3 and not human_only:
                cur_story_idx = len(tickets) - 1
            continue

        if cur is None:
            continue                            # preamble before the first heading

        if _AC.match(line):
            in_ac = True
            continue
        mm = _META.match(line)
        if mm and not in_ac:
            key, val = mm.group(1).lower(), mm.group(2).strip().strip("*").strip()
            if key == "color":
                if "name" in cur:               # only meaningful on an epic
                    if _COLOR.match(val):
                        cur["color"] = val
                    else:
                        warnings.append(f"line {ln_no}: bad color '{val}' — ignored (want #rrggbb)")
                continue
            if key == "priority":
                if "title" in cur:
                    p = val.upper()
                    if p in storage.PRIORITIES:
                        cur["priority"] = p
                    else:
                        warnings.append(f"line {ln_no}: unknown priority '{val}' — defaulting to {storage.DEFAULT_PRIORITY}")
                continue
            if key == "estimate":
                if "title" in cur:
                    est = _parse_estimate(val)
                    if est is None:
                        warnings.append(f"line {ln_no}: could not parse estimate '{val}' — expected hours like '12h'")
                    else:
                        cur["estimate_hours"] = est
                continue
        (cur["_ac"] if in_ac else cur["_desc"]).append(line)

    close_section()

    # Decision-shaped titles that weren't explicitly typed: a leading decision
    # verb means a person must answer this — flag it human-owned and say so in
    # the dry-run preview so the author can rephrase if it IS buildable.
    for t in tickets:
        if (not t.get("human_only") and t["type"] in ("task", "feature")
                and _DECISION_VERBS.match(t["title"])):
            t["human_only"] = True
            warnings.append(
                f"'{t['title'][:70]}': reads as a DECISION (leading verb) — imported "
                "human-owned; the agent will not build it and its story's children "
                "wait for its answer. Rephrase with an implementation verb (or use "
                "'### Task:') if an agent should build it.")

    for t in tickets:
        if not t["estimate_hours"]:
            # Stories summing their children is fine; anything else unestimated is worth flagging.
            if t["type"] != "story" or not any(x["parent_idx"] is not None and
                                               tickets[x["parent_idx"]] is t for x in tickets):
                warnings.append(f"'{t['title']}': no estimate — the roadmap needs one before it can enter a week")

    counts = {"epics": len(epics), "total": len(tickets),
              "estimated_hours": round(sum(t["estimate_hours"] or 0 for t in tickets), 1)}
    for kind in storage.TICKET_TYPES:
        counts[kind] = sum(1 for t in tickets if t["type"] == kind)
    counts["decisions"] = sum(1 for t in tickets if t.get("human_only"))
    return {"epics": epics, "tickets": tickets, "warnings": warnings, "counts": counts}


# ---------------------------------------------------------------------------
# Playlists — an ordered work-through instruction file
# ---------------------------------------------------------------------------
#
# A playlist tells Docket the ORDER to work tickets in (all of them or a
# subset), optionally queueing them to the pipeline immediately:
#
#     # Playlist: Alpha build order
#     Mode: queue            (or "order" — set the order only, queue nothing)
#
#     ## Phase 1: Foundations
#     1. DKT-12
#     2. Persist tus upload state to disk
#
#     ## Phase 2: Comms
#     1. DKT-40 Threaded view for group chats
#
# Items are matched by DKT ref when present, else by exact (case-insensitive)
# title — so a playlist can be authored against a plan file before the tickets
# even have refs. Phases are grouping/reporting only; the global order is the
# document order. Applying sets build_seq 1..N over the listed tickets (they
# sort ahead of everything unlisted) and, in queue mode, hands each one to the
# pipeline in that order.

_PL_MODE = re.compile(r"^\**\s*mode\s*:?\**\s*:?\s*(queue|order)[\s\-a-z]*$", re.IGNORECASE)
_PL_PHASE = re.compile(r"^#{1,4}\s*(?:phase\s*\d*\s*[:\-–—]?\s*)?(.*)$", re.IGNORECASE)
_PL_ITEM = re.compile(r"^\s*(?:\d+[.)]|[-*])\s+(.+?)\s*$")
_PL_REF = re.compile(r"\bDKT-(\d+)\b", re.IGNORECASE)


def parse_playlist(markdown: str) -> Dict[str, Any]:
    """Parse a playlist document into {mode, items:[{raw, ref_id, title, phase}],
    warnings}. No DB access."""
    mode = "order"
    items: List[Dict[str, Any]] = []
    warnings: List[str] = []
    phase = ""
    for ln_no, line in enumerate((markdown or "").replace("\r\n", "\n").split("\n"), 1):
        if not line.strip():
            continue
        m = _PL_MODE.match(line.strip())
        if m:
            mode = m.group(1).lower()
            continue
        if line.lstrip().startswith("#"):
            mm = _PL_PHASE.match(line.strip())
            title = (mm.group(1) if mm else "").strip()
            if title.lower().startswith("playlist"):
                continue                      # the document title
            phase = title
            continue
        m = _PL_ITEM.match(line)
        if not m:
            continue                          # prose between items is ignored
        raw = m.group(1).strip()
        ref = _PL_REF.search(raw)
        title = _PL_REF.sub("", raw).strip(" -–—·:\"'“”")
        items.append({"raw": raw, "ref_id": int(ref.group(1)) if ref else None,
                      "title": title, "phase": phase, "line": ln_no})
    if not items:
        warnings.append("no playlist items found — list tickets as '1. DKT-n' or '1. <exact title>'")
    return {"mode": mode, "items": items, "warnings": warnings}


def resolve_playlist(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Match every playlist item to a ticket (by ref, else exact title,
    case-insensitive). Adds `ticket` (or None) to each item and reports
    unmatched/duplicates. Read-only."""
    conn = storage._connect()
    try:
        rows = conn.execute("SELECT * FROM tickets").fetchall()
    finally:
        conn.close()
    tickets = [storage._row_to_ticket(r) for r in rows]
    by_id = {t["id"]: t for t in tickets}
    by_title: Dict[str, List[Dict[str, Any]]] = {}
    for t in tickets:
        by_title.setdefault(t["title"].strip().lower(), []).append(t)

    seen: set = set()
    unmatched: List[str] = []
    warnings = list(plan.get("warnings", []))
    for it in plan["items"]:
        t = None
        if it["ref_id"] is not None:
            t = by_id.get(it["ref_id"])
            if not t:
                unmatched.append(f"line {it['line']}: DKT-{it['ref_id']} does not exist")
        elif it["title"]:
            cands = by_title.get(it["title"].lower(), [])
            if len(cands) == 1:
                t = cands[0]
            elif len(cands) > 1:
                unmatched.append(f"line {it['line']}: title '{it['title']}' matches "
                                 f"{len(cands)} tickets — use its DKT ref")
            else:
                unmatched.append(f"line {it['line']}: no ticket titled '{it['title']}'")
        if t and t["id"] in seen:
            warnings.append(f"line {it['line']}: {t['ref']} listed twice — first position wins")
            t = None
        if t:
            seen.add(t["id"])
        it["ticket"] = t
    return {**plan, "unmatched": unmatched, "warnings": warnings}


def apply_playlist(resolved: Dict[str, Any], actor: str = "") -> Dict[str, Any]:
    """Stamp build_seq 1..N over the matched tickets in playlist order (they
    sort ahead of every unlisted ticket) and, in queue mode, hand each one to
    the pipeline in that order (skipping container stories and anything not
    in Discussion — reported, never silent)."""
    from docket_dev import roadmap as rm
    matched = [it for it in resolved["items"] if it.get("ticket")]
    listed_ids = {it["ticket"]["id"] for it in matched}
    conn = storage._connect()
    try:
        parents = {r[0] for r in conn.execute(
            "SELECT DISTINCT parent_id FROM tickets WHERE parent_id IS NOT NULL")}
        for seq, it in enumerate(matched, 1):
            conn.execute("UPDATE tickets SET build_seq=? WHERE id=?",
                         (seq, it["ticket"]["id"]))
        # Unlisted tickets that had a build order keep their relative order but
        # move AFTER the playlist, so the playlist genuinely runs first.
        others = [r["id"] for r in conn.execute(
            "SELECT id FROM tickets WHERE build_seq IS NOT NULL "
            "ORDER BY build_seq, id") if r["id"] not in listed_ids]
        for off, tid in enumerate(others, 1):
            conn.execute("UPDATE tickets SET build_seq=? WHERE id=?",
                         (len(matched) + off, tid))
        conn.commit()
    finally:
        conn.close()

    ordered = [{"seq": i + 1, "ref": it["ticket"]["ref"], "title": it["ticket"]["title"],
                "phase": it["phase"]} for i, it in enumerate(matched)]
    queued: List[str] = []
    skipped: List[Dict[str, Any]] = []
    if resolved["mode"] == "queue":
        for it in matched:
            t = storage.get_ticket(it["ticket"]["id"])   # fresh status
            if t["id"] in parents and t["type"] == "story":
                skipped.append({"ref": t["ref"], "reason": "container story"})
                continue
            if t["status"] != "discussion":
                skipped.append({"ref": t["ref"], "reason": f"not queueable ({t['status_label']})"})
                continue
            try:
                rm.send_to_pipeline(t["id"], queue=True, actor=actor)
                queued.append(t["ref"])
            except ValueError as e:
                skipped.append({"ref": t["ref"], "reason": str(e)})
    return {"mode": resolved["mode"], "ordered": ordered, "queued": queued,
            "skipped": skipped, "unmatched": resolved["unmatched"],
            "warnings": resolved["warnings"], "count": len(ordered)}


def apply(plan: Dict[str, Any], created_by: str = "") -> Dict[str, Any]:
    """Create the plan's epics and tickets. Epics are matched to existing ones
    by name (case-insensitive) and reused rather than duplicated; tickets get
    sequential build_seq continuing after the current maximum, so a later
    "Run Full Build" walks the document top-down."""
    existing = {e["name"].lower(): e for e in storage.list_epics()}
    epic_ids: Dict[str, int] = {}
    epic_report: List[Dict[str, Any]] = []
    for e in plan["epics"]:
        key = e["name"].lower()
        if key in existing:
            epic_ids[key] = existing[key]["id"]
            epic_report.append({"name": e["name"], "status": "existing",
                                "color": existing[key]["color"]})
        else:
            made = storage.create_epic(name=e["name"], color=e.get("color", ""),
                                       description=e.get("description", ""),
                                       created_by=created_by)
            epic_ids[key] = made["id"]
            epic_report.append({"name": e["name"], "status": "created",
                                "color": made["color"]})

    conn = storage._connect()
    try:
        row = conn.execute("SELECT MAX(build_seq) AS m FROM tickets").fetchone()
        seq = int(row["m"] or 0)
    finally:
        conn.close()

    created: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    idx_to_id: Dict[int, int] = {}
    for i, t in enumerate(plan["tickets"]):
        parent_id = None
        if t.get("parent_idx") is not None:
            parent_id = idx_to_id.get(t["parent_idx"])   # None if the parent errored
        seq += 1
        try:
            made = storage.create_ticket(
                title=t["title"], type=t["type"],
                description=t.get("description", ""),
                acceptance_criteria=t.get("acceptance_criteria", ""),
                priority=t.get("priority") or storage.DEFAULT_PRIORITY,
                created_by=created_by, build_seq=seq,
                epic_id=epic_ids.get((t.get("epic") or "").lower()),
                parent_id=parent_id,
                estimate_hours=t.get("estimate_hours"),
                human_only=bool(t.get("human_only")),
            )
            idx_to_id[i] = made["id"]
            created.append({"ref": made["ref"], "id": made["id"], "title": made["title"],
                            "type": made["type"], "epic": made["epic_name"],
                            "parent_ref": made["parent_ref"]})
        except ValueError as exc:
            seq -= 1
            errors.append({"title": t.get("title", ""), "error": str(exc)})

    return {"epics": epic_report, "created": created, "errors": errors,
            "count": len(created), "warnings": plan.get("warnings", []),
            "counts": plan.get("counts", {})}
