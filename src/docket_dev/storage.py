"""
Docket — ticket store + lifecycle state machine (SQLite).

Docket is the evolution of the QA testing hub into a real ticket pipeline: an
ask is raised in the Discussion zone, **submitted for processing**, then crawls
a visible production line (Queued → Assessment → Planning → In Development →
Self-Review → PR → User Review → Done) driven by an autonomous dev agent. The
board's quiet purpose is to make the *cost* of development legible to testers.

Why SQLite (not the flat JSON the old hub used): tickets need an audit trail,
queue ordering, a streaming work-history, and concurrent writes from three
directions at once (the agent, the UI, and testers). A transactional store in
WAL mode handles that cleanly where reload-and-overwrite JSON would lose writes.

Three tables:
  tickets        — one row per work item (the lifecycle state lives here)
  ticket_events  — append-only work history AND audit log (transitions,
                   the live "currently working on" activity, assessment, plan,
                   comments). The activity ticker = the latest 'activity' event.
  notifications  — outbound notification queue (events → recipient → channel),
                   drained by the notifier (msmtp / in-app badge).

The state machine is the contract: `transition()` REFUSES illegal moves, so no
caller (agent or human) can put a ticket into an impossible state. Every
transition writes a ticket_events row, so the timeline is never out of sync
with the status.
"""

from __future__ import annotations

import json
import re
import sqlite3
import statistics
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from docket_dev._timeutil import utcnow_iso
from docket_dev.config import CONFIG

# ---------------------------------------------------------------------------
# Vocabulary: ticket types, priorities, statuses, and the legal transitions.
# ---------------------------------------------------------------------------

# Work-item types. Epic is NOT a ticket type — epics are the named, color-coded
# container entities (the `epics` table); tickets of any type belong to one via
# `epic_id`. Hierarchy below the epic: a story groups tasks/bugs via `parent_id`.
TICKET_TYPES = ("feature", "bug", "story", "task")

# Priority scheme (provisional — Neil to confirm). P0 is most urgent; the queue
# is ordered by priority first, then FIFO within a priority (queue_seq).
PRIORITIES = ("P0", "P1", "P2", "P3")
DEFAULT_PRIORITY = "P2"

# Lifecycle statuses. `kind` groups them for the board's swimlanes; `label` is
# the human-facing name shown on cards.
#   discussion  — pre-pipeline: refine the ask, comment, set priority
#   queue       — waiting to be picked up (has a queue position)
#   agent       — the autonomous worker is actively in this stage
#   human_gate  — waiting on a person (Neil's PR review, Alex's user test, info)
#   terminal    — done
STATUS_META: Dict[str, Dict[str, str]] = {
    "discussion":        {"label": "Discussion",        "kind": "discussion"},
    "queued":            {"label": "Queued",            "kind": "queue"},
    "assessment":        {"label": "Assessment",        "kind": "agent"},
    "planning":          {"label": "Planning",          "kind": "agent"},
    "in_development":    {"label": "In Development",    "kind": "agent"},
    "self_review":       {"label": "Self-Review",       "kind": "agent"},
    "pr":                {"label": "PR — Awaiting OK",  "kind": "human_gate"},
    "user_review":       {"label": "User Review",       "kind": "human_gate"},
    "needs_info":        {"label": "Needs Info",        "kind": "human_gate"},
    "changes_requested": {"label": "Changes Requested", "kind": "human_gate"},
    "stalled":           {"label": "Stalled",           "kind": "human_gate"},
    "done":              {"label": "Done",              "kind": "terminal"},
    "cancelled":         {"label": "Won't Do",          "kind": "cancelled"},
}
STATUSES = tuple(STATUS_META.keys())

# The happy-path order, used for progress bars and "how far along" maths.
MAIN_LINE = (
    "queued", "assessment", "planning", "in_development",
    "self_review", "pr", "user_review", "done",
)

# Stages where the agent is running and so can be flipped to "stalled" by the
# heartbeat watchdog.
AGENT_STAGES = ("assessment", "planning", "in_development", "self_review")

# Allowed transitions: from-status -> set of legal to-statuses. Anything not
# listed here is rejected by transition(). The grooming gate, PR bounce, the
# self-review retry loop, the user-review fail->requeue loop, and the
# needs-info / stalled recovery paths are all encoded here.
TRANSITIONS: Dict[str, set] = {
    "discussion":        {"queued", "cancelled"},
    "queued":            {"assessment", "discussion", "cancelled", "stalled"},
    # "queued" is reachable from every agent stage so the agent can AUTO-REQUEUE a
    # ticket after a transient/infra failure (self-healing) instead of stranding
    # it in Stalled — see agent._recover_or_stall. "needs_info" from self_review
    # lets the recovery router bounce an underspecified ask back to the requester.
    "assessment":        {"planning", "needs_info", "stalled", "queued"},
    "planning":          {"in_development", "needs_info", "stalled", "queued"},
    "in_development":    {"self_review", "needs_info", "stalled", "queued"},
    # "user_review" direct from self_review is the direct_main path (no PR gate):
    # the agent committed straight to the base branch and advances the ticket itself.
    "self_review":       {"pr", "user_review", "in_development", "stalled", "queued", "needs_info"},
    "pr":                {"user_review", "changes_requested", "cancelled"},
    "changes_requested": {"in_development", "cancelled"},
    "user_review":       {"done", "queued", "discussion", "cancelled"},
    # Recovery paths: a bounced/stalled ticket re-enters the pipeline or returns
    # to discussion for amendment.
    "needs_info":        {"queued", "assessment", "planning", "in_development", "discussion", "cancelled"},
    "stalled":           {"queued", "assessment", "planning", "in_development", "needs_info", "cancelled"},
    "done":              {"queued"},  # reopen
    # "Won't Do" — a human dismisses an ask (e.g. redundant/out-of-scope). Allowed
    # from any human-controlled state above (not the live agent stages, to avoid
    # racing a running phase). Reopenable back into discussion or the queue.
    "cancelled":         {"discussion", "queued"},
}

# ticket_events.kind — what a timeline entry represents. 'impact' is a
# post-ship rating (1-5 + note) left on a Done ticket; 'grade' is the tester's
# 0-10 score of the BUILD quality given at user review. Latest per rater wins.
EVENT_KINDS = ("transition", "activity", "assessment", "plan", "comment", "note",
               "impact", "grade")

VALID_NOTIFY_EVENTS = ("needs_info", "pr_ready", "user_review", "stalled", "failed")


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:40] or "ticket"


# Words that signal a vague ask — used by the clarity heuristic to coach testers.
_VAGUE_WORDS = {"better", "improve", "improved", "fix", "stuff", "thing", "things",
                "nicer", "cleaner", "good", "bad", "broken", "off", "wrong", "weird"}


def score_clarity(title: str = "", description: str = "",
                  acceptance_criteria: str = "", type: str = "feature") -> dict:
    """Heuristic 0-100 quality score for an ask, with concrete suggestions. This
    is the coaching signal — it nudges testers toward specific, testable stories.
    Mirrored client-side for a live meter; stored at creation for analytics."""
    title = (title or "").strip()
    desc = (description or "").strip()
    ac = (acceptance_criteria or "").strip()
    score, sugg = 0, []

    if len(ac) >= 15:
        score += 30
    else:
        sugg.append('Add acceptance criteria — what does "done" look like?')

    if len(desc) >= 120:
        score += 25
    elif len(desc) >= 40:
        score += 12
        sugg.append("Add more detail to the description (context, why it matters).")
    else:
        sugg.append("Describe the ask in more detail — what, where, and why.")

    words = set(title.lower().split())
    vague = bool(words & _VAGUE_WORDS) or len(title) < 10
    if title and not vague and len(title) <= 90:
        score += 15
    elif vague:
        sugg.append('Make the title specific — avoid vague words like "better"/"fix".')

    blob = (desc + " " + ac).lower()
    concrete = ("/" in blob
                or bool(re.search(r"\b(when|should|so that|step|expected|click|open|see|"
                                  r"returns?|display|shows?)\b", blob))
                or bool(re.search(r"\d", blob)))
    if concrete:
        score += 20
    else:
        sugg.append("Add concrete behaviour — “when X, the user should see Y”.")

    if ac and re.search(r"\b(should|returns?|displays?|shows?|when|so that|must)\b", ac.lower()):
        score += 10
    elif ac:
        sugg.append('Phrase acceptance criteria as observable outcomes ("should show…").')

    score = max(0, min(100, score))
    level = "high" if score >= 70 else "medium" if score >= 40 else "low"
    return {"score": score, "level": level, "suggestions": sugg[:4]}


# ---------------------------------------------------------------------------
# Connection / schema
# ---------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    db_file = CONFIG.db_path
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_file, timeout=30.0)
    conn.row_factory = sqlite3.Row
    # WAL = concurrent readers don't block the single writer; busy_timeout lets
    # a contended write wait rather than fail instantly under the agent+UI load.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    title               TEXT NOT NULL,
    type                TEXT NOT NULL DEFAULT 'feature',
    description         TEXT NOT NULL DEFAULT '',
    acceptance_criteria TEXT NOT NULL DEFAULT '',
    clarity_score       INTEGER NOT NULL DEFAULT 0,   -- 0-100 ask-quality heuristic at creation
    clarity_level       TEXT NOT NULL DEFAULT '',     -- low | medium | high
    priority            TEXT NOT NULL DEFAULT 'P2',
    status              TEXT NOT NULL DEFAULT 'discussion',
    substage            TEXT NOT NULL DEFAULT '',
    queue_seq           INTEGER,            -- set when entering 'queued'; orders the queue within a priority
    iteration           INTEGER NOT NULL DEFAULT 0,
    branch              TEXT NOT NULL DEFAULT '',
    worktree_path       TEXT NOT NULL DEFAULT '',
    pr_url              TEXT NOT NULL DEFAULT '',
    test_instructions   TEXT NOT NULL DEFAULT '',
    touched_paths       TEXT NOT NULL DEFAULT '',   -- JSON list: files the implementation changed
    touched_routes      TEXT NOT NULL DEFAULT '',   -- JSON list: API route templates it affected
    seed_user_item_id   TEXT NOT NULL DEFAULT '',   -- the old-hub user_item this was promoted from
    created_by          TEXT NOT NULL DEFAULT '',
    assignee            TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);
CREATE INDEX IF NOT EXISTS idx_tickets_queue  ON tickets(status, priority, queue_seq);

