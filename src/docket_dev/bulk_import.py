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
    r"^(#{2,4})\s*(epic|story|task|bug|feature)\s*[:\-–—]\s*(.+?)\s*$",
    re.IGNORECASE)
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
            cur = {"title": title, "type": kind, "epic": cur_epic,
                   "parent_idx": parent_idx, "priority": "", "estimate_hours": None,
                   "_desc": [], "_ac": []}
            tickets.append(cur)
            if level <= 3:
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
    return {"epics": epics, "tickets": tickets, "warnings": warnings, "counts": counts}


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
