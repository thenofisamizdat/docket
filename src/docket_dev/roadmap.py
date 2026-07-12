"""Roadmap — a waterfall cycle overlaid on the Docket lifecycle.

The board answers one planning question the swimlanes can't: *which week is
this ticket committed to, and are we on track for the cycle?* It is an overlay,
not a second state machine — tickets keep their normal lifecycle statuses; the
roadmap only records the week commitment (`tickets.week_lane`) and the hours
maths (`estimate_hours` / `remaining_hours` / `bump_count`).

Shape of a cycle (default 5 weeks):

    Backlog  →  Week 1 … Week N  →  Done

Rules, mirroring how the founders plan:
  - A ticket needs an `estimate_hours` before it may enter a week lane.
  - Anything not Done when its week expires is BUMPED into the next week
    (`rollover()`), incrementing `bump_count` — churn is visible, not hidden.
  - "Hours to completion" = Σ remaining over all week lanes. Done tickets
    contribute 0; Won't-Do tickets drop out of both scope and remaining.
  - Every mutation (and every board read) upserts today's row in
    `roadmap_snapshots`, so the burndown needs no cron job. Scope *rises* when
    tickets join lanes mid-cycle — scope injection shows on the chart.

Schema lives in storage.py with the rest of the tables.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from docket_dev import storage
from docket_dev._timeutil import utcnow_iso
from docket_dev.storage import _connect, _row_to_ticket

# Statuses that count as finished/void for roadmap maths.
_DONE = "done"
_VOID = "cancelled"

DEFAULT_WEEKS = 5


# ---------------------------------------------------------------------------
# Cycles
# ---------------------------------------------------------------------------

def get_cycle() -> Optional[Dict[str, Any]]:
    """The active cycle — by convention the most recently created one."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM roadmap_cycles ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def create_cycle(name: str = "", start_date: str = "",
                 weeks: int = DEFAULT_WEEKS) -> Dict[str, Any]:
    """Start a new cycle. `start_date` is an ISO date (defaults to today);
    starting a new cycle clears every ticket's lane back to Backlog so the
    board never shows stale week commitments from the previous cycle."""
    try:
        start = date.fromisoformat(start_date) if start_date else date.today()
    except ValueError:
        raise ValueError(f"start_date must be an ISO date, got '{start_date}'")
    weeks = int(weeks)
    if not 1 <= weeks <= 12:
        raise ValueError("weeks must be between 1 and 12")
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO roadmap_cycles (name, start_date, weeks, created_at) "
            "VALUES (?,?,?,?)",
            (name.strip()[:120], start.isoformat(), weeks, utcnow_iso()),
        )
        # Fresh cycle, fresh commitments. Estimates survive; lanes reset.
        conn.execute("UPDATE tickets SET week_lane=NULL, bump_count=0 "
                     "WHERE week_lane IS NOT NULL")
        conn.commit()
    finally:
        conn.close()
    cycle = get_cycle()
    _snapshot()
    return cycle


def week_dates(cycle: Dict[str, Any], week: int) -> Dict[str, str]:
    """ISO start/end dates of week N (1-based) of a cycle."""
    start = date.fromisoformat(cycle["start_date"]) + timedelta(weeks=week - 1)
    return {"start": start.isoformat(), "end": (start + timedelta(days=6)).isoformat()}


def current_week(cycle: Dict[str, Any], today: Optional[date] = None) -> int:
    """Which week of the cycle today falls in: 0 = not started, 1..weeks =
    in-cycle, weeks+1 = the cycle has ended."""
    today = today or date.today()
    start = date.fromisoformat(cycle["start_date"])
    if today < start:
        return 0
    wk = (today - start).days // 7 + 1
    return min(wk, int(cycle["weeks"]) + 1)


# ---------------------------------------------------------------------------
# Hours maths + snapshots
# ---------------------------------------------------------------------------

def _effective_remaining(t: Dict[str, Any]) -> float:
    """Remaining hours the burndown should count for one ticket."""
    if t["status"] in (_DONE, _VOID):
        return 0.0
    rem = t.get("remaining_hours")
    if rem is None:
        rem = t.get("estimate_hours") or 0.0
    return max(0.0, float(rem))