CREATE TABLE IF NOT EXISTS ticket_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id  INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    ts         TEXT NOT NULL,
    phase      TEXT NOT NULL DEFAULT '',   -- the status this happened under
    actor      TEXT NOT NULL DEFAULT '',   -- 'agent' or a person's name
    kind       TEXT NOT NULL DEFAULT 'note',
    summary    TEXT NOT NULL DEFAULT '',
    payload    TEXT NOT NULL DEFAULT ''    -- JSON blob for structured detail
);

CREATE INDEX IF NOT EXISTS idx_events_ticket ON ticket_events(ticket_id, id);

CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id  INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    recipient  TEXT NOT NULL,
    channel    TEXT NOT NULL DEFAULT 'email',
    event      TEXT NOT NULL,
    subject    TEXT NOT NULL DEFAULT '',
    body       TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'pending',  -- pending | sent | failed
    created_at TEXT NOT NULL,
    sent_at    TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_notif_pending ON notifications(status, id);

CREATE TABLE IF NOT EXISTS ticket_links (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id  INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,  -- the NEW ticket (the complaint)
    target_id  INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,  -- the shipped ticket being implicated
    kind       TEXT NOT NULL DEFAULT 'regression',
    source     TEXT NOT NULL DEFAULT 'similarity',  -- mention | similarity | agent | human
    score      REAL,                                 -- similarity confidence (0-1) when source=similarity
    status     TEXT NOT NULL DEFAULT 'suspected',    -- suspected | confirmed | dismissed
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    resolved_by TEXT NOT NULL DEFAULT ''
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_links_pair ON ticket_links(ticket_id, target_id);
CREATE INDEX IF NOT EXISTS idx_links_target ON ticket_links(target_id, status);

-- Roadmap: a fixed-length waterfall cycle (Backlog → Week 1..N → Done) laid
-- over the existing lifecycle. Tickets keep their normal statuses; the roadmap
-- only records which week a ticket is committed to plus the hours maths.
-- Logic lives in roadmap.py; the schema lives here with everything else.
CREATE TABLE IF NOT EXISTS roadmap_cycles (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL DEFAULT '',
    start_date TEXT NOT NULL,                  -- ISO date; week N spans start+7*(N-1) .. +6
    weeks      INTEGER NOT NULL DEFAULT 5,
    created_at TEXT NOT NULL
);

-- One row per cycle per day — everything the burndown needs. Upserted on every
-- roadmap mutation and on read, so the chart stays correct without a cron job.
CREATE TABLE IF NOT EXISTS roadmap_snapshots (
    cycle_id        INTEGER NOT NULL REFERENCES roadmap_cycles(id) ON DELETE CASCADE,
    date            TEXT NOT NULL,             -- ISO date
    total_scope     REAL NOT NULL DEFAULT 0,   -- Σ estimate_hours committed to week lanes
    total_remaining REAL NOT NULL DEFAULT 0,   -- Σ remaining_hours still open in week lanes
    bumps           INTEGER NOT NULL DEFAULT 0,-- bump events recorded on this date
    PRIMARY KEY (cycle_id, date)
);

-- Same daily burndown maths, broken out per epic (epic_id 0 = tickets with no
-- epic), so an epic-filtered roadmap gets a real historical series instead of
-- projection-only. Rewritten (delete+insert) for today on every snapshot, so a
-- ticket moving between epics never leaves a stale row behind.
CREATE TABLE IF NOT EXISTS roadmap_epic_snapshots (
    cycle_id        INTEGER NOT NULL REFERENCES roadmap_cycles(id) ON DELETE CASCADE,
    date            TEXT NOT NULL,
    epic_id         INTEGER NOT NULL DEFAULT 0,
    total_scope     REAL NOT NULL DEFAULT 0,
    total_remaining REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (cycle_id, date, epic_id)
);

-- And the same again per assignee ('' = unassigned) for per-person burndowns.
CREATE TABLE IF NOT EXISTS roadmap_user_snapshots (
    cycle_id        INTEGER NOT NULL REFERENCES roadmap_cycles(id) ON DELETE CASCADE,
    date            TEXT NOT NULL,
    assignee        TEXT NOT NULL DEFAULT '',
    total_scope     REAL NOT NULL DEFAULT 0,
    total_remaining REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (cycle_id, date, assignee)
);

-- Epics: named, color-coded groupings of tickets (e.g. "Cellebrite",
-- "Financial"). A ticket belongs to at most one epic (tickets.epic_id).
CREATE TABLE IF NOT EXISTS epics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    color       TEXT NOT NULL DEFAULT '#6366f1',
    description TEXT NOT NULL DEFAULT '',
    created_by  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
"""


def init_db() -> None:
    """Create the schema if it doesn't exist (idempotent), with light migrations
    for columns added after first ship."""
    conn = _connect()
    try:
        conn.executescript(_SCHEMA)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tickets)")}
        for col, ddl in (("clarity_score", "INTEGER NOT NULL DEFAULT 0"),
                         ("clarity_level", "TEXT NOT NULL DEFAULT ''"),
                         ("touched_paths", "TEXT NOT NULL DEFAULT ''"),
                         ("touched_routes", "TEXT NOT NULL DEFAULT ''"),
                         # Roadmap overlay (see roadmap.py):
                         ("estimate_hours", "REAL"),                       # required to enter a week lane
                         ("remaining_hours", "REAL"),                      # counts down as work progresses
                         ("week_lane", "INTEGER"),                         # NULL = backlog; 1..cycle.weeks
                         ("bump_count", "INTEGER NOT NULL DEFAULT 0"),     # times bumped Wn -> Wn+1
                         # Build order for greenfield grooming / "Run Full Build":
                         ("build_seq", "INTEGER"),                         # 1-based build order; NULL = unset
                         # Interactive roadmap: manual work-state + logged effort, and the
                         # STRICT automation opt-in gate (a ticket may only be queued/built
                         # by the agent when dev_optin=1 — set solely by explicit handoff).
                         ("roadmap_status", "TEXT NOT NULL DEFAULT 'todo'"),  # backlog|todo|in_progress|done
                         ("hours_done", "REAL"),                             # actual effort logged
                         ("dev_optin", "INTEGER NOT NULL DEFAULT 0"),        # 1 = eligible for automation
                         ("epic_id", "INTEGER"),                             # epics.id; NULL = no epic
                         ("parent_id", "INTEGER"),                           # tickets.id of the parent story; NULL = top-level
                         ("engine", "TEXT NOT NULL DEFAULT ''")):            # build engine: ''=auto | claude | codex
            if col not in cols:
                conn.execute(f"ALTER TABLE tickets ADD COLUMN {col} {ddl}")
        conn.commit()
    finally:
        conn.close()


# Ensure the DB exists as soon as the module is imported.
init_db()


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def ticket_ref(ticket_id: int) -> str:
    """Human-facing id, e.g. DKT-12."""
    return f"DKT-{ticket_id}"


def _row_to_ticket(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    d["ref"] = ticket_ref(d["id"])
    meta = STATUS_META.get(d["status"], {})
    d["status_label"] = meta.get("label", d["status"])
    d["status_kind"] = meta.get("kind", "")
    for col in ("touched_paths", "touched_routes"):
        try:
            d[col] = json.loads(d.get(col) or "[]")
        except (ValueError, TypeError):
            d[col] = []
    epic = epics_map().get(d.get("epic_id")) if d.get("epic_id") else None
    d["epic_name"] = epic["name"] if epic else ""
    d["epic_color"] = epic["color"] if epic else ""
    d["parent_ref"] = ticket_ref(d["parent_id"]) if d.get("parent_id") else ""
    return d


def _row_to_event(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    if d.get("payload"):
        try:
            d["payload"] = json.loads(d["payload"])
        except (ValueError, TypeError):
            pass
    return d


# ---------------------------------------------------------------------------
# Epics — named, color-coded ticket groupings
# ---------------------------------------------------------------------------

# Distinct hues; a new epic without an explicit color takes the next unused.
EPIC_PALETTE = ["#6366f1", "#0ea5e9", "#10b981", "#f59e0b", "#ef4444",
                "#8b5cf6", "#ec4899", "#14b8a6", "#f97316", "#84cc16"]

# Per-process cache so every serialized ticket doesn't re-query epics.
# Invalidated on CRUD in this process; a short TTL covers other processes.
_EPIC_CACHE: Dict[str, Any] = {"at": 0.0, "map": {}}
_EPIC_TTL = 15.0


def epics_map() -> Dict[int, Dict[str, Any]]:
    now = time.monotonic()
    if now - _EPIC_CACHE["at"] > _EPIC_TTL:
        conn = _connect()
        try:
            _EPIC_CACHE["map"] = {r["id"]: dict(r) for r in
                                  conn.execute("SELECT * FROM epics").fetchall()}
            _EPIC_CACHE["at"] = now
        finally:
            conn.close()
    return _EPIC_CACHE["map"]


def _invalidate_epics() -> None:
    _EPIC_CACHE["at"] = 0.0


def list_epics() -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT e.*, COUNT(t.id) AS ticket_count,
                      SUM(CASE WHEN t.status='done' THEN 1 ELSE 0 END) AS done_count
               FROM epics e LEFT JOIN tickets t ON t.epic_id = e.id
               GROUP BY e.id ORDER BY e.name COLLATE NOCASE""").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def create_epic(name: str, color: str = "", description: str = "",
                created_by: str = "") -> Dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise ValueError("epic name is required")
    conn = _connect()
    try:
        existing = conn.execute(
            "SELECT id FROM epics WHERE name=? COLLATE NOCASE", (name,)).fetchone()
        if existing:
            raise ValueError(f"an epic named '{name}' already exists")
        if not color:
            used = {r["color"] for r in conn.execute("SELECT color FROM epics")}
            color = next((c for c in EPIC_PALETTE if c not in used),
                         EPIC_PALETTE[conn.execute("SELECT COUNT(*) FROM epics")
                                      .fetchone()[0] % len(EPIC_PALETTE)])
        cur = conn.execute(
            "INSERT INTO epics (name, color, description, created_by, created_at) "
            "VALUES (?,?,?,?,?)",
            (name[:120], color[:20], str(description)[:2000], created_by, utcnow_iso()))
        conn.commit()
        row = conn.execute("SELECT * FROM epics WHERE id=?", (cur.lastrowid,)).fetchone()
    finally:
        conn.close()
    _invalidate_epics()
    return dict(row)


