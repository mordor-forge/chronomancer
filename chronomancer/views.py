"""What the surfaces show: the plan for the owner's page, a compact plan for the agent tools, and
the result of one move. Every sentence a person or an agent reads about the plan is built here or
in ``clashes``; the surfaces only lay them out.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import clashes
from .board import PARKABLE
from .planner import (DONE, DUE, FAILED_, GONE, MISSED_, OWNER, PLANNED_, RUNNING, UNPLANNED, WAITING, Change, Item,
                      Planner, Snapshot)
from .store import LogEntry

HORIZONS = {"day": (2, 24, 1), "2d": (3, 48, 3), "week": (0, 168, 12)}   # hours back, hours ahead, hours per tick
DEFAULT_HORIZON = "2d"
NIGHT = (22, 6)             # the quiet band on the timeline, local hours
_NOT_MOVABLE = ("running", "review", "done", "archived", "triage")
_QUIET = ("label", "unlabel", "tag")

_TRAY_HINTS = {
    "triage": "In triage. Specify the card before it can be planned.",
    "todo": "Waits for its parent cards. The Kanban starts it without the plan.",
    "ready": "Ready. The Kanban starts it without the plan.",
    "scheduled": "Parked. It runs when it gets a slot or when you run it now.",
    "blocked": "Blocked. It runs when it gets a slot or when you run it now.",
    "review": "In review.",
}


def _since(seconds: int) -> str:
    return clashes.minutes_text(abs(seconds))


def _local(planner: Planner, ts: int) -> str:
    """A moment as the page's date-and-time field wants it: local, to the minute, without an offset."""
    return datetime.fromtimestamp(ts, planner.tz() or timezone.utc).strftime("%Y-%m-%dT%H:%M")


# ---- the horizon ----

def horizon(planner: Planner, key: Any, now: int) -> dict[str, Any]:
    """The time span on the page: ``day``, ``2d`` or ``week``, or a number of hours ahead (1 to 336)."""
    zone = planner.tz() or timezone.utc
    if key in HORIZONS:
        back, ahead, step = HORIZONS[key]
    else:
        try:
            ahead = min(max(int(key), 1), 336)
        except (TypeError, ValueError):
            key, (back, ahead, step) = DEFAULT_HORIZON, HORIZONS[DEFAULT_HORIZON]
        else:
            key, back, step = str(ahead), 2, 1 if ahead <= 30 else 3 if ahead <= 72 else 12
    local = datetime.fromtimestamp(now, zone)
    if key == "week":
        first = local.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        first = local.replace(minute=0, second=0, microsecond=0) - timedelta(hours=back)
    start = int(first.timestamp())
    if key == "week":  # seven local days, also when one of them has 23 or 25 hours
        end = int((first + timedelta(days=7)).timestamp())
    else:
        end = int(local.replace(minute=0, second=0, microsecond=0).timestamp()) + ahead * 3600
    ticks, nights, days = [], [], []
    ts = start
    while ts <= end:
        at = datetime.fromtimestamp(ts, zone)
        if at.minute == 0 and at.hour % step == 0:
            midnight = at.hour == 0
            ticks.append({"ts": ts, "label": at.strftime("%a %d") if midnight else at.strftime("%H:%M"), "day": midnight})
        ts += 3600
    day = first.replace(hour=0, minute=0, second=0, microsecond=0)
    while int(day.timestamp()) < end:
        nxt = (day + timedelta(days=1, hours=3)).replace(hour=0)  # the next local midnight, also across a DST change
        d0, d1 = int(day.timestamp()), int(nxt.timestamp())
        days.append({"start": max(d0, start), "end": min(d1, end), "label": day.strftime("%a %d %b"),
                     "today": day.date() == local.date()})
        for n0, n1 in ((d0, int(day.replace(hour=NIGHT[1]).timestamp())), (int(day.replace(hour=NIGHT[0]).timestamp()), d1)):
            if n1 > start and n0 < end:
                nights.append({"start": max(n0, start), "end": min(n1, end)})
        day = nxt
    return {"key": key, "start": start, "end": end, "ticks": ticks, "days": days, "nights": nights,
            "label": f"{planner.fmt(start)} to {planner.fmt(end)}"}