def _lane_tickets(conn) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM tickets WHERE week_lane IS NOT NULL"
    ).fetchall()
    return [_row_to_ticket(r) for r in rows]


def _totals(conn) -> Dict[str, float]:
    scope = remaining = 0.0
    for t in _lane_tickets(conn):
        if t["status"] == _VOID:
            continue  # Won't-Do drops out of the cycle entirely
        scope += float(t.get("estimate_hours") or 0.0)
        remaining += _effective_remaining(t)
    return {"scope": round(scope, 2), "remaining": round(remaining, 2)}


def _snapshot(bumps_increment: int = 0) -> None:
    """Upsert today's burndown row for the active cycle (no-op without one)."""
    cycle = get_cycle()
    if not cycle:
        return
    conn = _connect()
    try:
        tot = _totals(conn)
        conn.execute(
            """INSERT INTO roadmap_snapshots (cycle_id, date, total_scope,
                                              total_remaining, bumps)
               VALUES (?,?,?,?,?)
               ON CONFLICT(cycle_id, date) DO UPDATE SET
                   total_scope=excluded.total_scope,
                   total_remaining=excluded.total_remaining,
                   bumps=roadmap_snapshots.bumps+?""",
            (cycle["id"], date.today().isoformat(), tot["scope"],
             tot["remaining"], bumps_increment, bumps_increment),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Ticket moves
# ---------------------------------------------------------------------------

def set_ticket(ticket_id: int, *, week_lane: Any = "unset",
               estimate_hours: Any = "unset", remaining_hours: Any = "unset",
               actor: str = "") -> Dict[str, Any]:
    """Patch a ticket's roadmap fields. `week_lane=None` returns it to Backlog;
    moving it FORWARD a week counts as a bump (per week skipped) and is stamped
    on the timeline, so churn is auditable. Entering a lane requires an
    estimate (either already set or provided in the same call).
    """
    t = storage.get_ticket(ticket_id)
    if not t:
        raise ValueError(f"ticket {ticket_id} not found")
    cycle = get_cycle()

    sets: List[str] = []
    vals: List[Any] = []
    notes: List[str] = []
    bumps = 0

    est = t.get("estimate_hours")
    if estimate_hours != "unset":
        est = None if estimate_hours is None else max(0.0, float(estimate_hours))
        sets.append("estimate_hours=?"); vals.append(est)
        notes.append(f"estimate set to {est}h")
        # A fresh estimate refreshes remaining unless the caller pins it.
        if remaining_hours == "unset" and t.get("week_lane") is None:
            sets.append("remaining_hours=?"); vals.append(est)

    if remaining_hours != "unset":
        rem = None if remaining_hours is None else max(0.0, float(remaining_hours))
        sets.append("remaining_hours=?"); vals.append(rem)
        notes.append(f"remaining set to {rem}h")

    if week_lane != "unset":
        if week_lane is not None:
            if not cycle:
                raise ValueError("no active cycle — create one first")
            week_lane = int(week_lane)
            if not 1 <= week_lane <= int(cycle["weeks"]):
                raise ValueError(f"week_lane must be 1..{cycle['weeks']}")
            if est is None:
                raise ValueError("an estimate (hours) is required to enter a week")
            if t["status"] in (_DONE, _VOID) and t.get("week_lane") is None:
                raise ValueError("finished tickets can't be committed to a week")
        old = t.get("week_lane")
        if old != week_lane:
            sets.append("week_lane=?"); vals.append(week_lane)
            # Entering a lane for the first time seeds remaining from estimate.
            if old is None and t.get("remaining_hours") is None and remaining_hours == "unset":
                sets.append("remaining_hours=?"); vals.append(est)
            if old is not None and week_lane is not None and week_lane > old:
                bumps = week_lane - old
                sets.append("bump_count=?"); vals.append(int(t.get("bump_count") or 0) + bumps)
                notes.append(f"bumped W{old} → W{week_lane}")
            elif week_lane is None:
                notes.append("returned to Backlog")
            else:
                notes.append(f"scheduled into W{week_lane}"
                             if old is None else f"moved W{old} → W{week_lane}")

    if sets:
        sets.append("updated_at=?"); vals.append(utcnow_iso())
        vals.append(ticket_id)
        conn = _connect()
        try:
            conn.execute(f"UPDATE tickets SET {', '.join(sets)} WHERE id=?", vals)
            conn.commit()
        finally:
            conn.close()
        storage.add_event(ticket_id, "note", actor=actor or "system",
                          summary="Roadmap: " + "; ".join(notes),
                          payload={"roadmap": True, "bumps": bumps})
        _snapshot(bumps_increment=bumps)
    return storage.get_ticket(ticket_id)


def mark_done_hours(ticket_id: int) -> None:
    """Zero a ticket's remaining hours (called when it reaches Done so the
    counter and burndown react immediately). Safe no-op off-roadmap."""
    conn = _connect()
    try:
        conn.execute("UPDATE tickets SET remaining_hours=0 "
                     "WHERE id=? AND week_lane IS NOT NULL", (ticket_id,))
        conn.commit()
    finally:
        conn.close()
    _snapshot()


def rollover(actor: str = "") -> List[Dict[str, Any]]:
    """Bump every unfinished ticket sitting in an expired week into the current
    week. Idempotent — run it at the Friday review or any time after. Tickets
    stranded past the final week stay in the last lane and are reported
    `overdue` so the board can flag them."""
    cycle = get_cycle()
    if not cycle:
        return []
    wk = current_week(cycle)
    if wk <= 1:
        return []
    last = int(cycle["weeks"])
    target = min(wk, last)
    moved: List[Dict[str, Any]] = []
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM tickets WHERE week_lane IS NOT NULL AND week_lane < ? "
            "AND status NOT IN (?,?)", (target, _DONE, _VOID)).fetchall()
        tickets = [_row_to_ticket(r) for r in rows]
    finally:
        conn.close()
    for t in tickets:
        upd = set_ticket(t["id"], week_lane=target, actor=actor or "rollover")
        moved.append({"id": t["id"], "ref": t["ref"], "from": t["week_lane"],
                      "to": target, "bump_count": upd["bump_count"]})
    return moved


