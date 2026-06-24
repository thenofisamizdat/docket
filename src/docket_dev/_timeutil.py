"""Timezone-correct ISO timestamp helpers.

Historically the backend wrote `datetime.now().isoformat()` — a NAIVE
local-wall-clock string with no timezone marker. The server runs in UTC,
so the instant was right, but JavaScript's `new Date(s)` parses a naive
ISO string as *browser-local* time. On a UTC+N browser every "heartbeat
age" therefore read N hours stale (observed 2026-05-23: a task updated
21s earlier showed "1h ago / STALLED" on a UK browser).

`utcnow_iso()` emits an offset-aware UTC timestamp (`...+00:00`) that both
JS and `datetime.fromisoformat` interpret unambiguously. `parse_iso_utc()`
is the tolerant reader: it coerces any value — naive (legacy rows) or
offset-aware (new rows), with or without a trailing `Z` — to an aware UTC
datetime so arithmetic against `utcnow()` never raises the
"can't subtract offset-naive and offset-aware" TypeError.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def utcnow() -> datetime:
    """Current time as an offset-aware UTC datetime."""
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    """Current UTC time as an offset-aware ISO 8601 string (`...+00:00`)."""
    return datetime.now(timezone.utc).isoformat()


def parse_iso_utc(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO string to an aware UTC datetime, or None if unparseable.

    Naive values (legacy timestamps with no tz) are assumed to be UTC,
    matching how the server wrote them.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