def update_epic(epic_id: int, **fields) -> Optional[Dict[str, Any]]:
    sets, vals = [], []
    for k in ("name", "color", "description"):
        if k in fields and fields[k] is not None:
            sets.append(f"{k}=?")
            vals.append(str(fields[k]).strip())
    conn = _connect()
    try:
        if sets:
            vals.append(epic_id)
            conn.execute(f"UPDATE epics SET {', '.join(sets)} WHERE id=?", vals)
            conn.commit()
        row = conn.execute("SELECT * FROM epics WHERE id=?", (epic_id,)).fetchone()
    finally:
        conn.close()
    _invalidate_epics()
    return dict(row) if row else None


def delete_epic(epic_id: int) -> bool:
    """Delete an epic; its tickets are unlinked, never touched otherwise."""
    conn = _connect()
    try:
        conn.execute("UPDATE tickets SET epic_id=NULL WHERE epic_id=?", (epic_id,))
        n = conn.execute("DELETE FROM epics WHERE id=?", (epic_id,)).rowcount
        conn.commit()
    finally:
        conn.close()
    _invalidate_epics()
    return n > 0


# ---------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------

def create_ticket(
    title: str,
    type: str = "feature",
    description: str = "",
    acceptance_criteria: str = "",
    priority: str = DEFAULT_PRIORITY,
    created_by: str = "",
    seed_user_item_id: str = "",
    build_seq: Optional[int] = None,
    dev_optin: bool = False,
    epic_id: Optional[int] = None,
    parent_id: Optional[int] = None,
    estimate_hours: Optional[float] = None,
) -> Dict[str, Any]:
    """Raise a new ticket in the Discussion zone. Returns the created ticket.
    `build_seq` records 1-based build order (set by greenfield grooming; drives
    "Run Full Build"). `dev_optin=True` marks the ticket eligible for the automated
    pipeline — set ONLY by explicit acts (grooming a greenfield build).
    `parent_id` nests this ticket under a story; the child inherits the parent's
    epic when no `epic_id` is given."""
    title = (title or "").strip()
    if not title:
        raise ValueError("title is required")
    if type not in TICKET_TYPES:
        raise ValueError(f"type must be one of {TICKET_TYPES}")
    if priority not in PRIORITIES:
        priority = DEFAULT_PRIORITY
    if parent_id:
        parent = get_ticket(int(parent_id))
        if not parent:
            raise ValueError(f"parent ticket {parent_id} not found")
        if parent.get("parent_id"):
            raise ValueError(f"parent {parent['ref']} is itself a child — "
                             "only one level of nesting (story → task/bug)")
        if epic_id is None:
            epic_id = parent.get("epic_id")
    if estimate_hours is not None:
        estimate_hours = max(0.0, float(estimate_hours))
    now = utcnow_iso()
    clarity = score_clarity(title, description, acceptance_criteria, type)

    conn = _connect()
    try:
        cur = conn.execute(
            """INSERT INTO tickets
               (title, type, description, acceptance_criteria, clarity_score,
                clarity_level, priority, status, created_by, seed_user_item_id,
                build_seq, dev_optin, epic_id, parent_id, estimate_hours,
                created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,'discussion',?,?,?,?,?,?,?,?,?)""",
            (title[:300], type, str(description)[:20000],
             str(acceptance_criteria)[:10000], clarity["score"], clarity["level"],
             priority, created_by, seed_user_item_id, build_seq,
             1 if dev_optin else 0, epic_id or None, parent_id or None,
             estimate_hours, now, now),
        )
        tid = cur.lastrowid
        conn.execute(
            """INSERT INTO ticket_events (ticket_id, ts, phase, actor, kind, summary)
               VALUES (?,?,?,?,?,?)""",
            (tid, now, "discussion", created_by or "system", "transition",
             "Ticket created"),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
        ticket = _row_to_ticket(row)
    finally:
        conn.close()
    # Relatedness pass: is this really a follow-up of something already shipped?
    try:
        detect_links(ticket)
    except Exception:
        pass  # linking is best-effort; never block ticket creation
    return ticket


def get_ticket(ticket_id: int) -> Optional[Dict[str, Any]]:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        return _row_to_ticket(row) if row else None
    finally:
        conn.close()


def list_tickets(status: Optional[str] = None) -> List[Dict[str, Any]]:
    """All tickets (optionally filtered by status), newest-updated first."""
    conn = _connect()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM tickets WHERE status=? ORDER BY updated_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tickets ORDER BY updated_at DESC"
            ).fetchall()
        return [_row_to_ticket(r) for r in rows]
    finally:
        conn.close()


def unestimated_tickets() -> List[Dict[str, Any]]:
    """Open tickets with no estimate yet (candidates for auto-estimation).
    Excludes finished/cancelled work."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM tickets WHERE estimate_hours IS NULL "
            "AND status NOT IN ('done','cancelled') ORDER BY id"
        ).fetchall()
        return [_row_to_ticket(r) for r in rows]
    finally:
        conn.close()


# Fields a caller may patch directly (lifecycle status goes through transition()).
_EDITABLE = {
    "title", "type", "description", "acceptance_criteria", "priority",
    "substage", "branch", "worktree_path", "pr_url", "test_instructions",
    "assignee", "touched_paths", "touched_routes", "dev_optin", "epic_id",
    "parent_id", "engine",
}

ENGINES = ("", "claude", "codex")   # '' = auto (the agent's router decides)


def update_ticket(ticket_id: int, **fields) -> Optional[Dict[str, Any]]:
    """Patch editable fields (NOT status — use transition()). Returns the ticket."""
    sets, vals = [], []
    for k, v in fields.items():
        if k not in _EDITABLE:
            raise ValueError(f"field '{k}' is not directly editable")
        if k == "priority" and v not in PRIORITIES:
            continue
        if k == "type" and v not in TICKET_TYPES:
            continue
        if k == "engine":
            v = (v or "").strip().lower()
            if v not in ENGINES:
                raise ValueError(f"engine must be one of {ENGINES}")
        if k == "epic_id":
            v = int(v) if v else None       # 0 / "" / None all unlink the epic
            if v is not None and v not in epics_map():
                _invalidate_epics()          # maybe created moments ago elsewhere
                if v not in epics_map():
                    raise ValueError(f"unknown epic {v}")
        if k == "parent_id":
            v = int(v) if v else None       # 0 / "" / None all unnest
            if v is not None:
                if v == ticket_id:
                    raise ValueError("a ticket cannot be its own parent")
                parent = get_ticket(v)
                if not parent:
                    raise ValueError(f"parent ticket {v} not found")
                if parent.get("parent_id"):
                    raise ValueError(f"parent {parent['ref']} is itself a child — "
                                     "only one level of nesting (story → task/bug)")
        sets.append(f"{k}=?")
        vals.append(v)
    if not sets:
        return get_ticket(ticket_id)
    sets.append("updated_at=?")
    vals.append(utcnow_iso())
    vals.append(ticket_id)
    conn = _connect()
    try:
        conn.execute(f"UPDATE tickets SET {', '.join(sets)} WHERE id=?", vals)
        conn.commit()
    finally:
        conn.close()
    return get_ticket(ticket_id)


def children_of(ticket_id: int) -> List[Dict[str, Any]]:
    """Light summaries of the tickets nested under a story (for the detail
    view's breakdown list)."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM tickets WHERE parent_id=? ORDER BY id", (ticket_id,)
        ).fetchall()
        kids = [_row_to_ticket(r) for r in rows]
    finally:
        conn.close()
    return [{k: t.get(k) for k in ("id", "ref", "title", "type", "status",
                                   "status_label", "priority", "estimate_hours")}
            for t in kids]


def delete_ticket(ticket_id: int) -> bool:
    """Permanently delete a ticket. FK cascades remove its events, notifications,
    and relatedness links (both directions); child tickets are unnested, never
    deleted. Returns False if the ticket didn't exist."""
    conn = _connect()
    try:
        conn.execute("UPDATE tickets SET parent_id=NULL WHERE parent_id=?", (ticket_id,))
        n = conn.execute("DELETE FROM tickets WHERE id=?", (ticket_id,)).rowcount
        conn.commit()
    finally:
        conn.close()
    return bool(n)


def transition(
    ticket_id: int,
    to_status: str,
    actor: str = "system",
    summary: str = "",
    payload: Optional[dict] = None,
) -> Dict[str, Any]:
    """Move a ticket to `to_status`, enforcing the state machine.

    Side effects, all in one transaction:
      - validates the (from -> to) move against TRANSITIONS
      - on entering 'queued', assigns the next queue_seq
      - on the user-review fail->requeue loop, bumps `iteration`
      - records a 'transition' event so the timeline stays in sync

    Raises ValueError on an illegal transition or unknown ticket/status.
    """
    if to_status not in STATUSES:
        raise ValueError(f"unknown status '{to_status}'")
    now = utcnow_iso()
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        if not row:
            raise ValueError(f"ticket {ticket_id} not found")
        cur_status = row["status"]
        if cur_status == to_status:
            # No-op move; record nothing, just return current.
            return _row_to_ticket(row)
        allowed = TRANSITIONS.get(cur_status, set())
        if to_status not in allowed:
            raise ValueError(
                f"illegal transition {cur_status} -> {to_status} "
                f"(allowed: {sorted(allowed)})"
            )

        sets = ["status=?", "updated_at=?"]
        vals: List[Any] = [to_status, now]

        # Entering the queue: stamp a fresh queue_seq (FIFO within a priority).
        if to_status == "queued":
            nxt = conn.execute(
                "SELECT COALESCE(MAX(queue_seq),0)+1 AS n FROM tickets"
            ).fetchone()["n"]
            sets.append("queue_seq=?")
            vals.append(nxt)

        # User-review bounce back into the queue = a new iteration of the ask.
        if cur_status == "user_review" and to_status in ("queued", "discussion"):
            sets.append("iteration=?")
            vals.append(int(row["iteration"]) + 1)

        # Done zeroes the roadmap hours so the hours-to-completion counter and
        # burndown react the moment work ships (see roadmap.py).
        if to_status == "done":
            sets.append("remaining_hours=?")
            vals.append(0)

        vals.append(ticket_id)
        conn.execute(f"UPDATE tickets SET {', '.join(sets)} WHERE id=?", vals)
        conn.execute(
            """INSERT INTO ticket_events (ticket_id, ts, phase, actor, kind, summary, payload)
               VALUES (?,?,?,?,?,?,?)""",
            (ticket_id, now, to_status, actor, "transition",
             summary or f"{cur_status} → {to_status}",
             json.dumps(payload) if payload else ""),
        )
        conn.commit()
        out = conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        return _row_to_ticket(out)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Events (work history + the live activity ticker)
# ---------------------------------------------------------------------------

