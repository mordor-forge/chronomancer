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


def explain(actions: Any, w: Window, others: list[Window], now: int) -> dict[str, Any]:
    """THE conflict explanation: every sentence an owner reads about ``w`` overlapping ``others``.

    Hermes Desktop, the Mini App and Telegram lay these strings out; none of them writes its own.
    ``conflicts`` has one entry per other window:

    - data: ``id``, ``name``, ``scope``, ``state``, ``span``, ``proposer``, ``exclusive``, and the
      shared span as ``start_ts``/``end_ts``, ``overlap`` ("14:00-14:30") and ``length`` ("30 min");
    - ``label``: the short human line, for a chip or a button;
    - ``line``: the full line for a message, the id last; ``brief``: the same for a list row;
    - ``why``: the rule; ``consequence``: what it means; ``way_out``: what the owner can do;
    - ``title`` and ``rows`` (label, text): the explanation as a panel, a sheet or a message block;
    - ``pair`` and ``note``: the names of both windows, and a short tag, for a timeline.

    ``refusal`` is what an approval of ``w`` is answered with while ``others`` are approved.
    """
    today = _day(actions, now)
    mine = _short(w.name)
    conflicts, clashes = [], []
    for other in others:
        start, end = max(w.start_ts, other.start_ts), min(w.end_ts, other.end_ts)
        overlap, length = _range(actions, start, end, today=today), duration(end - start)
        name, state, span = _short(other.name), state_word(other, now), actions.span(other)
        by = proposer(other.proposed_by)
        if w.exclusive and other.exclusive:
            rule = "both windows are exclusive"
        elif w.exclusive:
            rule = "this window is exclusive"
        else:
            rule = "that window is exclusive"
        why = f"same scope {other.scope}; {rule}"
        if w.state == "pending" and other.state == "approved":
            consequence = f"Approving this window is refused while '{name}' is approved."
        elif w.state == "approved" and other.state == "pending":
            consequence = f"Approving '{name}' is refused while this window is approved."
        else:
            consequence = "Only one of them can be approved: after one approval, approving the other is refused."
        proposed = f"proposed by {by}" if by else ""
        conflicts.append({
            "id": other.id, "name": other.name, "scope": other.scope, "state": state, "span": span,
            "proposer": by, "exclusive": other.exclusive,
            "start_ts": start, "end_ts": end, "overlap": overlap, "length": length,
            "text": f"overlaps {overlap}, {length}",
            "label": f"Overlaps '{name}' {overlap}, {length}",
            "line": (f"Overlaps '{name}' ({state}, {span}{', ' + proposed if proposed else ''}): "
                     f"overlaps {overlap}, {length}. Why: {why}. [{other.id}]"),
            "brief": f"overlaps '{name}' {overlap}, {length} ({why})",
            "why": why, "consequence": consequence, "way_out": WAY_OUT,
            "title": f"This window and '{name}' overlap",
            "rows": [["Other window", " · ".join(x for x in (name, state, span, f"scope {other.scope}", proposed) if x)],
                     ["Shared span", f"{overlap}, {length}"],
                     ["Rule", why],
                     ["What it means", consequence],
                     ["Ways out", WAY_OUT]],
            "pair": f"Overlap: '{mine}' and '{name}', {overlap}, {length}",
            "note": f"overlap {length}",
        })
        clashes.append(f"'{name}' ({state}, {_range(actions, other.start_ts, other.end_ts, today=today)}, "
                       f"{other.scope}) for {length}")
    refusal = f"Can't approve '{mine}': it overlaps {' and '.join(clashes)}. {WAY_OUT}" if clashes else ""
    return {"conflicts": conflicts, "refusal": refusal}


def describe(actions: Any, w: Window, now: int) -> list[dict[str, Any]]:
    """``explain`` for the live windows ``w`` conflicts with now (none for a finished window)."""
    if w.state not in ("pending", "approved") or w.closed_at is not None:
        return []
    return explain(actions, w, actions._overlaps(w), now)["conflicts"]


def refusal(actions: Any, error: OverlapError, now: int) -> str:
    """``explain``'s refusal for the approval the store refused."""
    return explain(actions, error.window, error.clashes, now)["refusal"]