# ---- one card ----

def _status_line(planner: Planner, item: Item, now: int) -> str:
    slot, card = item.slot, item.card
    if item.phase == PLANNED_:
        return f"Starts in {_since(slot.start_ts - now)}."
    if item.phase == DUE:
        return "Its slot has started. The timer releases it within a minute."
    if item.phase == WAITING:
        return f"Released {planner.fmt(slot.released_at)}. The card is {card.status if card else 'unknown'}."
    if item.phase == RUNNING:
        text = f"Running for {_since(now - item.start)}."
        return text[:-1] + f", {_since(item.overrun_s)} over its estimate." if item.overrun_s else text
    if item.phase == DONE:
        return f"Done {planner.fmt(item.end)}."
    if item.phase == MISSED_:
        return "Missed its slot: the timer was not running. Move it or run it now."
    if item.phase == FAILED_:
        return f"Not released: {slot.note or 'unknown reason'}."
    if item.phase == GONE:
        return "The card is no longer on the board."
    return _TRAY_HINTS.get(card.status if card else "", "")


def item_view(planner: Planner, snap: Snapshot, item: Item) -> dict[str, Any]:
    """Everything the owner's page shows for one card. Explicit fields: nothing else leaves the store."""
    slot, card = item.slot, item.card
    status = card.status if card else ""
    found = snap.clashes_of(item.task_id)
    movable = card is not None and status not in _NOT_MOVABLE
    free = None
    if movable and item.phase == PLANNED_ and any(c.kind == clashes.CLASH for c in found):
        own = next((s for s in snap.spans if s.task_id == item.task_id), None)
        free = clashes.next_free(own, snap.spans) if own else None
    return {
        "task_id": item.task_id, "title": item.title, "short": clashes.short(item.title),
        "assignee": item.assignee, "status": status, "phase": item.phase, "priority": card.priority if card else 0,
        "model": card.model if card else "", "labels": list(item.labels),
        "start": item.start, "end": item.end,
        "planned_start": slot.start_ts if slot else None, "planned_end": slot.end_ts if slot else None,
        "estimate_s": slot.estimate_s if slot else None,
        "estimate_min": slot.estimate_s // 60 if slot else None,
        "estimate": clashes.minutes_text(slot.estimate_s) if slot else "",
        "start_local": _local(planner, slot.start_ts) if slot else "",
        "when": planner.span_text(item.start, item.end) if item.start is not None and item.end is not None else "",
        "pinned": bool(slot and slot.pinned), "note": slot.note if slot else "",
        "set_by": slot.set_by if slot else "", "set_by_text": f"Planned by {planner.who(slot.set_by)}." if slot else "",
        "line": _status_line(planner, item, snap.now), "overrun_s": item.overrun_s,
        "clash": any(c.kind == clashes.CLASH for c in found), "busy": any(c.kind == clashes.BUSY for c in found),
        "clashes": [{"kind": c.kind, "with": c.other(item.task_id).task_id, "start": c.start, "end": c.end,
                     "text": planner.describe(c, item.task_id)} for c in found],
        "next_free": free, "next_free_text": planner.fmt(free) if free is not None else "",
        "can": {"move": movable, "now": movable, "clear": slot is not None, "pin": slot is not None},
    }


# ---- the change log ----