# ---------------------------------------------------------------------------
# Board payload
# ---------------------------------------------------------------------------

def board() -> Dict[str, Any]:
    """Everything the roadmap page needs in one call: cycle + lanes + hours
    counter + burndown series. Reading also upserts today's snapshot, so the
    chart is current the moment anyone looks at it."""
    cycle = get_cycle()
    _snapshot()
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM tickets ORDER BY priority ASC, updated_at DESC").fetchall()
        tickets = [_row_to_ticket(r) for r in rows]
        snaps: List[Dict[str, Any]] = []
        if cycle:
            snaps = [dict(r) for r in conn.execute(
                "SELECT * FROM roadmap_snapshots WHERE cycle_id=? ORDER BY date ASC",
                (cycle["id"],)).fetchall()]
        tot = _totals(conn)
    finally:
        conn.close()

    wk = current_week(cycle) if cycle else 0
    lanes: Dict[str, List[Dict[str, Any]]] = {"backlog": [], "done": []}
    weeks_meta = []
    if cycle:
        for n in range(1, int(cycle["weeks"]) + 1):
            lanes[f"w{n}"] = []
            weeks_meta.append({"week": n, **week_dates(cycle, n),
                               "is_current": n == wk})
    for t in tickets:
        t["effective_remaining"] = _effective_remaining(t)
        if t["status"] == _VOID and t.get("week_lane") is None:
            continue  # cancelled backlog noise stays off the roadmap
        if t["status"] == _DONE:
            lanes["done"].append(t)
        elif t.get("week_lane"):
            lanes.setdefault(f"w{t['week_lane']}", []).append(t)
        else:
            lanes["backlog"].append(t)

    return {
        "cycle": cycle,
        "current_week": wk,
        "weeks": weeks_meta,
        "lanes": lanes,
        "hours_to_completion": tot["remaining"],
        "total_scope": tot["scope"],
        "snapshots": snaps,
        "status_meta": storage.STATUS_META,
    }
