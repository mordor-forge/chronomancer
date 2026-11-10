"""Overlaps in words an owner can follow: which window, when, why, what it means and the way out.

One source for every owner surface (Hermes Desktop, the Mini App, Telegram) and for the refusal an
approval gets. Ids stay in the data; the sentences use names, scopes and local times. Names are
agent-written: every surface shows these strings as plain text.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .store import OverlapError, Window

WAY_OUT = "Decline one of them, or ask the agent to propose another time."
_NAME_MAX = 60


def proposer(proposed_by: str) -> str:
    """The agent profile behind ``agent:<profile>``; other proposers as stored."""
    text = str(proposed_by or "")
    return text[6:] if text.startswith("agent:") else text


def duration(seconds: int) -> str:
    minutes = max(1, round(seconds / 60))
    hours, rest = divmod(minutes, 60)
    if not hours:
        return f"{minutes} min"
    return f"{hours} h {rest} min" if rest else f"{hours} h"


def _short(name: str) -> str:
    return name if len(name) <= _NAME_MAX else name[:_NAME_MAX - 1].rstrip() + "…"


def _day(actions: Any, ts: int) -> Any:
    return datetime.fromtimestamp(ts, actions.tz() or timezone.utc).date()


def _range(actions: Any, start: int, end: int, *, today: Any) -> str:
    """``14:00-14:30`` today, ``Wed 11 Nov 14:00-14:30`` on another day."""
    first = actions.fmt(start, time_only=_day(actions, start) == today)
    return f"{first}-{actions.fmt(end, time_only=_day(actions, start) == _day(actions, end))}"


def state_word(w: Window, now: int) -> str:
    return "open" if w.is_open(now) else w.state


def facts(actions: Any, w: Window, other: Window, now: int) -> dict[str, Any]:
    """Everything a surface needs to explain that ``w`` overlaps ``other``."""
    start, end = max(w.start_ts, other.start_ts), min(w.end_ts, other.end_ts)
    today = _day(actions, now)
    span = _range(actions, start, end, today=today)
    length = duration(end - start)
    if w.exclusive and other.exclusive:
        rule = "both windows are exclusive"
    elif w.exclusive:
        rule = "this window is exclusive"
    else:
        rule = "that window is exclusive"
    name = _short(other.name)
    if w.state == "pending" and other.state == "approved":
        consequence = f"Approving this window is refused while '{name}' is approved."
    elif w.state == "approved" and other.state == "pending":
        consequence = f"Approving '{name}' is refused while this window is approved."
    else:
        consequence = "Only one of them can be approved: after one approval, approving the other is refused."
    return {
        "id": other.id, "name": other.name, "scope": other.scope, "state": state_word(other, now),
        "span": actions.span(other), "proposer": proposer(other.proposed_by), "exclusive": other.exclusive,
        "start_ts": start, "end_ts": end, "overlap": span, "length": length,
        "text": f"overlaps {span}, {length}",
        "why": f"same scope {other.scope}; {rule}",
        "consequence": consequence, "way_out": WAY_OUT,
    }


def describe(actions: Any, w: Window, now: int) -> list[dict[str, Any]]:
    """The live windows ``w`` conflicts with (none for a finished window)."""
    if w.state not in ("pending", "approved") or w.closed_at is not None:
        return []
    return [facts(actions, w, other, now) for other in actions._overlaps(w)]


def line(fact: dict[str, Any]) -> str:
    """One conflict as one plain-text line; the id comes last."""
    by = f", proposed by {fact['proposer']}" if fact["proposer"] else ""
    return (f"'{_short(fact['name'])}' ({fact['state']}, {fact['span']}{by}): {fact['text']}. "
            f"Why: {fact['why']}. [{fact['id']}]")


def refusal(actions: Any, error: OverlapError, now: int) -> str:
    """Why an approval was refused, for the person who pressed Approve."""
    w = error.window
    today = _day(actions, now)
    parts = []
    for other in error.clashes:
        start, end = max(w.start_ts, other.start_ts), min(w.end_ts, other.end_ts)
        parts.append(f"'{_short(other.name)}' ({state_word(other, now)}, "
                     f"{_range(actions, other.start_ts, other.end_ts, today=today)}, {other.scope}) "
                     f"for {duration(end - start)}")
    return f"Can't approve '{_short(w.name)}': it overlaps {' and '.join(parts)}. {WAY_OUT}"