def log_text(planner: Planner, entry: LogEntry) -> str:
    """One log entry as a sentence."""
    d, who = entry.detail, planner.who(entry.actor)
    title = f'"{clashes.short(str(d.get("title") or entry.task_id))}"'
    start, before = d.get("start"), d.get("from")
    when = planner.fmt(start) if isinstance(start, int) else ""
    was = planner.fmt(before) if isinstance(before, int) else ""
    text = {
        "plan": f"{who} planned {title} for {when} ({clashes.minutes_text(int(d.get('estimate_s') or 60))})",
        "move": f"{who} moved {title} from {was} to {when}",
        "now": f"{who} ran {title} now" + (f" (it was planned for {was})" if was else ""),
        "clear": f"{who} took {title} out of the plan" + (f" (it was planned for {was})" if was else ""),
        "pin": f"{who} pinned {title} at {when}",
        "unpin": f"{who} unpinned {title}",
        "release": f"{who} released {title} at its slot",
        "missed": f"{title} missed its slot at {when}: the timer was not running",
        "failed": f"{who} could not release {title}: {d.get('note') or 'unknown reason'}",
        "label": f"{who} {'created' if d.get('new') else 'changed'} label {d.get('label')}"
                 + (" (one card at a time)" if d.get("exclusive") else ""),
        "unlabel": f"{who} removed label {d.get('label')}",
        "tag": (f"{who} set the labels of {title} to {', '.join(d.get('labels') or [])}" if d.get("labels")
                else f"{who} removed every label from {title}"),
    }.get(entry.action, f"{who}: {entry.action} {title}")
    if entry.action in ("plan", "move") and d.get("clashes"):
        text += f", with {d['clashes']} clash{'es' if d['clashes'] != 1 else ''}"
    return text[:1].upper() + text[1:] + "."


def log_view(planner: Planner, entry: LogEntry) -> dict[str, Any]:
    return {"id": entry.id, "ts": entry.ts, "when": planner.fmt(entry.ts), "actor": entry.actor,
            "by_owner": entry.actor == OWNER, "task_id": entry.task_id, "text": log_text(planner, entry),
            "quiet": entry.action in _QUIET}  # label housekeeping: the page shows it only on request


# ---- badges on Kanban cards ----

def badge(planner: Planner, snap: Snapshot, item: Item) -> Optional[dict[str, str]]:
    """A short label for the card's tile on the Kanban board, or None when the plan has nothing to add."""
    slot = item.slot
    if slot is None or item.phase in (DONE, RUNNING, UNPLANNED):
        return None
    at = datetime.fromtimestamp(slot.start_ts, planner.tz() or timezone.utc).strftime("%a %H:%M")
    clash = any(c.kind == clashes.CLASH for c in snap.clashes_of(item.task_id))
    if item.phase in (PLANNED_, DUE):
        label, tone = (f"clash {at}", "red") if clash else (f"slot {at}", "blue")
    elif item.phase == WAITING:
        label, tone = f"released {at}", "green"
    elif item.phase == MISSED_:
        label, tone = "missed its slot", "orange"
    else:
        label, tone = "not released", "orange"
    return {"label": label, "tone": tone, "tooltip": f"Chronomancer. {planner.span_text(slot.start_ts, slot.end_ts)}. "
                                                     f"{_status_line(planner, item, snap.now)}"}


# ---- the headline ----

def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def summary(planner: Planner, snap: Snapshot, counts: dict[str, int]) -> dict[str, Any]:
    """What runs next and what needs a look, for the page header and the status-bar chip."""
    zone = planner.tz() or timezone.utc
    upcoming = sorted((i for i in snap.items if i.phase == PLANNED_), key=lambda i: i.start or 0)
    running = sum(1 for i in snap.items if i.phase == RUNNING)
    if upcoming:
        nxt = upcoming[0]
        at = datetime.fromtimestamp(nxt.start, zone)
        same_day = at.date() == datetime.fromtimestamp(snap.now, zone).date()
        when = at.strftime("%H:%M") if same_day else at.strftime("%a %H:%M")
        headline = (f'Next: "{clashes.short(nxt.title)}" ({nxt.assignee or "no profile"}) '
                    f"in {_since(nxt.start - snap.now)}, at {when}.")
        chip = f"next {when}"
    elif running:
        headline, chip = f"{_count(running, 'card runs', 'cards run')} now. Nothing else is planned.", f"{running} running"
    else:
        headline, chip = "Nothing is planned.", "no slots"
    facts = [(kind, text) for kind, n, text in (
        ("running", running, _count(running, "running", "running")),
        ("clash", counts["clashes"], _count(counts["clashes"], "clash", "clashes")),
        ("busy", counts["busy"], _count(counts["busy"], "busy overlap", "busy overlaps")),
        ("attention", counts["attention"], _count(counts["attention"], "needs a look", "need a look")),
        ("planned", counts["planned"], _count(counts["planned"], "planned", "planned")),
        ("tray", counts["tray"], _count(counts["tray"], "unplanned", "unplanned")),
    ) if n]
    if counts["clashes"]:
        chip += f" · {_count(counts['clashes'], 'clash', 'clashes')}"
    if counts["attention"]:
        chip += f" · {_count(counts['attention'], 'needs a look', 'need a look')}"
    return {"headline": headline, "facts": [{"kind": kind, "text": text} for kind, text in facts],
            "chip": chip, "tip": headline}


