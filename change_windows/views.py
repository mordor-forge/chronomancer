"""What an owner surface (the Mini App, Hermes Desktop) may read: one projection for both.

Explicit field lists, so a new store column never reaches a client by accident and command
previews never leave the audit table.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import cards as card_reader
from . import overlaps

IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9_-]{16,128}")
WINDOW_ID = re.compile(r"W[0-9A-F]{6}")
ACTIONS = ("approve", "decline", "close")
WINDOW_FIELDS = ("id", "name", "scope", "start_ts", "end_ts", "exclusive", "presence",
                 "state", "kind", "reason", "cards", "checked_in_at", "closed_at", "decided_via")
AUDIT_FIELDS = ("id", "ts", "verdict", "tool", "rule_ids", "scopes", "window_id")
_LISTED_STATES = ("pending", "approved", "closed", "declined", "cancelled")


def guard_mode(setting: Callable[..., Any]) -> str:
    """``enforce``, ``warn`` or ``off``, read the way the guard reads it: the page must not say
    "blocked" when the guard only records. An unknown or unreadable value is ``enforce``, as in the guard."""
    try:
        raw = setting("mode", "enforce")
    except Exception:
        return "enforce"
    if raw is False:  # YAML 1.1 reads an unquoted ``mode: off`` as the boolean False
        return "off"
    value = str(raw).strip().lower()
    return value if value in ("enforce", "warn", "off") else "enforce"


def window_view(actions: Any, window: Any, now: int) -> dict[str, Any]:
    data = {k: getattr(window, k) for k in WINDOW_FIELDS}
    data["open"] = window.is_open(now)
    data["span"] = actions.span(window)
    # What a person needs to trace an overlap (the other window's name, the shared span, the rule);
    # ``overlaps`` keeps the bare ids.
    data["conflicts"] = overlaps.describe(actions, window, now)
    data["overlaps"] = [c["id"] for c in data["conflicts"]]
    data["proposer"] = overlaps.proposer(window.proposed_by)  # the agent profile, not a person
    data["proposed"] = overlaps.proposed(window.proposed_by)  # the words: "proposed by profile 'default'"
    data["starts"] = actions.fmt(window.start_ts)
    data["decided_at"] = window.decided_at
    data["decided_when"] = actions.fmt(window.decided_at) if window.decided_at else ""
    data["closed_when"] = actions.fmt(window.closed_at) if window.closed_at else ""
    data["card_details"] = card_reader.details(window.cards)
    return data


def snapshot_view(result: dict[str, Any]) -> dict[str, Any]:
    """The same fields from a stored action result. A replayed result from before a field existed
    has no value for it."""
    return {k: result.get(k) for k in WINDOW_FIELDS}


def list_view(actions: Any) -> dict[str, Any]:
    """Windows from today to seven days ahead, in the plugin's time zone."""
    now = int(actions.clock())
    today = datetime.fromtimestamp(now, actions.tz() or timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0)
    start, end = int(today.timestamp()), int((today + timedelta(days=8)).timestamp())
    rows = actions.store.list_windows(start, end, states=_LISTED_STATES)
    # Today and tomorrow, midnight to midnight in the plugin's zone: what the timeline draws.
    days = [{"start": int((today + timedelta(days=i)).timestamp()), "end": int((today + timedelta(days=i + 1)).timestamp()),
             "label": (today + timedelta(days=i)).strftime("%a %d %b")} for i in (0, 1)]
    return {"now": now, "start": start, "end": end, "days": days, "zone": today.strftime("%Z"),
            "windows": [window_view(actions, w, now) for w in rows]}


def audit_view(store: Any, limit: int = 20) -> list[dict[str, Any]]:
    """Recent matched calls, metadata only: even an opted-in command preview stays in the store."""
    return [{k: row[k] for k in AUDIT_FIELDS} for row in store.audit_tail(limit)]


def decisions_view(store: Any, limit: int = 20) -> list[dict[str, Any]]:
    """Recent approvals and declines with the channel each came through."""
    return [{"window_id": w.id, "name": w.name, "scope": w.scope, "state": w.state, "decided_at": w.decided_at,
             "decided_by": w.decided_by, "decided_via": w.decided_via} for w in store.recent_decisions(limit)]
