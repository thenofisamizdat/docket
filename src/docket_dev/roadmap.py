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

# Manual work-state for a roadmap card (independent of the pipeline lifecycle
# `status`). Purely a human/visual state — setting it NEVER queues or automates.
ROADMAP_STATUSES = ("backlog", "todo", "in_progress", "done")


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

def _is_done(t: Dict[str, Any]) -> bool:
    """A card is Done if the pipeline shipped it OR it was marked done manually
    on the roadmap (roadmap_status)."""
    return t["status"] == _DONE or t.get("roadmap_status") == _DONE


def _effective_remaining(t: Dict[str, Any]) -> float:
    """Remaining hours the burndown should count for one ticket."""
    if t["status"] == _VOID or _is_done(t):
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
               roadmap_status: Any = "unset", hours_done: Any = "unset",
               actor: str = "") -> Dict[str, Any]:
    """Patch a ticket's roadmap fields. `week_lane=None` returns it to Backlog;
    moving it FORWARD a week counts as a bump (per week skipped) and is stamped
    on the timeline, so churn is auditable. Entering a lane requires an
    estimate (either already set or provided in the same call).

    `roadmap_status` (backlog|todo|in_progress|done) is the manual work-state —
    a plain field write that NEVER queues or automates. Setting it to `backlog`
    also returns the card to the Backlog lane. `hours_done` logs actual effort.
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

    if roadmap_status != "unset":
        rs = (roadmap_status or "").strip().lower()
        if rs not in ROADMAP_STATUSES:
            raise ValueError(f"roadmap_status must be one of {ROADMAP_STATUSES}")
        sets.append("roadmap_status=?"); vals.append(rs)
        notes.append(f"status → {rs}")
        # Backlog status returns the card to the Backlog lane (unless the caller
        # already set a lane explicitly in this same call).
        if rs == "backlog" and week_lane == "unset" and t.get("week_lane") is not None:
            sets.append("week_lane=?"); vals.append(None)
            notes.append("returned to Backlog")

    if hours_done != "unset":
        hd = None if hours_done is None else max(0.0, float(hours_done))
        sets.append("hours_done=?"); vals.append(hd)
        notes.append(f"hours done set to {hd}h")

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


def send_to_pipeline(ticket_id: int, queue: bool = False,
                     actor: str = "") -> Dict[str, Any]:
    """Hand a roadmap ticket to the automated Docket pipeline. This is the EXPLICIT
    opt-in: it sets `dev_optin=1` (the only way, besides board Submit / greenfield
    grooming, a ticket becomes eligible for the agent) and marks the card In
    Progress. If `queue`, it also transitions the ticket into the live queue so the
    agent picks it up; otherwise it stays in Discussion for a manual Submit."""
    t = storage.get_ticket(ticket_id)
    if not t:
        raise ValueError(f"ticket {ticket_id} not found")
    conn = _connect()
    try:
        conn.execute("UPDATE tickets SET dev_optin=1, roadmap_status='in_progress', "
                     "updated_at=? WHERE id=?", (utcnow_iso(), ticket_id))
        conn.commit()
    finally:
        conn.close()
    if queue:
        # Only Discussion tickets can be queued; surface a clear error otherwise.
        storage.transition(ticket_id, "queued", actor=actor or "user",
                           summary="Queued to Docket pipeline from the roadmap")
        storage.add_event(ticket_id, "note", actor=actor or "user",
                          summary="→ Queued to the Docket pipeline for automated development.")
    else:
        storage.add_event(ticket_id, "note", actor=actor or "user",
                          summary="→ Sent to the Docket pipeline — available to Submit for "
                                  "automated development.")
    return storage.get_ticket(ticket_id)


def record_pipeline_done(ticket_id: int, actor: str = "agent") -> Optional[Dict[str, Any]]:
    """Write pipeline results back onto the ticket when the agent finishes it:
    auto-fill `hours_done` from the summed agent wall-clock, mark the roadmap card
    Done, and add a human-readable "Done by Docket pipeline" note (cost/turns/hours).
    Idempotent (skips if that note already exists) and only auto-fills hours when
    the ticket actually has agent run-events — a hand-completed ticket gets nothing
    fabricated."""
    try:
        events = storage.get_events(ticket_id)
    except Exception:
        events = []
    if any(isinstance(e.get("summary"), str)
           and "Done by Docket pipeline" in e["summary"] for e in events):
        return storage.get_ticket(ticket_id)      # already recorded

    secs = cost = 0.0
    turns = 0
    for e in events:
        p = e.get("payload")
        if isinstance(p, dict):
            secs += float(p.get("duration_secs") or 0)
            cost += float(p.get("cost_usd") or 0)
            turns += int(p.get("turns") or 0)
    if secs <= 0:
        # No agent run-events — not actually built by the pipeline. Don't fabricate.
        return None

    hours = round(secs / 3600.0, 2)
    conn = _connect()
    try:
        conn.execute("UPDATE tickets SET hours_done=?, roadmap_status='done', "
                     "remaining_hours=0, updated_at=? WHERE id=?",
                     (hours, utcnow_iso(), ticket_id))
        conn.commit()
    finally:
        conn.close()
    storage.add_event(
        ticket_id, "note", actor=actor,
        summary=(f"✅ Done by Docket pipeline — logged {hours}h "
                 f"(${cost:.2f}, {turns} turns wall-clock). "
                 f"The implementation + self-review notes above are the work record."),
        payload={"pipeline_done": True, "hours": hours, "cost_usd": round(cost, 4),
                 "turns": turns})
    _snapshot()
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
        if _is_done(t):
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


def analytics(epic_id: Optional[int] = None) -> Dict[str, Any]:
    """Schedule/effort analytics for the roadmap dashboard: burndown series (with an
    ideal + a velocity projection), scope-creep, spent-vs-left, per-week loading, and
    a composite schedule-health score. Reuses board() + the snapshot series.

    With `epic_id`, every number is computed over that epic's tickets only. The
    daily snapshots are cycle-global (no epic dimension), so the historical
    burndown series is dropped — the chart shows ideal + live projection from
    current state instead, and scope-creep reports zero injected."""
    b = board()
    if epic_id:
        b["lanes"] = {k: [t for t in v if t.get("epic_id") == epic_id]
                      for k, v in b["lanes"].items()}
        live = [t for lane in b["lanes"].values() for t in lane
                if t.get("week_lane") and t["status"] != _VOID]
        b["total_scope"] = sum(float(t.get("estimate_hours") or 0) for t in live)
        b["hours_to_completion"] = sum(float(t.get("effective_remaining") or 0)
                                       for t in live if not _is_done(t))
        b["snapshots"] = []
    cycle = b["cycle"]
    weeks = b["weeks"]
    snaps = b["snapshots"]
    all_t = [t for lane in b["lanes"].values() for t in lane]
    lane_t = [t for t in all_t if t.get("week_lane")]        # committed to a week

    scope = float(b["total_scope"] or 0)
    remaining = float(b["hours_to_completion"] or 0)
    spent = sum(float(t.get("hours_done") or 0) for t in all_t)
    done_ct = sum(1 for t in all_t if _is_done(t))
    open_ct = sum(1 for t in lane_t if not _is_done(t))
    pct_done = round((scope - remaining) / scope * 100, 1) if scope else 0.0

    # Per-week loading (committed estimate vs remaining). Overloaded = well above the
    # even-spread average across the weeks in the cycle.
    n_weeks = int(cycle["weeks"]) if cycle else 0
    avg = (scope / n_weeks) if n_weeks else 0.0
    week_load = []
    for w in weeks:
        cards = b["lanes"].get(f"w{w['week']}", [])
        est = sum(float(c.get("estimate_hours") or 0) for c in cards)
        rem = sum(float(c.get("effective_remaining") or 0) for c in cards)
        week_load.append({"week": w["week"], "start": w["start"], "end": w["end"],
                          "is_current": w["is_current"], "estimate": round(est, 1),
                          "remaining": round(rem, 1),
                          "overloaded": bool(avg and est > avg * 1.5)})

    # Scope creep = scope injected since the cycle opened (first snapshot).
    scope_start = float(snaps[0]["total_scope"]) if snaps else scope
    bumps_total = sum(int(s.get("bumps") or 0) for s in snaps)
    creep = {"scope_start": round(scope_start, 1), "scope_now": round(scope, 1),
             "injected": round(scope - scope_start, 1), "bumps_total": bumps_total}

    # Velocity (hours burned per week so far) + a straight-line projection to zero.
    wk = int(b["current_week"] or 0)
    elapsed = max(wk, 0)
    burned = max(scope_start - remaining, 0.0)
    velocity = (burned / elapsed) if elapsed > 0 else 0.0        # hours/week
    weeks_left_at_velocity = (remaining / velocity) if velocity > 0 else None
    projected_week = (wk + weeks_left_at_velocity) if weeks_left_at_velocity is not None else None
    on_track = (projected_week is not None and n_weeks and projected_week <= n_weeks)
    forecast = {"velocity_per_week": round(velocity, 1),
                "projected_finish_week": round(projected_week, 1) if projected_week else None,
                "deadline_week": n_weeks, "on_track": bool(on_track),
                "remaining": round(remaining, 1)}

    # Schedule-health score (0-100): behind-vs-ideal + creep + churn penalties.
    ideal_remaining = scope_start * max(0.0, 1 - (wk / n_weeks)) if n_weeks else 0.0
    behind = max(0.0, remaining - ideal_remaining)
    score = 100.0
    if scope_start:
        score -= min(45.0, behind / scope_start * 100)           # schedule slippage
        score -= min(25.0, max(0.0, scope - scope_start) / scope_start * 60)  # creep
    score -= min(15.0, bumps_total * 3)                          # churn
    score = max(0.0, round(score))
    label = "on_track" if score >= 75 else "at_risk" if score >= 50 else "behind"
    health = {"score": score, "label": label,
              "signals": {"pct_done": pct_done, "behind_hours": round(behind, 1),
                          "injected_hours": creep["injected"], "bumps": bumps_total,
                          "on_track": bool(on_track)}}

    return {
        "cycle": cycle, "weeks": weeks, "current_week": wk, "snapshots": snaps,
        "totals": {"scope": round(scope, 1), "remaining": round(remaining, 1),
                   "spent": round(spent, 1), "done_count": done_ct,
                   "open_count": open_ct, "pct_done": pct_done},
        "week_load": week_load, "creep": creep, "forecast": forecast, "health": health,
        "ideal_remaining_now": round(ideal_remaining, 1),
    }