def add_event(
    ticket_id: int,
    kind: str,
    summary: str = "",
    actor: str = "",
    phase: str = "",
    payload: Optional[dict] = None,
) -> Dict[str, Any]:
    """Append a work-history / audit entry. `phase` defaults to current status."""
    if kind not in EVENT_KINDS:
        raise ValueError(f"kind must be one of {EVENT_KINDS}")
    now = utcnow_iso()
    conn = _connect()
    try:
        if not phase:
            r = conn.execute("SELECT status FROM tickets WHERE id=?", (ticket_id,)).fetchone()
            if not r:
                raise ValueError(f"ticket {ticket_id} not found")
            phase = r["status"]
        cur = conn.execute(
            """INSERT INTO ticket_events (ticket_id, ts, phase, actor, kind, summary, payload)
               VALUES (?,?,?,?,?,?,?)""",
            (ticket_id, now, phase, actor, kind, str(summary)[:5000],
             json.dumps(payload) if payload else ""),
        )
        # Bump the ticket's updated_at so "last activity" reflects the event.
        conn.execute("UPDATE tickets SET updated_at=? WHERE id=?", (now, ticket_id))
        conn.commit()
        row = conn.execute(
            "SELECT * FROM ticket_events WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        return _row_to_event(row)
    finally:
        conn.close()


def set_activity(ticket_id: int, text: str, actor: str = "agent") -> Dict[str, Any]:
    """Update the 'currently working on' ticker (an 'activity' event)."""
    return add_event(ticket_id, "activity", summary=text, actor=actor)


def get_events(ticket_id: int) -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM ticket_events WHERE ticket_id=? ORDER BY id ASC",
            (ticket_id,),
        ).fetchall()
        return [_row_to_event(r) for r in rows]
    finally:
        conn.close()


def current_activity(ticket_id: int) -> Optional[Dict[str, Any]]:
    """The latest 'activity' event — what the agent is doing right now."""
    conn = _connect()
    try:
        row = conn.execute(
            """SELECT * FROM ticket_events
               WHERE ticket_id=? AND kind='activity' ORDER BY id DESC LIMIT 1""",
            (ticket_id,),
        ).fetchone()
        return _row_to_event(row) if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------

def queue() -> List[Dict[str, Any]]:
    """Queued tickets in pick-up order (priority, then FIFO). Each gets a
    1-based `position`."""
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT * FROM tickets WHERE status='queued'
               ORDER BY priority ASC, queue_seq ASC"""
        ).fetchall()
        out = []
        for i, r in enumerate(rows, start=1):
            t = _row_to_ticket(r)
            t["position"] = i
            out.append(t)
        return out
    finally:
        conn.close()


def next_in_queue() -> Optional[Dict[str, Any]]:
    """The ticket the orchestrator should pick up next (highest priority, oldest)."""
    q = queue()
    return q[0] if q else None


def requeue_stuck_agent_tickets() -> List[Dict[str, Any]]:
    """Recovery: re-queue tickets left mid-pipeline in an agent stage. The queue
    only feeds 'queued' tickets, so a ticket interrupted in assessment/planning/
    in_development/self_review (e.g. the agent was restarted) would otherwise
    strand forever. The agent is single-instance, so on startup nothing is truly
    in-flight — safe to re-queue and re-run from the top. Returns what was reset."""
    conn = _connect()
    try:
        placeholders = ",".join("?" * len(AGENT_STAGES))
        rows = conn.execute(
            f"SELECT id, status FROM tickets WHERE status IN ({placeholders}) ORDER BY id",
            AGENT_STAGES,
        ).fetchall()
        stuck = [(r["id"], r["status"]) for r in rows]
        for tid, _st in stuck:
            nxt = conn.execute(
                "SELECT COALESCE(MAX(queue_seq),0)+1 AS n FROM tickets"
            ).fetchone()["n"]
            conn.execute("UPDATE tickets SET status='queued', queue_seq=?, updated_at=? WHERE id=?",
                         (nxt, utcnow_iso(), tid))
        conn.commit()
    finally:
        conn.close()
    for tid, st in stuck:
        add_event(tid, "transition", actor="agent", phase="queued",
                  summary=f"Re-queued for resume (was '{st}' when the agent restarted)")
    return [{"id": tid, "from": st} for tid, st in stuck]


def queue_position(ticket_id: int) -> Optional[int]:
    """1-based position of a queued ticket, or None if it isn't queued."""
    for t in queue():
        if t["id"] == ticket_id:
            return t["position"]
    return None