# ---- the owner's page ----

def owner_view(planner: Planner, *, horizon_key: Any = DEFAULT_HORIZON, board: Optional[str] = None) -> dict[str, Any]:
    snap = planner.snapshot(board)
    span = horizon(planner, horizon_key, snap.now)
    views = [item_view(planner, snap, i) for i in snap.items]
    timed = [v for v in views if v["start"] is not None]
    shown = [v for v in timed if v["end"] > span["start"] and v["start"] < span["end"]]
    later = sorted((v for v in timed if v["start"] >= span["end"]), key=lambda v: v["start"])
    tray = [v for v in views if v["phase"] == UNPLANNED]
    profiles = list(dict.fromkeys(sorted(v["assignee"] for v in [*shown, *tray])))
    pairs = [c for c in snap.clashes if c.end > span["start"] and c.start < span["end"]]
    used: dict[str, int] = {}
    for v in views:
        for name in v["labels"]:
            used[name] = used.get(name, 0) + 1
    known = {label.name for label in snap.labels}
    counts = {"clashes": sum(1 for c in pairs if c.kind == clashes.CLASH),
              "busy": sum(1 for c in pairs if c.kind == clashes.BUSY),
              "planned": sum(1 for v in shown if v["phase"] in (PLANNED_, DUE)),
              "running": sum(1 for v in shown if v["phase"] == RUNNING),
              "attention": sum(1 for v in views if v["phase"] in (MISSED_, FAILED_, GONE)),
              "tray": len(tray)}
    zone = planner.tz() or timezone.utc
    local_now = datetime.fromtimestamp(snap.now, zone)
    offset = local_now.utcoffset()
    next_hour = local_now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    later_text = ""
    if later:
        later_text = (f"{_count(len(later), 'more slot', 'more slots')} after this horizon. "
                      f'Next: "{later[0]["short"]}", {later[0]["when"]}.')
    return {
        "now": snap.now, "now_text": planner.fmt(snap.now), "zone": local_now.strftime("%Z"),
        "tz": getattr(zone, "key", "") or "", "utc_offset_s": int(offset.total_seconds()) if offset else 0,
        "suggest_local": next_hour.strftime("%Y-%m-%dT%H:%M"), "default_estimate_min": planner.default_estimate_s() // 60,
        "board": snap.board, "board_error": snap.board_error, "horizon": span,
        "lanes": [{"profile": p, "label": p or "No profile",
                   "slots": sum(1 for v in shown if v["assignee"] == p)} for p in profiles],
        "items": shown, "tray": tray,
        "later": {"count": len(later), "next": later[0]["when"] if later else "",
                  "next_title": later[0]["short"] if later else "", "text": later_text},
        "labels": [{"name": label.name, "exclusive": label.exclusive, "description": label.description,
                    "cards": used.get(label.name, 0), "derived": False} for label in snap.labels]
                  + [{"name": name, "exclusive": False, "description": "From the board.", "cards": count, "derived": True}
                     for name, count in sorted(used.items()) if name not in known],
        "counts": counts, "summary": summary(planner, snap, counts),
        "badges": {i.task_id: b for i in snap.items for b in [badge(planner, snap, i)] if b},
        "log": [log_view(planner, e) for e in planner.store.log_tail(40)],
    }


# ---- the agent tools ----

