"""
Docket router — the ticket pipeline API (`/api/tickets/*`).

Docket is the testing hub grown into a real ticket lifecycle + autonomous-dev
pipeline with a visible production line. This router is the read/write surface
the standalone Docket app talks to: raise tickets, move them through the
lifecycle (guarded by the state machine in `services.docket_storage`), comment,
and read the queue + per-ticket timeline.

Auth reuses the hub-scoped tester login (neil / alex / conor / arturo — see
services/testing_auth.py); we import `require_tester` from the testing router so
there's a single source of truth for "who is signed in". Every mutation is
attributed to the verified tester (the client can't spoof authorship).

Lifecycle moves are NOT free-form: `/transition` and `/submit` defer to
docket_storage.transition(), which rejects any illegal state change.

Endpoints:
    GET  /api/tickets/meta             → vocabulary (statuses/priorities/types/transitions)
    GET  /api/tickets/testers          → testers (for assignee/creator pickers)
    GET  /api/tickets                  → list tickets (optional ?status=)
    GET  /api/tickets/queue            → the queue in pick-up order, with positions
    GET  /api/tickets/board            → everything the board needs in one call
    POST /api/tickets                  → raise a ticket (lands in Discussion)
    GET  /api/tickets/{id}             → ticket + timeline + activity + queue position
    PATCH/api/tickets/{id}             → edit fields (title/desc/priority/...; NOT status)
    POST /api/tickets/{id}/submit      → submit for processing (Discussion → Queued)
    POST /api/tickets/{id}/transition  → guarded lifecycle move
    POST /api/tickets/{id}/comment     → add a comment to the timeline
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from docket_dev import storage as dk
from docket_dev import auth as testing_auth
from docket_dev.auth import require_tester

router = APIRouter(prefix="/api/tickets", tags=["docket"])


# ---- vocabulary (so the frontend renders the board generically) ----

@router.get("/meta")
def get_meta(tester: dict = Depends(require_tester)):
    """The lifecycle vocabulary the UI needs to render lanes/transitions."""
    return {
        "statuses": list(dk.STATUSES),
        "status_meta": dk.STATUS_META,
        "main_line": list(dk.MAIN_LINE),
        "priorities": list(dk.PRIORITIES),
        "default_priority": dk.DEFAULT_PRIORITY,
        "types": list(dk.TICKET_TYPES),
        # Legal next-moves per status (sets aren't JSON-serialisable → lists).
        "transitions": {k: sorted(v) for k, v in dk.TRANSITIONS.items()},
    }


@router.get("/testers")
def get_testers(tester: dict = Depends(require_tester)):
    """Testers for assignee/creator pickers (username + display name)."""
    return {"testers": [
        {"username": t["username"], "name": t["name"]}
        for t in testing_auth.all_testers()
    ]}


# ---- listing / queue / board ----

@router.get("")
def list_tickets(status: Optional[str] = None, tester: dict = Depends(require_tester)):
    """All tickets, optionally filtered by status."""
    if status and status not in dk.STATUSES:
        raise HTTPException(status_code=400, detail=f"unknown status '{status}'")
    return {"tickets": dk.list_tickets(status=status)}


@router.get("/queue")
def get_queue(tester: dict = Depends(require_tester)):
    """The processing queue in pick-up order, each with a 1-based position."""
    return {"queue": dk.queue()}


@router.get("/analytics")
def get_analytics(tester: dict = Depends(require_tester)):
    """Coaching dashboard metrics (throughput, effort, quality, per-tester, clarity)."""
    return dk.analytics()


@router.get("/profiles")
def get_profiles(tester: dict = Depends(require_tester)):
    """Gamified per-tester scorecards: Docket Score, dimensions, badges, best/
    worst ask showcases, assists, and post-ship impact — plus a hall of fame."""
    return dk.profiles([
        {"username": t["username"], "name": t["name"]}
        for t in testing_auth.all_testers()
    ])


class ClarityIn(BaseModel):
    title: str = ""
    description: str = ""
    acceptance_criteria: str = ""
    type: str = "feature"


@router.post("/clarity")
def clarity_preview(body: ClarityIn, tester: dict = Depends(require_tester)):
    """Score an in-progress ask for clarity (live meter in the New Ticket form)."""
    return dk.score_clarity(body.title, body.description, body.acceptance_criteria, body.type)


@router.get("/board")
def get_board(tester: dict = Depends(require_tester)):
    """One-shot payload for the production-line board: every ticket plus its
    live activity, grouped client-side by status. Queue positions are merged
    onto the queued tickets so the UI can show 'Position #N' without a 2nd call.
    """
    tickets = dk.list_tickets()
    positions = {t["id"]: t["position"] for t in dk.queue()}
    efforts = dk.effort_by_ticket()
    for t in tickets:
        t["position"] = positions.get(t["id"])
        act = dk.current_activity(t["id"])
        t["current_activity"] = act["summary"] if act else ""
        t["effort"] = efforts.get(t["id"])
    return {"tickets": tickets, "status_meta": dk.STATUS_META,
            "main_line": list(dk.MAIN_LINE)}


# ---- create ----

class TicketIn(BaseModel):
    title: str
    type: str = "feature"
    description: str = ""
    acceptance_criteria: str = ""
    priority: str = dk.DEFAULT_PRIORITY
    seed_user_item_id: str = ""


@router.post("")
def create_ticket(body: TicketIn, tester: dict = Depends(require_tester)):
    """Raise a new ticket. It lands in the Discussion zone."""
    try:
        t = dk.create_ticket(
            title=body.title,
            type=body.type,
            description=body.description,
            acceptance_criteria=body.acceptance_criteria,
            priority=body.priority,
            created_by=tester.get("name", ""),
            seed_user_item_id=body.seed_user_item_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ticket": t}


class BulkIn(BaseModel):
    tickets: List[TicketIn]


@router.post("/bulk")
def bulk_create(body: BulkIn, tester: dict = Depends(require_tester)):
    """Create many tickets at once (bulk upload). Bad rows are skipped and
    reported rather than failing the whole batch."""
    created, errors = [], []
    for i, row in enumerate(body.tickets):
        if not (row.title or "").strip():
            errors.append({"row": i + 1, "error": "title is required"})
            continue
        try:
            t = dk.create_ticket(
                title=row.title, type=row.type, description=row.description,
                acceptance_criteria=row.acceptance_criteria, priority=row.priority,
                created_by=tester.get("name", ""))
            created.append({"ref": t["ref"], "id": t["id"], "title": t["title"]})
        except ValueError as e:
            errors.append({"row": i + 1, "title": row.title, "error": str(e)})
    return {"created": created, "errors": errors, "count": len(created)}


# ---- single ticket ----

def _detail(ticket_id: int) -> dict:
    t = dk.get_ticket(ticket_id)
    if not t:
        raise HTTPException(status_code=404, detail=f"ticket {ticket_id} not found")
    t["events"] = dk.get_events(ticket_id)
    act = dk.current_activity(ticket_id)
    t["current_activity"] = act["summary"] if act else ""
    t["position"] = dk.queue_position(ticket_id)
    t["links"] = dk.links_for(ticket_id)
    t["perf"] = dk.platform_performance(t) if t["status"] == "done" else None
    return t


@router.get("/{ticket_id}")
def get_ticket(ticket_id: int, tester: dict = Depends(require_tester)):
    """A ticket with its full timeline, live activity, and queue position."""
    return {"ticket": _detail(ticket_id)}


class TicketPatch(BaseModel):
    title: Optional[str] = None
    type: Optional[str] = None
    description: Optional[str] = None
    acceptance_criteria: Optional[str] = None
    priority: Optional[str] = None
    test_instructions: Optional[str] = None
    assignee: Optional[str] = None


@router.patch("/{ticket_id}")
def patch_ticket(ticket_id: int, body: TicketPatch, tester: dict = Depends(require_tester)):
    """Edit ticket fields. Lifecycle status is NOT editable here (use /transition)."""
    if not dk.get_ticket(ticket_id):
        raise HTTPException(status_code=404, detail=f"ticket {ticket_id} not found")
    fields = {k: v for k, v in body.dict().items() if v is not None}
    try:
        dk.update_ticket(ticket_id, **fields)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ticket": _detail(ticket_id)}


# ---- lifecycle moves ----

class SubmitIn(BaseModel):
    priority: Optional[str] = None
    note: str = ""


@router.post("/{ticket_id}/submit")
def submit_for_processing(ticket_id: int, body: SubmitIn,
                          tester: dict = Depends(require_tester)):
    """Submit for processing: Discussion → Queued. Optionally set priority first.
    An explicit Submit is an automation opt-in, so it sets dev_optin."""
    if body.priority:
        dk.update_ticket(ticket_id, priority=body.priority)
    dk.update_ticket(ticket_id, dev_optin=1)   # explicit opt-in to automation
    try:
        dk.transition(ticket_id, "queued", actor=tester.get("name", ""),
                      summary=body.note or "Submitted for processing")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ticket": _detail(ticket_id)}


@router.post("/build/run")
def run_full_build(tester: dict = Depends(require_tester)):
    """Run Full Build: submit every Discussion ticket into the queue IN BUILD ORDER
    (build_seq, then id). Each entry to 'queued' stamps an increasing queue_seq, so
    the agent works them through the pipeline in build order — greenfield projects
    assemble on the base branch ticket-by-ticket. On a `/build/…` path so it never
    collides with the `/{ticket_id}` routes.

    SAFETY: only queues tickets with dev_optin=1 (greenfield grooming / explicit
    handoff). A ticket a human is working by hand on the roadmap is NEVER swept in."""
    backlog = [t for t in dk.list_tickets("discussion") if t.get("dev_optin")]
    skipped = len(dk.list_tickets("discussion")) - len(backlog)
    backlog.sort(key=lambda t: (t.get("build_seq") if t.get("build_seq") is not None
                                else 10_000, t["id"]))
    queued = 0
    for t in backlog:
        try:
            dk.transition(t["id"], "queued", actor=tester.get("name", ""),
                          summary="Run Full Build")
            queued += 1
        except ValueError:
            continue  # e.g. a ticket that can't currently be queued — skip it
    return {"queued": queued, "eligible": len(backlog), "skipped_manual": skipped}


class TransitionIn(BaseModel):
    to_status: str
    summary: str = ""


@router.post("/{ticket_id}/transition")
def transition_ticket(ticket_id: int, body: TransitionIn,
                      tester: dict = Depends(require_tester)):
    """Guarded lifecycle move. The state machine rejects illegal transitions."""
    try:
        dk.transition(ticket_id, body.to_status, actor=tester.get("name", ""),
                      summary=body.summary)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # "Ready for you to test" — notify the assignee (creator if unassigned) plus
    # the user-test lead. Shared with the agent's merge reconciler.
    if body.to_status == "user_review":
        dk.enqueue_user_review_notification(dk.get_ticket(ticket_id))
    # Pipeline completion write-back (covers the PR/human "→ done" path): record
    # effort + a "Done by Docket pipeline" note, but only for tickets the pipeline
    # actually built (record_pipeline_done no-ops without agent run-events).
    if body.to_status == "done":
        try:
            from docket_dev import roadmap as rm
            rm.record_pipeline_done(ticket_id)
        except Exception:
            pass
    return {"ticket": _detail(ticket_id)}


class ResubmitIn(BaseModel):
    reason: str                                # required: what's still wrong / what changed
    priority: Optional[str] = None
    description: Optional[str] = None
    acceptance_criteria: Optional[str] = None
    test_instructions: Optional[str] = None


@router.post("/{ticket_id}/resubmit")
def resubmit(ticket_id: int, body: ResubmitIn, tester: dict = Depends(require_tester)):
    """Amend the ask and put it back in the queue (the resubmit loop).

    Used when a ticket fails User Review (or comes back as Needs Info / Stalled):
    gather the reason + any edits to the ask, record them on the timeline, then
    transition → queued. The state machine bumps `iteration` on the
    user_review→queued bounce, so the board shows how many rounds it's taken.
    """
    if not body.reason.strip():
        raise HTTPException(status_code=400, detail="a reason for resubmitting is required")
    if not dk.get_ticket(ticket_id):
        raise HTTPException(status_code=404, detail=f"ticket {ticket_id} not found")

    # Apply any edits to the ask first.
    edits = {k: v for k, v in {
        "priority": body.priority,
        "description": body.description,
        "acceptance_criteria": body.acceptance_criteria,
        "test_instructions": body.test_instructions,
    }.items() if v is not None}
    if edits:
        dk.update_ticket(ticket_id, **edits)

    # Record the resubmit reason on the timeline so the agent + everyone sees it.
    dk.add_event(ticket_id, "comment", summary=f"Resubmitted: {body.reason.strip()}",
                 actor=tester.get("name", ""))

    try:
        dk.transition(ticket_id, "queued", actor=tester.get("name", ""),
                      summary="Resubmitted for processing")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ticket": _detail(ticket_id)}


class LinkResolveIn(BaseModel):
    action: str   # 'confirm' (yes, the old fix didn't stick) | 'dismiss'


@router.post("/{ticket_id}/links/{link_id}/resolve")
def resolve_link(ticket_id: int, link_id: int, body: LinkResolveIn,
                 tester: dict = Depends(require_tester)):
    """Human verdict on a suspected relatedness link. Confirming means the
    shipped ticket's solution wasn't satisfactory — it starts counting against
    that ticket's post-ship health."""
    if not dk.get_ticket(ticket_id):
        raise HTTPException(status_code=404, detail=f"ticket {ticket_id} not found")
    try:
        ln = dk.resolve_link(link_id, body.action, actor=tester.get("name", ""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if ln["status"] == "confirmed":
        dk.add_event(ticket_id, "note", actor=tester.get("name", ""),
                     summary=f"Confirmed as a follow-up of shipped "
                             f"{dk.ticket_ref(ln['target_id'])} — its fix didn't "
                             f"fully solve this.")
    return {"link": ln, "ticket": _detail(ticket_id)}


class ImpactIn(BaseModel):
    rating: int            # 1 (not useful) … 5 (big win)
    note: str = ""


@router.post("/{ticket_id}/impact")
def rate_impact(ticket_id: int, body: ImpactIn, tester: dict = Depends(require_tester)):
    """Post-ship impact rating on a Done ticket (1-5 stars + optional note).
    One rating per tester per ticket — re-rating replaces your earlier one
    (the profile maths keeps only the latest per rater)."""
    if not 1 <= body.rating <= 5:
        raise HTTPException(status_code=400, detail="rating must be 1-5")
    t = dk.get_ticket(ticket_id)
    if not t:
        raise HTTPException(status_code=404, detail=f"ticket {ticket_id} not found")
    if t["status"] != "done":
        raise HTTPException(status_code=400, detail="impact can only be rated on Done tickets")
    note = body.note.strip()[:500]
    stars = "★" * body.rating + "☆" * (5 - body.rating)
    ev = dk.add_event(
        ticket_id, "impact", actor=tester.get("name", ""),
        summary=f"Rated impact {stars} ({body.rating}/5)" + (f" — {note}" if note else ""),
        payload={"rating": body.rating, "note": note},
    )
    return {"event": ev}


class CommentIn(BaseModel):
    text: str


@router.post("/{ticket_id}/comment")
def add_comment(ticket_id: int, body: CommentIn, tester: dict = Depends(require_tester)):
    """Append a comment to the ticket's timeline."""
    if not body.text.strip():
        raise HTTPException(status_code=400, detail="comment text is required")
    if not dk.get_ticket(ticket_id):
        raise HTTPException(status_code=404, detail=f"ticket {ticket_id} not found")
    ev = dk.add_event(ticket_id, "comment", summary=body.text.strip(),
                      actor=tester.get("name", ""))
    return {"event": ev}
