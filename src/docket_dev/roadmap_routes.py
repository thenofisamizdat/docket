"""Roadmap router — the waterfall-cycle API (`/api/roadmap/*`).

A deliberately small surface over roadmap.py. Kept on its own prefix (rather
than under `/api/tickets`) so it can never collide with the `/{ticket_id}`
path-parameter routes in routes.py.

Endpoints:
    GET   /api/roadmap                → the whole board: cycle, lanes, hours
                                        counter, burndown series
    POST  /api/roadmap/cycle          → start a (new) cycle {name, start_date, weeks}
    PATCH /api/roadmap/tickets/{id}   → set week_lane / estimate_hours /
                                        remaining_hours (bumps are detected here)
    POST  /api/roadmap/rollover       → bump all unfinished tickets out of
                                        expired weeks into the current week

Auth is the same tester login as everything else; every mutation is attributed
to the verified tester.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from docket_dev import roadmap as rm
from docket_dev.auth import require_tester

router = APIRouter(prefix="/api/roadmap", tags=["roadmap"])


@router.get("")
def get_board(tester: dict = Depends(require_tester)):
    """One-shot payload for the roadmap page (also refreshes today's snapshot)."""
    return rm.board()


@router.get("/analytics")
def get_analytics(tester: dict = Depends(require_tester)):
    """Schedule/effort analytics: burndown+ideal+projection, scope-creep, spent-vs-
    left, per-week loading, and a schedule-health score."""
    return rm.analytics()


class CycleIn(BaseModel):
    name: str = ""
    start_date: str = ""          # ISO date; defaults to today
    weeks: int = rm.DEFAULT_WEEKS


@router.post("/cycle")
def start_cycle(body: CycleIn, tester: dict = Depends(require_tester)):
    """Start a new cycle. Lanes reset to Backlog; estimates survive."""
    try:
        cycle = rm.create_cycle(body.name, body.start_date, body.weeks)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"cycle": cycle}


class RoadmapPatch(BaseModel):
    # All optional; `week_lane=None` (explicit null) returns a ticket to Backlog.
    week_lane: Optional[int] = None
    estimate_hours: Optional[float] = None
    remaining_hours: Optional[float] = None
    roadmap_status: Optional[str] = None      # backlog|todo|in_progress|done (manual, never automates)
    hours_done: Optional[float] = None        # actual effort logged


@router.patch("/tickets/{ticket_id}")
def patch_roadmap(ticket_id: int, body: RoadmapPatch,
                  tester: dict = Depends(require_tester)):
    """Move a ticket between lanes and/or edit its hours/status. Forward moves
    count as bumps and land on the ticket timeline. Setting `roadmap_status` is a
    plain field write — it NEVER queues the ticket or starts the agent."""
    fields = body.dict(exclude_unset=True)   # distinguishes "absent" from null
    if not fields:
        raise HTTPException(status_code=400, detail="nothing to change")
    try:
        t = rm.set_ticket(ticket_id, actor=tester.get("name", ""), **fields)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ticket": t}


class PipelineIn(BaseModel):
    queue: bool = False      # False = make available (manual Submit); True = queue now


@router.post("/tickets/{ticket_id}/pipeline")
def to_pipeline(ticket_id: int, body: PipelineIn,
                tester: dict = Depends(require_tester)):
    """Explicitly hand a roadmap ticket to the automated Docket pipeline (the ONLY
    way, besides board Submit / greenfield grooming, a ticket becomes automation-
    eligible). `queue=false` makes it available for a manual Submit; `queue=true`
    queues it now for the agent."""
    try:
        t = rm.send_to_pipeline(ticket_id, queue=body.queue, actor=tester.get("name", ""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ticket": t}


class EstimateIn(BaseModel):
    ids: Optional[List[int]] = None      # specific tickets; default = all unestimated


@router.post("/estimate")
def auto_estimate(body: EstimateIn, tester: dict = Depends(require_tester)):
    """Auto-estimate effort hours (via Claude) for the given tickets, or every
    unestimated open ticket if none are given. Writes estimate_hours + a rationale
    note on each. Runs server-side; can take a while for a large batch."""
    from docket_dev import recognize
    try:
        estimated = recognize.estimate_tickets(body.ids)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"estimation failed: {e}")
    return {"estimated": estimated, "count": len(estimated)}


@router.post("/rollover")
def rollover(tester: dict = Depends(require_tester)):
    """Bump everything unfinished in expired weeks into the current week."""
    moved: List[dict] = rm.rollover(actor=tester.get("name", ""))
    return {"moved": moved, "board": rm.board()}