def agent_view(planner: Planner, actor: str, *, horizon_key: Any = DEFAULT_HORIZON, board: Optional[str] = None,
               profile: str = "", label: str = "", tray_limit: int = 40) -> dict[str, Any]:
    """The plan for an agent: compact, with times as ISO 8601 in the plugin's zone."""
    snap = planner.snapshot(board)
    span = horizon(planner, horizon_key, snap.now)

    def keep(item: Item) -> bool:
        return (not profile or item.assignee == profile) and (not label or label in item.labels)

    slots, tray = [], []
    for item in filter(keep, snap.items):
        if item.phase == UNPLANNED:
            tray.append({"card": item.task_id, "title": clashes.short(item.title, 90), "profile": item.assignee,
                         "status": item.card.status if item.card else "", "labels": list(item.labels)})
        elif item.start is not None and item.end > span["start"] and item.start < span["end"]:
            row = {"card": item.task_id, "title": clashes.short(item.title, 90), "profile": item.assignee,
                   "status": item.card.status if item.card else "", "phase": item.phase,
                   "start": planner.iso(item.start), "end": planner.iso(item.end),
                   "labels": list(item.labels)}
            if item.slot:
                row["estimate_minutes"] = item.slot.estimate_s // 60
                if item.slot.pinned:
                    row["pinned_by_owner"] = True
                if item.slot.note:
                    row["note"] = item.slot.note
            found = [planner.describe(c, item.task_id) for c in snap.clashes_of(item.task_id)]
            if found:
                row["clashes"] = found
            if item.overrun_s:
                row["over_estimate_minutes"] = item.overrun_s // 60
            slots.append(row)
    news = [{"when": planner.iso(e.ts), "what": log_text(planner, e)}
            for e in planner.changes_since_last_look(actor)]
    out: dict[str, Any] = {
        "now": planner.iso(snap.now), "horizon": {"from": planner.iso(span["start"]), "to": planner.iso(span["end"])},
        "slots": slots, "unplanned": tray[:tray_limit],
        "labels": [{"name": l.name, "one_at_a_time": l.exclusive, **({"description": l.description} if l.description else {})}
                   for l in snap.labels],
        "since_your_last_look": news,
    }
    if len(tray) > tray_limit:
        out["unplanned_more"] = len(tray) - tray_limit
    if snap.board_error:
        out["board_error"] = snap.board_error
    return out


# ---- the result of one move ----

def change_message(planner: Planner, change: Change) -> str:
    title = f'"{clashes.short(change.card.title if change.card else change.task_id)}"'
    slot = change.slot
    if change.action in ("plan", "move", "preview") and slot:
        head = {"plan": "Planned", "move": "Moved", "preview": "Preview, nothing changed:"}[change.action]
        text = f"{head} {title} {'to' if change.action == 'move' else 'for'} {planner.span_text(slot.start_ts, slot.end_ts)}."
        if change.parked and change.card:
            text += f" The card was {change.card.status} and is parked until then."
    elif change.action == "now":
        text = f"{title} runs now ({change.note})."
    elif change.action == "clear":
        text = f"Took {title} out of the plan."
        if change.note:
            text += f" {change.note}"
    elif change.action in ("pin", "unpin"):
        text = f"{'Pinned' if change.action == 'pin' else 'Unpinned'} {title}."
    else:
        text = f"{title}: {change.note or change.action}."
    hard = sum(1 for c in change.clashes if c.kind == clashes.CLASH)
    if hard:
        text += f" It clashes with {hard} other slot{'s' if hard != 1 else ''}."
        if change.next_free is not None:
            text += f" The next start without a clash is {planner.fmt(change.next_free)}."
    return text


def change_view(planner: Planner, change: Change) -> dict[str, Any]:
    slot = change.slot
    out: dict[str, Any] = {"action": change.action, "card": change.task_id, "message": change_message(planner, change)}
    if change.card:
        out["title"], out["status_before"] = clashes.short(change.card.title, 90), change.card.status
    if slot:
        out["slot"] = {"start": planner.iso(slot.start_ts), "end": planner.iso(slot.end_ts), "start_ts": slot.start_ts,
                       "estimate_minutes": slot.estimate_s // 60, "pinned_by_owner": slot.pinned, "state": slot.state}
    if change.clashes:
        out["clashes"] = [planner.describe(c, change.task_id) for c in change.clashes]
    if change.next_free is not None:
        out["next_free_start"] = planner.iso(change.next_free)
    return out


def tray_hint(status: str) -> str:
    return _TRAY_HINTS.get(status, "")


def dispatchable(status: str) -> bool:
    """True when the Kanban starts the card without the plan."""
    return status in PARKABLE