def effort_by_ticket() -> Dict[int, Dict[str, Any]]:
    """Per-ticket agent-effort rollup (seconds + cost) from event payloads, for
    the board cards — part of making development cost legible at a glance."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT ticket_id, payload FROM ticket_events WHERE payload != ''"
        ).fetchall()
    finally:
        conn.close()
    out: Dict[int, Dict[str, Any]] = {}
    for r in rows:
        try:
            p = json.loads(r["payload"])
        except ValueError:
            continue
        if not isinstance(p, dict):
            continue
        secs, cost = p.get("duration_secs"), p.get("cost_usd")
        if secs is None and cost is None:
            continue
        d = out.setdefault(r["ticket_id"], {"secs": 0.0, "cost": 0.0})
        d["secs"] += float(secs or 0)
        d["cost"] += float(cost or 0)
    for d in out.values():
        d["secs"] = round(d["secs"])
        d["cost"] = round(d["cost"], 2)
    return out


# ---------------------------------------------------------------------------
# Analytics (Phase 4 — coaching dashboard)
# ---------------------------------------------------------------------------

def analytics() -> Dict[str, Any]:
    """Aggregate metrics for the coaching dashboard: throughput, agent effort
    (time + cost), quality signals (bounces, resubmits), per-tester breakdown,
    clarity distribution, and a recent 'bounced & why' feed."""
    conn = _connect()
    try:
        tickets = [dict(r) for r in conn.execute("SELECT * FROM tickets").fetchall()]
        evs = [dict(r) for r in conn.execute(
            """SELECT te.*, t.title AS tk_title FROM ticket_events te
               JOIN tickets t ON t.id = te.ticket_id ORDER BY te.id""").fetchall()]
    finally:
        conn.close()

    total = len(tickets)
    by_status: Dict[str, int] = {}
    for t in tickets:
        by_status[t["status"]] = by_status.get(t["status"], 0) + 1
    done = by_status.get("done", 0)

    # Effort from event payloads.
    cost = secs = 0.0
    eff_tickets = set()
    for e in evs:
        p = e.get("payload")
        if isinstance(p, str) and p:
            try:
                p = json.loads(p)
            except ValueError:
                p = None
        if isinstance(p, dict):
            if p.get("cost_usd"):
                cost += float(p["cost_usd"]); eff_tickets.add(e["ticket_id"])
            if p.get("duration_secs"):
                secs += float(p["duration_secs"])

    # Quality signals.
    needs_info = {e["ticket_id"] for e in evs
                  if e["phase"] == "needs_info" and e["kind"] == "transition"}
    clar_comment = {e["ticket_id"]: e["summary"] for e in evs
                    if e["kind"] == "comment" and str(e.get("summary", "")).startswith("Needs clarification")}
    resubmits = sum(int(t.get("iteration") or 0) for t in tickets)
    failed_review = sum(1 for t in tickets if int(t.get("iteration") or 0) > 0)

    # Per-tester coaching table.
    authors: Dict[str, Dict[str, Any]] = {}
    for t in tickets:
        a = t.get("created_by") or "—"
        d = authors.setdefault(a, {"author": a, "tickets": 0, "c_sum": 0, "c_n": 0,
                                   "iterations": 0, "bounced": 0})
        d["tickets"] += 1
        if t.get("clarity_score"):
            d["c_sum"] += int(t["clarity_score"]); d["c_n"] += 1
        d["iterations"] += int(t.get("iteration") or 0)
        if t["id"] in needs_info:
            d["bounced"] += 1
    per_author = sorted(
        [{"author": d["author"], "tickets": d["tickets"],
          "avg_clarity": round(d["c_sum"] / d["c_n"]) if d["c_n"] else None,
          "iterations": d["iterations"], "bounced": d["bounced"]}
         for d in authors.values()],
        key=lambda x: -x["tickets"])

    # Clarity distribution.
    clar = {"low": 0, "medium": 0, "high": 0}
    c_sum = c_n = 0
    for t in tickets:
        if t.get("clarity_level") in clar:
            clar[t["clarity_level"]] += 1
        if t.get("clarity_score"):
            c_sum += int(t["clarity_score"]); c_n += 1

    # Recent "bounced & why" feed (needs-info clarifications + resubmit reasons).
    bounced = []
    for e in reversed(evs):
        if len(bounced) >= 15:
            break
        if e["phase"] == "needs_info" and e["kind"] == "transition":
            bounced.append({"ref": ticket_ref(e["ticket_id"]), "title": e.get("tk_title", ""),
                            "kind": "needs_info",
                            "reason": clar_comment.get(e["ticket_id"], e.get("summary", "")),
                            "ts": e["ts"]})
        elif e["kind"] == "comment" and str(e.get("summary", "")).startswith("Resubmitted"):
            bounced.append({"ref": ticket_ref(e["ticket_id"]), "title": e.get("tk_title", ""),
                            "kind": "resubmit", "reason": e.get("summary", ""), "ts": e["ts"]})

    # ---- time-series + pipeline health (raw ts data; nothing bucketed before) ----
    from datetime import datetime as _dt
    from collections import defaultdict as _dd

    def _date(ts): return (ts or "")[:10]

    def _epoch(ts):
        try:
            return _dt.fromisoformat(ts).timestamp()
        except Exception:
            return None

    def _pl(e):
        p = e.get("payload")
        if isinstance(p, str) and p:
            try:
                p = json.loads(p)
            except ValueError:
                p = None
        return p if isinstance(p, dict) else {}

    throughput_by_day: Dict[str, int] = {}
    cost_by_day: Dict[str, float] = {}
    for e in evs:
        if e["kind"] == "transition" and e["phase"] == "done":
            throughput_by_day[_date(e["ts"])] = throughput_by_day.get(_date(e["ts"]), 0) + 1
        p = _pl(e)
        if p.get("cost_usd"):
            d = _date(e["ts"]); cost_by_day[d] = cost_by_day.get(d, 0.0) + float(p["cost_usd"])

    done_ts: Dict[int, str] = {}
    for e in evs:
        if e["kind"] == "transition" and e["phase"] == "done" and e["ticket_id"] not in done_ts:
            done_ts[e["ticket_id"]] = e["ts"]
    tk_by_id = {t["id"]: t for t in tickets}
    cycle_secs, est_actual = [], []
    for tid, dts in done_ts.items():
        t = tk_by_id.get(tid)
        if not t:
            continue
        c, d = _epoch(t.get("created_at")), _epoch(dts)
        if c and d and d >= c:
            cycle_secs.append(d - c)
        if t.get("estimate_hours") and t.get("hours_done"):
            est_actual.append({"ref": ticket_ref(tid),
                               "estimate": round(float(t["estimate_hours"]), 1),
                               "actual": round(float(t["hours_done"]), 2)})

    verified = unverified = 0
    for tid in done_ts:
        blob = " ".join(str(e.get("summary") or "") for e in evs
                        if e["ticket_id"] == tid and e["kind"] == "note")
        if "NOT agent-verified" in blob or "could NOT verify" in blob:
            unverified += 1
        elif "agent-verified" in blob or "Verified —" in blob:
            verified += 1

    per_ticket_trans = _dd(list)
    for e in evs:
        if e["kind"] == "transition":
            per_ticket_trans[e["ticket_id"]].append(e)
    stage_secs, stage_n = _dd(float), _dd(int)
    for tl in per_ticket_trans.values():
        for a, b in zip(tl, tl[1:]):
            ea, eb = _epoch(a["ts"]), _epoch(b["ts"])
            if ea and eb and eb >= ea:
                stage_secs[a["phase"]] += (eb - ea); stage_n[a["phase"]] += 1
    time_in_stage = [{"status": s, "avg_secs": round(stage_secs[s] / stage_n[s]) if stage_n[s] else 0,
                      "count": stage_n[s]} for s in MAIN_LINE if s in stage_secs]

    def _series(d):
        return [{"date": k, "value": round(v, 2)} for k, v in sorted(d.items())]

    pipeline = {
        "throughput_by_day": _series(throughput_by_day),
        "cost_by_day": _series(cost_by_day),
        "avg_cycle_secs": round(sum(cycle_secs) / len(cycle_secs)) if cycle_secs else 0,
        "cycle_count": len(cycle_secs),
        "time_in_stage": time_in_stage,
        "verified": verified, "unverified": unverified,
        "rework_rate": round(failed_review / total * 100) if total else 0,
        "estimate_vs_actual": est_actual,
        "wip": {s: by_status.get(s, 0) for s in MAIN_LINE if s != "done"},
    }

    # ---- per-ticket enriched dataset: the frontend filters / aggregates / compares
    # this client-side (any slice or side-by-side is instant, no endpoint per filter). ----
    agg = _dd(lambda: {"secs": 0.0, "cost": 0.0, "turns": 0})
    notes_by = _dd(list)
    stalls_by = _dd(int)                       # transitions into Stalled per ticket
    grades_by = _dd(dict)                      # ticket -> {rater: latest 0-10 score}
    # Per-(engine, model) run rollup — every agent run event carries its engine
    # + model since v0.13, so the scoreboard can compare families AND versions.
    model_agg = _dd(lambda: {"runs": 0, "secs": 0.0, "cost": 0.0, "turns": 0,
                             "tokens_in": 0, "tokens_out": 0, "tickets": set()})
    for e in evs:
        p = _pl(e)
        if p:
            a = agg[e["ticket_id"]]
            a["secs"] += float(p.get("duration_secs") or 0)
            a["cost"] += float(p.get("cost_usd") or 0)
            a["turns"] += int(p.get("turns") or 0)
            if p.get("duration_secs") and (p.get("engine") or p.get("model")):
                m = model_agg[(p.get("engine") or "claude", p.get("model") or "")]
                m["runs"] += 1
                m["secs"] += float(p.get("duration_secs") or 0)
                m["cost"] += float(p.get("cost_usd") or 0)
                m["turns"] += int(p.get("turns") or 0)
                tok = p.get("tokens") or {}
                m["tokens_in"] += int(tok.get("input") or 0)
                m["tokens_out"] += int(tok.get("output") or 0)
                m["tickets"].add(e["ticket_id"])
            if e["kind"] == "grade" and p.get("score") is not None:
                grades_by[e["ticket_id"]][e.get("actor") or "?"] = float(p["score"])
        if e["kind"] == "transition" and e.get("phase") == "stalled":
            stalls_by[e["ticket_id"]] += 1
        if e["kind"] == "note":
            notes_by[e["ticket_id"]].append(str(e.get("summary") or ""))

    def _grade(tid):
        g = grades_by.get(tid)
        return round(sum(g.values()) / len(g), 1) if g else None

    def _verified(tid):
        blob = " ".join(notes_by.get(tid, []))
        if "NOT agent-verified" in blob or "could NOT verify" in blob:
            return False
        if "agent-verified" in blob or "Verified —" in blob:
            return True
        return None

    ticket_rows = []
    for t in tickets:
        tid = t["id"]
        a = agg.get(tid) or {"secs": 0.0, "cost": 0.0, "turns": 0}
        dts = done_ts.get(tid)
        cs = None
        if dts:
            c, d = _epoch(t.get("created_at")), _epoch(dts)
            if c and d and d >= c:
                cs = round(d - c)
        ep = epics_map().get(t.get("epic_id")) if t.get("epic_id") else None
        ticket_rows.append({
            "id": tid, "ref": ticket_ref(tid), "title": t.get("title", ""),
            "type": t.get("type"), "status": t.get("status"), "priority": t.get("priority"),
            "epic_id": t.get("epic_id"), "epic_name": ep["name"] if ep else "",
            "epic_color": ep["color"] if ep else "",
            "assignee": t.get("assignee") or "", "created_by": t.get("created_by") or "",
            "created_at": t.get("created_at"), "updated_at": t.get("updated_at"),
            "done_ts": dts, "cycle_secs": cs,
            "agent_secs": round(a["secs"]), "cost_usd": round(a["cost"], 4), "turns": a["turns"],
            "is_automated": a["secs"] > 0,      # the agent ran on it (has run-events)
            "engine": t.get("engine") or "",    # build engine ('' until routed)
            "stalls": stalls_by.get(tid, 0),    # times it fell into Stalled
            "grade": _grade(tid),               # avg tester build-grade (0-10)
            "hours_done": t.get("hours_done"), "estimate_hours": t.get("estimate_hours"),
            "roadmap_status": t.get("roadmap_status"), "week_lane": t.get("week_lane"),
            "iteration": int(t.get("iteration") or 0),
            "clarity_score": t.get("clarity_score"), "clarity_level": t.get("clarity_level"),
            "verified": _verified(tid),
        })

    models = sorted(
        [{"engine": k[0], "model": k[1], "runs": v["runs"],
          "secs": round(v["secs"]), "cost_usd": round(v["cost"], 2),
          "turns": v["turns"], "tokens_in": v["tokens_in"],
          "tokens_out": v["tokens_out"], "tickets": len(v["tickets"])}
         for k, v in model_agg.items()],
        key=lambda m: -m["secs"])

    return {
        "tickets": ticket_rows,
        "models": models,
        "totals": {"total": total, "done": done, "open": total - done, "by_status": by_status},
        "effort": {"total_cost_usd": round(cost, 2), "total_secs": round(secs),
                   "tickets_with_effort": len(eff_tickets),
                   "avg_cost_usd": round(cost / len(eff_tickets), 2) if eff_tickets else 0,
                   "avg_secs": round(secs / len(eff_tickets)) if eff_tickets else 0},
        "quality": {"bounced_tickets": len(needs_info), "resubmits": resubmits,
                    "failed_review": failed_review,
                    "bounce_rate": round(len(needs_info) / total * 100) if total else 0},
        "clarity": {"distribution": clar, "avg": round(c_sum / c_n) if c_n else None},
        "per_author": per_author,
        "recently_bounced": bounced,
        "pipeline": pipeline,
    }


# ---------------------------------------------------------------------------
# Ticket relatedness (Phase 6) — "did an old fix actually stick?"
#
# A new ticket that's really a follow-up of something already shipped means the
# shipped solution wasn't satisfactory — that should show up (and score) against
# the old ticket automatically. Three detection sources, in increasing strength:
#   similarity — lexical match at creation time → SUSPECTED (needs a human or
#                the agent to confirm; never penalises on its own)
#   mention    — the new ticket literally names DKT-<n> → CONFIRMED
#   agent      — the assessment phase explores the codebase and flags the link
#                (RELATED: DKT-n control line) → CONFIRMED
#   human      — a tester confirms/dismisses a suspected link in the UI
# Only CONFIRMED links count as regressions in the profile maths.
# ---------------------------------------------------------------------------

_STOPWORDS = {
    "the", "a", "an", "in", "on", "at", "to", "of", "and", "or", "is", "are",
    "was", "be", "been", "it", "its", "this", "that", "with", "for", "from",
    "i", "my", "we", "our", "you", "your", "they", "there", "have", "has",
    "do", "does", "did", "can", "cant", "cannot", "will", "would", "should",
    "when", "what", "how", "why", "as", "by", "so", "if", "but", "also",
    "please", "need", "needs", "want", "like", "get", "gets", "getting",
    "app", "application", "ticket", "feature", "bug",
}

SIMILARITY_THRESHOLD = 0.30


def _sim_terms(title: str, description: str = "", acceptance: str = "") -> Dict[str, float]:
    """Term-frequency map of an ask, title tokens weighted 3× (titles carry the
    most signal in short tickets)."""
    terms: Dict[str, float] = {}
    for text, w in ((title, 3.0), (description, 1.0), (acceptance, 1.0)):
        for tok in re.findall(r"[a-z0-9]{2,}", (text or "").lower()):
            if tok not in _STOPWORDS:
                terms[tok] = terms.get(tok, 0.0) + w
    return terms


def _cosine(a: Dict[str, float], b: Dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(v * b[k] for k, v in a.items() if k in b)
    na = sum(v * v for v in a.values()) ** 0.5
    nb = sum(v * v for v in b.values()) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def shipped_tickets() -> List[Dict[str, Any]]:
    """Tickets that have reached Done at some point (still-done OR reopened) —
    the candidate set a new complaint could be a follow-up of."""
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT DISTINCT t.* FROM tickets t
               JOIN ticket_events e ON e.ticket_id = t.id
               WHERE e.kind='transition' AND e.phase='done'
               ORDER BY t.id"""
        ).fetchall()
        return [_row_to_ticket(r) for r in rows]
    finally:
        conn.close()


def find_related_shipped(title: str, description: str = "",
                         acceptance_criteria: str = "",
                         exclude_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Shipped tickets lexically similar to the given ask, best first."""
    probe = _sim_terms(title, description, acceptance_criteria)
    out = []
    for t in shipped_tickets():
        if t["id"] == exclude_id:
            continue
        score = _cosine(probe, _sim_terms(t["title"], t["description"],
                                          t["acceptance_criteria"]))
        if score >= SIMILARITY_THRESHOLD:
            out.append({"ticket": t, "score": round(score, 3)})
    out.sort(key=lambda x: -x["score"])
    return out


def add_link(ticket_id: int, target_id: int, source: str,
             status: str = "suspected", score: Optional[float] = None,
             note: str = "") -> Optional[Dict[str, Any]]:
    """Record that `ticket_id` (new complaint) implicates shipped `target_id`.
    One link per pair; a confirmed link is never downgraded to suspected."""
    if ticket_id == target_id:
        return None
    now = utcnow_iso()
    conn = _connect()
    try:
        existing = conn.execute(
            "SELECT * FROM ticket_links WHERE ticket_id=? AND target_id=?",
            (ticket_id, target_id)).fetchone()
        if existing:
            if existing["status"] in ("confirmed", "dismissed") or status != "confirmed":
                return dict(existing)
            conn.execute(
                "UPDATE ticket_links SET status=?, source=?, note=? WHERE id=?",
                (status, source, note[:500], existing["id"]))
            conn.commit()
            return dict(conn.execute("SELECT * FROM ticket_links WHERE id=?",
                                     (existing["id"],)).fetchone())
        cur = conn.execute(
            """INSERT INTO ticket_links
               (ticket_id, target_id, kind, source, score, status, note, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (ticket_id, target_id, "regression", source, score, status,
             note[:500], now))
        conn.commit()
        return dict(conn.execute("SELECT * FROM ticket_links WHERE id=?",
                                 (cur.lastrowid,)).fetchone())
    finally:
        conn.close()


def resolve_link(link_id: int, action: str, actor: str = "") -> Dict[str, Any]:
    """Human verdict on a suspected link: 'confirm' or 'dismiss'."""
    if action not in ("confirm", "dismiss"):
        raise ValueError("action must be 'confirm' or 'dismiss'")
    status = "confirmed" if action == "confirm" else "dismissed"
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM ticket_links WHERE id=?", (link_id,)).fetchone()
        if not row:
            raise ValueError(f"link {link_id} not found")
        conn.execute(
            "UPDATE ticket_links SET status=?, source='human', resolved_by=? WHERE id=?",
            (status, actor, link_id))
        conn.commit()
        return dict(conn.execute("SELECT * FROM ticket_links WHERE id=?",
                                 (link_id,)).fetchone())
    finally:
        conn.close()


def links_for(ticket_id: int) -> Dict[str, List[Dict[str, Any]]]:
    """Links from a ticket's point of view: `out` = shipped tickets THIS one
    implicates; `in` = later tickets implicating THIS shipped one."""
    conn = _connect()
    try:
        out_rows = conn.execute(
            """SELECT l.*, t.title AS other_title, t.status AS other_status
               FROM ticket_links l JOIN tickets t ON t.id = l.target_id
               WHERE l.ticket_id=? ORDER BY l.id""", (ticket_id,)).fetchall()
        in_rows = conn.execute(
            """SELECT l.*, t.title AS other_title, t.status AS other_status
               FROM ticket_links l JOIN tickets t ON t.id = l.ticket_id
               WHERE l.target_id=? ORDER BY l.id""", (ticket_id,)).fetchall()
    finally:
        conn.close()

    def _fmt(rows, other_key):
        out = []
        for r in rows:
            d = dict(r)
            d["other_ref"] = ticket_ref(d[other_key])
            out.append(d)
        return out

    return {"out": _fmt(out_rows, "target_id"), "in": _fmt(in_rows, "ticket_id")}


def detect_links(ticket: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Creation-time relatedness pass for a new ticket: explicit DKT-n mentions
    become confirmed links; lexical similarity becomes suspected links (with a
    timeline note inviting confirm/dismiss). Returns the links created."""
    created = []
    text = f'{ticket.get("title", "")} {ticket.get("description", "")}'
    shipped_ids = {t["id"]: t for t in shipped_tickets()}

    for m in _DKT_RE.finditer(text):
        tgt = int(m.group(1))
        if tgt in shipped_ids and tgt != ticket["id"]:
            ln = add_link(ticket["id"], tgt, source="mention", status="confirmed",
                          note="Named in the ticket text")
            if ln:
                created.append(ln)
                add_event(ticket["id"], "note", actor="system",
                          summary=f"Marked as a follow-up of shipped {ticket_ref(tgt)} "
                                  f"(named in the text) — this counts against "
                                  f"{ticket_ref(tgt)}'s post-ship health.")

    linked = {ln["target_id"] for ln in created}
    for rel in find_related_shipped(ticket.get("title", ""),
                                    ticket.get("description", ""),
                                    ticket.get("acceptance_criteria", ""),
                                    exclude_id=ticket["id"])[:2]:
        tgt = rel["ticket"]
        if tgt["id"] in linked:
            continue
        ln = add_link(ticket["id"], tgt["id"], source="similarity",
                      status="suspected", score=rel["score"])
        if ln and ln["status"] == "suspected":
            created.append(ln)
            add_event(ticket["id"], "note", actor="system",
                      summary=f"This looks related to shipped {tgt['ref']} "
                              f"“{tgt['title'][:60]}” (similarity "
                              f"{round(rel['score'] * 100)}%). If that fix didn't "
                              f"fully solve it, confirm the link in the Related "
                              f"panel — otherwise dismiss it.")
    return created


def platform_performance(ticket: Dict[str, Any],
                         done_ts: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Real post-ship performance of a shipped ticket, from platform telemetry:
    traffic + error rate on the routes the implementation touched, since the day
    it shipped, against a 7-day pre-ship baseline. Returns None when the ticket
    has no route map (nothing measurable) or never shipped."""
    routes = ticket.get("touched_routes") or []
    if isinstance(routes, str):
        try:
            routes = json.loads(routes or "[]")
        except (ValueError, TypeError):
            routes = []
    if not routes:
        return None
    if done_ts is None:
        conn = _connect()
        try:
            row = conn.execute(
                """SELECT ts FROM ticket_events
                   WHERE ticket_id=? AND kind='transition' AND phase='done'
                   ORDER BY id LIMIT 1""", (ticket["id"],)).fetchone()
        finally:
            conn.close()
        done_ts = row["ts"] if row else None
    if not done_ts:
        return None

    from docket_dev import telemetry as tel
    done_day = done_ts[:10]
    since = tel.route_stats(routes, since_day=done_day)
    try:
        d0 = datetime.fromisoformat(done_ts)
        base = tel.route_stats(routes,
                               since_day=(d0 - timedelta(days=7)).strftime("%Y-%m-%d"),
                               until_day=(d0 - timedelta(days=1)).strftime("%Y-%m-%d"))
    except (ValueError, TypeError):
        base = {"hits": 0, "errors": 0, "err_rate": 0.0, "avg_ms": None}

    # 'degraded' needs real evidence: several 5xx AND a rate clearly above the
    # pre-ship baseline — a single blip on a busy route shouldn't tank a score.
    glob = None
    if since["hits"] == 0:
        verdict = "no_traffic"
    elif since["errors"] >= 3 and since["err_rate"] > max(0.05, base["err_rate"] * 2):
        verdict = "degraded"
    else:
        # Collateral damage: the feature's own routes look clean, but did the
        # PLATFORM's error rate jump after this shipped? Attributable (degraded)
        # only if nothing else shipped in the same window; otherwise 'watch'
        # (surfaced for a human, no automatic score penalty).
        g_since = tel.global_stats(since_day=done_day)
        try:
            d0 = datetime.fromisoformat(done_ts)
            g_base = tel.global_stats(
                since_day=(d0 - timedelta(days=7)).strftime("%Y-%m-%d"),
                until_day=(d0 - timedelta(days=1)).strftime("%Y-%m-%d"))
        except (ValueError, TypeError):
            g_base = {"hits": 0, "errors": 0, "err_rate": 0.0}
        spiked = (g_since["errors"] >= 3
                  and g_since["err_rate"] > max(0.05, g_base["err_rate"] * 2))
        if spiked:
            conn = _connect()
            try:
                others = conn.execute(
                    """SELECT COUNT(DISTINCT ticket_id) AS n FROM ticket_events
                       WHERE kind='transition' AND phase='done'
                         AND ts >= ? AND ticket_id != ?""",
                    (done_ts, ticket["id"])).fetchone()["n"]
            finally:
                conn.close()
            verdict = "degraded" if others == 0 else "watch"
            glob = {"since": g_since, "baseline": g_base, "other_ships": others}
        else:
            verdict = "healthy"
    return {"routes": routes, "since_day": done_day,
            "hits": since["hits"], "errors": since["errors"],
            "err_rate": since["err_rate"], "avg_ms": since["avg_ms"],
            "baseline": base, "global": glob, "verdict": verdict}


# ---------------------------------------------------------------------------
# Tester profiles (Phase 5 — gamified coaching)
#
# Per-tester scorecards built from the same tickets + events the board uses.
# Six dimensions roll up into one transparent "Docket Score"; the weights are
# returned to the UI so the score is never a black box. The negative signals
# (bounces, retries) live inside "first-time-through" so the framing stays
# "ship it clean", not "you failed". Post-ship impact = human star-ratings on
# Done tickets plus auto-detected regressions (a later bug ticket whose text
# mentions DKT-<n> counts against ticket n's health).
# ---------------------------------------------------------------------------

SCORE_WEIGHTS = {
    "clarity": 0.30,        # avg clarity of their asks (the coaching core)
    "first_time": 0.25,     # % of shipped tickets with no bounces / retries
    "helpfulness": 0.15,    # comments on OTHERS' tickets that got them moving
    "responsiveness": 0.10, # how fast they test what's sent to them
    "efficiency": 0.10,     # agent cost per ticket vs the team's best
    "impact": 0.10,         # post-ship ratings + regression-free record
}

_DKT_RE = re.compile(r"\bDKT-(\d+)\b", re.I)


def _norm_name(s: str) -> str:
    return (s or "").strip().lower()


def _ts_secs(iso: str) -> Optional[float]:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (ValueError, TypeError):
        return None


def _strengths(t: Dict[str, Any]) -> List[str]:
    """What a well-written ask did right — chips for the 'best asks' showcase.
    Mirrors the score_clarity components so praise matches the scoring."""
    s = []
    if len((t.get("acceptance_criteria") or "").strip()) >= 15:
        s.append("acceptance criteria")
    if len((t.get("description") or "").strip()) >= 120:
        s.append("detailed description")
    blob = ((t.get("description") or "") + " " + (t.get("acceptance_criteria") or "")).lower()
    if ("/" in blob or re.search(r"\b(when|should|so that|step|expected|click|open|see|"
                                 r"returns?|display|shows?)\b", blob) or re.search(r"\d", blob)):
        s.append("concrete behaviour")
    title = (t.get("title") or "").strip()
    if len(title) >= 10 and not (set(title.lower().split()) & _VAGUE_WORDS):
        s.append("specific title")
    return s


def profiles(testers: List[Dict[str, str]]) -> Dict[str, Any]:
    """Full gamified scorecard for every tester, plus a team hall of fame.

    `testers` = [{"username", "name"}, ...] from testing_auth (authorship in the
    DB is by display name with inconsistent casing, so both forms alias to the
    username here).
    """
    conn = _connect()
    try:
        tickets = [dict(r) for r in conn.execute("SELECT * FROM tickets ORDER BY id")]
        events = [dict(r) for r in conn.execute("SELECT * FROM ticket_events ORDER BY id")]
    finally:
        conn.close()
    for e in events:
        p = e.get("payload")
        if p:
            try:
                e["payload"] = json.loads(p)
            except (ValueError, TypeError):
                e["payload"] = None
        else:
            e["payload"] = None

    tk = {t["id"]: t for t in tickets}
    evs_by_ticket: Dict[int, List[dict]] = {}
    for e in events:
        evs_by_ticket.setdefault(e["ticket_id"], []).append(e)

    alias: Dict[str, str] = {}
    display: Dict[str, str] = {}
    for tt in testers:
        display[tt["username"]] = tt["name"]
        for k in {_norm_name(tt["username"]), _norm_name(tt["name"])}:
            if k:
                alias[k] = tt["username"]

    def owner(t: dict) -> Optional[str]:
        return alias.get(_norm_name(t.get("created_by")))

    # ---- per-ticket facts -------------------------------------------------
    facts: Dict[int, dict] = {}
    for t in tickets:
        evs = evs_by_ticket.get(t["id"], [])
        trans = [e for e in evs if e["kind"] == "transition"]
        secs = cost = 0.0
        has_eff = False
        for e in evs:
            p = e["payload"]
            if isinstance(p, dict):
                if p.get("duration_secs"):
                    secs += float(p["duration_secs"]); has_eff = True
                if p.get("cost_usd"):
                    cost += float(p["cost_usd"]); has_eff = True
        ratings: Dict[str, dict] = {}   # latest impact rating per rater
        for e in evs:
            p = e["payload"]
            if e["kind"] == "impact" and isinstance(p, dict) and p.get("rating"):
                ratings[_norm_name(e["actor"])] = {
                    "rating": int(p["rating"]), "note": p.get("note", ""), "ts": e["ts"]}
        facts[t["id"]] = {
            "queued": any(e["phase"] == "queued" for e in trans),
            "bounced": sum(1 for e in trans if e["phase"] == "needs_info"),
            "changes": sum(1 for e in trans if e["phase"] == "changes_requested"),
            "done_ts": next((e["ts"] for e in trans if e["phase"] == "done"), None),
            "secs": secs, "cost": cost, "has_eff": has_eff,
            "ratings": ratings,
        }

    # ---- regressions: CONFIRMED relatedness links against a shipped ticket
    # (mention / agent / human-confirmed; suspected similarity never penalises).
    conn = _connect()
    try:
        link_rows = [dict(r) for r in conn.execute("SELECT * FROM ticket_links")]
    finally:
        conn.close()
    regressions: Dict[int, List[str]] = {}
    suspected: Dict[int, int] = {}
    for ln in link_rows:
        if ln["status"] == "confirmed":
            regressions.setdefault(ln["target_id"], []).append(ticket_ref(ln["ticket_id"]))
        elif ln["status"] == "suspected":
            suspected[ln["target_id"]] = suspected.get(ln["target_id"], 0) + 1

    # ---- real platform performance per shipped ticket (telemetry join)
    perf: Dict[int, Optional[dict]] = {}
    for t in tickets:
        if facts[t["id"]]["done_ts"]:
            try:
                perf[t["id"]] = platform_performance(t, facts[t["id"]]["done_ts"])
            except Exception:
                perf[t["id"]] = None

    # ---- review responsiveness: time from entering user_review to a human
    # moving it out (done / queued / discussion), credited to that human.
    resp_hours: Dict[str, List[float]] = {}
    for tid, evs in evs_by_ticket.items():
        trans = [e for e in evs if e["kind"] == "transition"]
        for prev, curr in zip(trans, trans[1:]):
            if prev["phase"] == "user_review" and curr["phase"] in ("done", "queued", "discussion"):
                who = alias.get(_norm_name(curr["actor"]))
                a, b = _ts_secs(prev["ts"]), _ts_secs(curr["ts"])
                if who and a and b and b >= a:
                    resp_hours.setdefault(who, []).append((b - a) / 3600.0)

    # ---- assists: comments on someone ELSE's ticket; "helped" = the ticket
    # subsequently moved forward (queued or done after the comment).
    comments_on_others: Dict[str, int] = {}
    assists: Dict[str, int] = {}
    assist_feed: Dict[str, List[dict]] = {}
    for tid, evs in evs_by_ticket.items():
        t = tk[tid]
        t_owner = owner(t)
        trans = [e for e in evs if e["kind"] == "transition"]
        for e in evs:
            if e["kind"] != "comment":
                continue
            who = alias.get(_norm_name(e["actor"]))
            if not who or who == t_owner:
                continue
            helped = any(tr["id"] > e["id"] and tr["phase"] in ("queued", "done")
                         for tr in trans)
            comments_on_others[who] = comments_on_others.get(who, 0) + 1
            if helped:
                assists[who] = assists.get(who, 0) + 1
            assist_feed.setdefault(who, []).append({
                "ref": ticket_ref(tid), "title": t["title"], "ts": e["ts"],
                "snippet": str(e["summary"])[:140], "helped": helped})

    # ---- per-tester rollup --------------------------------------------------
    raw: Dict[str, dict] = {}
    for tt in testers:
        u = tt["username"]
        mine = [t for t in tickets if owner(t) == u]
        scored = [t for t in mine if int(t.get("clarity_score") or 0) > 0]
        done = [t for t in mine if facts[t["id"]]["done_ts"]]
        processed = [t for t in mine if facts[t["id"]]["queued"]]
        eff = [t for t in mine if facts[t["id"]]["has_eff"] and facts[t["id"]]["cost"] > 0]

        ftt = [t for t in done
               if int(t.get("iteration") or 0) == 0
               and facts[t["id"]]["bounced"] == 0 and facts[t["id"]]["changes"] == 0]

        series = [{"ref": ticket_ref(t["id"]), "score": int(t["clarity_score"])}
                  for t in scored]
        trend = None
        if len(series) >= 6:
            first3 = [p["score"] for p in series[:3]]
            last3 = [p["score"] for p in series[-3:]]
            trend = round(sum(last3) / 3 - sum(first3) / 3)

        cycles = []
        for t in done:
            a, b = _ts_secs(t["created_at"]), _ts_secs(facts[t["id"]]["done_ts"])
            if a and b and b >= a:
                cycles.append(b - a)

        all_ratings = [r["rating"] for t in mine
                       for r in facts[t["id"]]["ratings"].values()]
        # Unhealthy = a confirmed follow-up exists OR telemetry shows the
        # touched routes erroring above their pre-ship baseline.
        regressed = [t for t in done
                     if regressions.get(t["id"])
                     or (perf.get(t["id"]) or {}).get("verdict") == "degraded"]

        rh = resp_hours.get(u, [])
        raw[u] = {
            "username": u, "name": display[u],
            "tickets": len(mine), "processed": len(processed), "done": len(done),
            "done_bugs": sum(1 for t in done if t["type"] == "bug"),
            "n_scored": len(scored),
            "avg_clarity": round(sum(int(t["clarity_score"]) for t in scored) / len(scored)) if scored else None,
            "n_clear80": sum(1 for t in scored if int(t["clarity_score"]) >= 80),
            "trend": trend, "series": series[-30:],
            "ftt_count": len(ftt),
            "ftt_rate": round(len(ftt) / len(done) * 100) if done else None,
            "bounced": sum(facts[t["id"]]["bounced"] for t in mine),
            "resubmits": sum(int(t.get("iteration") or 0) for t in mine),
            "assists": assists.get(u, 0),
            "comments_on_others": comments_on_others.get(u, 0),
            "n_resp": len(rh),
            "avg_resp_h": round(sum(rh) / len(rh), 1) if rh else None,
            "n_eff": len(eff),
            "avg_cost": round(sum(facts[t["id"]]["cost"] for t in eff) / len(eff), 2) if eff else None,
            "avg_secs": round(sum(facts[t["id"]]["secs"] for t in eff) / len(eff)) if eff else None,
            "avg_cycle_secs": round(sum(cycles) / len(cycles)) if cycles else None,
            "rating_avg": round(sum(all_ratings) / len(all_ratings), 1) if all_ratings else None,
            "rating_n": len(all_ratings),
            "healthy_done": len(done) - len(regressed),
            "regressed_done": len(regressed),
            "_mine": mine, "_scored": scored, "_done": done, "_eff": eff,
        }

    # ---- team anchors for the relative dimensions ---------------------------
    max_assists = max([d["assists"] for d in raw.values()] or [0])
    costs = [d["avg_cost"] for d in raw.values() if d["avg_cost"]]
    best_cost = min(costs) if costs else None
    median_cost = round(statistics.median(costs), 2) if costs else None

    # ---- dimensions + composite + badges ------------------------------------
    out_profiles = []
    for u, d in raw.items():
        dims: Dict[str, Optional[int]] = {
            "clarity": d["avg_clarity"],
            "first_time": d["ftt_rate"],
            "helpfulness": (min(100, round(100 * d["assists"] / max_assists))
                            if max_assists and (d["assists"] or d["tickets"] or d["comments_on_others"]) else
                            (0 if max_assists else None)),
            "responsiveness": (max(0, round(100 * (1 - d["avg_resp_h"] / 72)))
                               if d["avg_resp_h"] is not None else None),
            "efficiency": (min(100, round(100 * best_cost / d["avg_cost"]))
                           if d["avg_cost"] and best_cost else None),
        }
        impact_parts = []
        if d["rating_avg"] is not None:
            impact_parts.append((d["rating_avg"] - 1) / 4 * 100)
        if d["done"]:
            impact_parts.append(d["healthy_done"] / d["done"] * 100)
        dims["impact"] = round(sum(impact_parts) / len(impact_parts)) if impact_parts else None

        avail = {k: v for k, v in dims.items() if v is not None}
        score = (round(sum(v * SCORE_WEIGHTS[k] for k, v in avail.items())
                       / sum(SCORE_WEIGHTS[k] for k in avail)) if avail else None)

        badges = []
        def _badge(bid, name, emoji, desc, n, target, hint=""):
            badges.append({"id": bid, "name": name, "emoji": emoji, "desc": desc,
                           "earned": n >= target, "n": n, "target": target,
                           "progress": min(1.0, n / target if target else 0.0),
                           "hint": hint})
        _badge("first_ship", "Shipped It", "🚀", "Got an ask all the way to Done",
               d["done"], 1, "Take one ticket through user review")
        _badge("crystal_clear", "Crystal Clear", "💎", "3 asks scored 80+ for clarity",
               d["n_clear80"], 3, "Acceptance criteria + concrete detail push scores up")
        _badge("one_shot", "One-Shot", "🎯", "3 tickets shipped with no bounces or retries",
               d["ftt_count"], 3, "Clear asks sail through first time")
        _badge("good_neighbour", "Good Neighbour", "🤝",
               "Helped 5 of other people's tickets move forward",
               d["assists"], 5, "Comment with repro steps or answers on others' tickets")
        _badge("bug_spotter", "Bug Spotter", "🐛", "5 bug reports shipped",
               d["done_bugs"], 5, "")
        _badge("on_the_up", "On the Up", "📈", "Clarity trending up 10+ points",
               1 if (d["trend"] is not None and d["trend"] >= 10) else 0, 1,
               "Your last few asks vs your first few")
        _badge("quick_draw", "Quick on the Draw", "⚡",
               "Tests what's sent to you within a day (3+ reviews)",
               1 if (d["n_resp"] >= 3 and d["avg_resp_h"] is not None
                     and d["avg_resp_h"] < 24) else 0, 1, "")
        _badge("lean_asker", "Lean Asker", "🪙",
               "Average agent cost at or below the team median (3+ worked tickets)",
               1 if (d["n_eff"] >= 3 and median_cost is not None
                     and d["avg_cost"] is not None and d["avg_cost"] <= median_cost)
               else 0, 1, "Clear, scoped asks burn fewer tokens")

        # Showcases: best vs needs-work, each with the downstream story.
        def _example(t, with_suggestions=False):
            f = facts[t["id"]]
            ex = {"ref": ticket_ref(t["id"]), "id": t["id"], "title": t["title"],
                  "score": int(t["clarity_score"]), "status": t["status"],
                  "iterations": int(t.get("iteration") or 0),
                  "bounced": f["bounced"],
                  "cost": round(f["cost"], 2) if f["has_eff"] else None,
                  "secs": round(f["secs"]) if f["has_eff"] else None}
            if with_suggestions:
                ex["suggestions"] = score_clarity(
                    t["title"], t["description"], t["acceptance_criteria"],
                    t["type"])["suggestions"]
            else:
                ex["strengths"] = _strengths(t)
            return ex

        by_score = sorted(d["_scored"], key=lambda t: -int(t["clarity_score"]))
        best = [_example(t) for t in by_score[:3] if int(t["clarity_score"]) >= 60]
        best_ids = {e["id"] for e in best}
        worst = [_example(t, with_suggestions=True)
                 for t in reversed(by_score[-3:])
                 if int(t["clarity_score"]) < 70 and t["id"] not in best_ids]

        # Their tickets' clear-vs-unclear agent cost — the "clarity pays" stat.
        clear = [t for t in d["_eff"] if int(t.get("clarity_score") or 0) >= 70]
        unclear = [t for t in d["_eff"] if int(t.get("clarity_score") or 0) < 70]
        cvu = None
        if clear and unclear:
            cvu = {
                "clear": {"n": len(clear),
                          "avg_cost": round(sum(facts[t["id"]]["cost"] for t in clear) / len(clear), 2),
                          "avg_secs": round(sum(facts[t["id"]]["secs"] for t in clear) / len(clear))},
                "unclear": {"n": len(unclear),
                            "avg_cost": round(sum(facts[t["id"]]["cost"] for t in unclear) / len(unclear), 2),
                            "avg_secs": round(sum(facts[t["id"]]["secs"] for t in unclear) / len(unclear))},
            }

        shipped = sorted(
            [{"ref": ticket_ref(t["id"]), "id": t["id"], "title": t["title"],
              "done_ts": facts[t["id"]]["done_ts"],
              "rating_avg": (round(sum(r["rating"] for r in facts[t["id"]]["ratings"].values())
                                   / len(facts[t["id"]]["ratings"]), 1)
                             if facts[t["id"]]["ratings"] else None),
              "rating_n": len(facts[t["id"]]["ratings"]),
              "regressions": regressions.get(t["id"], []),
              "suspected": suspected.get(t["id"], 0),
              "perf": perf.get(t["id"])}
             for t in d["_done"]],
            key=lambda x: x["done_ts"] or "", reverse=True)

        # Full ticket history with platform performance — the profile's record
        # of every ask and how it actually fared once shipped.
        history = sorted(
            [{"ref": ticket_ref(t["id"]), "id": t["id"], "title": t["title"],
              "type": t["type"], "status": t["status"],
              "clarity": int(t.get("clarity_score") or 0) or None,
              "iterations": int(t.get("iteration") or 0),
              "bounced": facts[t["id"]]["bounced"],
              "cost": round(facts[t["id"]]["cost"], 2) if facts[t["id"]]["has_eff"] else None,
              "created_at": t["created_at"],
              "done_ts": facts[t["id"]]["done_ts"],
              "regressions": regressions.get(t["id"], []),
              "suspected": suspected.get(t["id"], 0),
              "rating_avg": (round(sum(r["rating"] for r in facts[t["id"]]["ratings"].values())
                                   / len(facts[t["id"]]["ratings"]), 1)
                             if facts[t["id"]]["ratings"] else None),
              "perf": perf.get(t["id"]) if facts[t["id"]]["done_ts"] else None}
             for t in d["_mine"]],
            key=lambda x: x["created_at"], reverse=True)[:50]

        stats = {k: v for k, v in d.items() if not k.startswith("_") and k != "series"}
        out_profiles.append({
            "username": u, "name": d["name"], "score": score, "dims": dims,
            "badges": badges, "badges_earned": sum(1 for b in badges if b["earned"]),
            "stats": stats, "clarity_series": d["series"],
            "best": best, "worst": worst, "clear_vs_unclear": cvu,
            "assist_feed": sorted(assist_feed.get(u, []),
                                  key=lambda x: x["ts"], reverse=True)[:8],
            "shipped": shipped,
            "history": history,
        })

    out_profiles.sort(key=lambda p: (p["score"] is None, -(p["score"] or 0)))
    rank = 0
    for p in out_profiles:
        if p["score"] is not None:
            rank += 1
            p["rank"] = rank
        else:
            p["rank"] = None

    # ---- team hall of fame: what a good ask looks like ----------------------
    candidates = [t for t in tickets
                  if facts[t["id"]]["queued"] and int(t.get("clarity_score") or 0) > 0]
    candidates.sort(key=lambda t: (-int(t["clarity_score"]),
                                   int(t.get("iteration") or 0),
                                   facts[t["id"]]["bounced"],
                                   facts[t["id"]]["cost"]))
    hall = [{"ref": ticket_ref(t["id"]), "id": t["id"], "title": t["title"],
             "author": t.get("created_by") or "—",
             "score": int(t["clarity_score"]), "status": t["status"],
             "iterations": int(t.get("iteration") or 0),
             "cost": round(facts[t["id"]]["cost"], 2) if facts[t["id"]]["has_eff"] else None,
             "strengths": _strengths(t)}
            for t in candidates[:3]]

    return {"weights": SCORE_WEIGHTS, "profiles": out_profiles,
            "hall_of_fame": hall,
            "team": {"median_cost": median_cost, "best_cost": best_cost,
                     "max_assists": max_assists}}


# ---------------------------------------------------------------------------
# Notifications (queued here; drained by the notifier service later)
# ---------------------------------------------------------------------------

def enqueue_notification(
    ticket_id: int, recipient: str, event: str,
    subject: str = "", body: str = "", channel: str = "email",
) -> Dict[str, Any]:
    if event not in VALID_NOTIFY_EVENTS:
        raise ValueError(f"event must be one of {VALID_NOTIFY_EVENTS}")
    now = utcnow_iso()
    conn = _connect()
    try:
        cur = conn.execute(
            """INSERT INTO notifications
               (ticket_id, recipient, channel, event, subject, body, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (ticket_id, recipient, channel, event, subject, body, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM notifications WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def _user_test_lead() -> str:
    """Username always looped in on user_review — runs hands-on user testing, so
    they get a heads-up on every ship regardless of who created/owns the ticket.
    Falls back to the first configured tester."""
    lead = (CONFIG.user_test_lead or "").strip().lower()
    if lead:
        return lead
    return CONFIG.testers[0]["username"].lower() if CONFIG.testers else ""


def _default_recipient() -> str:
    """Fallback notification recipient when a ticket has no assignee/creator."""
    rec = (CONFIG.default_recipient or "").strip().lower()
    if rec:
        return rec
    return CONFIG.testers[0]["username"].lower() if CONFIG.testers else ""


def enqueue_user_review_notification(ticket: Dict[str, Any]) -> None:
    """Tell the assignee (creator if unassigned) AND the user-test lead that a
    ticket is ready to test. Deduped by resolved identity so nobody gets two
    copies. Shared by the /transition route and the agent's merge reconciler."""
    primary = ticket.get("assignee") or ticket.get("created_by") or _default_recipient()
    recipients: List[str] = []
    seen: set = set()
    for r in (primary, _user_test_lead()):
        key = (r or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            recipients.append(r)
    ref = ticket.get("ref") or ticket_ref(ticket["id"])
    for recipient in recipients:
        enqueue_notification(
            ticket["id"], recipient, "user_review",
            subject=f"Docket {ref}: ready for you to test",
            body=(f"{ticket['title']}\n\nOpen the ticket and follow the test "
                  f"instructions, then mark It works / Send back:\n"
                  f"{CONFIG.base_url}/docket"),
        )


def pending_notifications() -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM notifications WHERE status='pending' ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def mark_notification(notif_id: int, status: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE notifications SET status=?, sent_at=? WHERE id=?",
            (status, utcnow_iso() if status == "sent" else "", notif_id),
        )
        conn.commit()
    finally:
        conn.close()
