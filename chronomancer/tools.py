"""The orchestrator's tools: read the plan, give a card a slot, run it now, clear its slot, and
manage labels. Every handler returns a JSON string and never raises.

The tools and the owner's Desktop page change the same plan. A tool call is recorded as
``agent:<profile>``. An agent cannot pin a slot, and cannot move, run or clear a slot that the
owner pinned.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable

from . import views
from .board import BoardError
from .planner import PlanError, Planner

logger = logging.getLogger(__name__)

_BOARD = {"type": "string", "description": "Kanban board slug. Leave empty for the configured board."}
_CARD = {"type": "string", "description": "Kanban card id, for example t_1a2b3c4d."}

PLAN = {
    "name": "chrono_plan",
    "description": (
        "Read the plan of the Kanban board: every card with a slot (planned start, estimate, profile, status), "
        "the clashes between slots, the cards without a slot, the labels, and what the owner or the timer "
        "changed since you last looked. Call it before you plan or move work, and to answer what is on the "
        "plate for the next day, two days or week."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "horizon": {"type": "string", "enum": ["day", "2d", "week"],
                        "description": "How far ahead to look: day (24 h), 2d (48 h, the default) or week."},
            "profile": {"type": "string", "description": "Only the cards assigned to this profile."},
            "label": {"type": "string", "description": "Only the cards that carry this label."},
            "board": _BOARD,
        },
        "required": [],
    },
}

SLOT = {
    "name": "chrono_slot",
    "description": (
        "Give a Kanban card a slot (a planned start and an estimate), or move its slot. The plugin parks the card "
        "and releases it at the start, so no cron job or wake-up is needed. The result lists the clashes with "
        "other slots and the next start without a clash. A clash never refuses the move: read the result and move "
        "the slot again when it matters. Refused when the owner pinned the slot, or when the card is running, in "
        "review, done or in triage. Set dry_run to check a time without changing anything."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "card": _CARD,
            "start": {"type": "string", "description": "Start: ISO 8601 (2026-11-12T09:00, read in the plugin's time "
                                                       "zone when it has no offset), or a delay such as +90m, +2h or +1d."},
            "estimate_minutes": {"type": "integer", "description": "Expected duration in minutes. A guess, not a "
                                                                   "limit. Default: the slot's current estimate, else the configured default."},
            "dry_run": {"type": "boolean", "description": "Only report the clashes and the next free start."},
            "board": _BOARD,
        },
        "required": ["card", "start"],
    },
}

NOW = {
    "name": "chrono_now",
    "description": (
        "Run a Kanban card now: release it at once and move its slot to now. Use it when the owner says "
        "\"do it now\". Refused when the owner pinned the slot, or when the card is running, in review, done or in triage."
    ),
    "parameters": {"type": "object", "properties": {"card": _CARD, "board": _BOARD}, "required": ["card"]},
}

CLEAR = {
    "name": "chrono_clear",
    "description": (
        "Take a card out of the plan (remove its slot). The card itself is not changed: a parked or blocked card "
        "stays as it is until it gets a new slot or is run now."
    ),
    "parameters": {"type": "object", "properties": {"card": _CARD, "board": _BOARD}, "required": ["card"]},
}

LABEL = {
    "name": "chrono_label",
    "description": (
        "Create or change a label, put it on cards or take it off. Labels are the plan's filters: a repository, "
        "a project, a kind of operation (repo:web, project:billing, op:upgrade). Create them as you need them. "
        "A label with one_at_a_time=true names a thing that only one card may touch at a time (a cluster, a "
        "database): two overlapping slots that share it clash."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "label": {"type": "string", "description": "Lower-case name, at most one colon: repo:web, cluster:prod."},
            "one_at_a_time": {"type": "boolean", "description": "True: overlapping slots that share this label clash."},
            "description": {"type": "string", "description": "What the label means, in a few words."},
            "cards": {"type": "array", "items": {"type": "string"}, "description": "Card ids to put the label on."},
            "remove_cards": {"type": "array", "items": {"type": "string"}, "description": "Card ids to take the label off."},
            "delete": {"type": "boolean", "description": "True: delete the label from the plan and from every card."},
            "board": _BOARD,
        },
        "required": ["label"],
    },
}

SCHEMAS = (PLAN, SLOT, NOW, CLEAR, LABEL)
_DELAY = re.compile(r"\+\s*(\d{1,5})\s*(m|min|h|d)", re.IGNORECASE)


def _ok(**data: Any) -> str:
    return json.dumps({"ok": True, **data}, ensure_ascii=False, default=str)


def _err(message: str) -> str:
    return json.dumps({"ok": False, "error": message}, ensure_ascii=False)


def parse_time(value: Any, tz: Any, now: int) -> int:
    """A start as Unix seconds: ISO 8601 (read in the plugin's zone when it has no offset), or a
    delay from now such as ``+90m``, ``+2h`` or ``+1d``."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)) or not str(value).strip():
        raise PlanError("Give a start: ISO 8601 such as 2026-11-12T09:00, or a delay such as +90m.")
    if isinstance(value, (int, float)):
        return int(value)
    text = value.strip()
    delay = _DELAY.fullmatch(text)
    if delay:
        return now + int(delay.group(1)) * {"m": 60, "min": 60, "h": 3600, "d": 86400}[delay.group(2).lower()]
    if text.lower() == "now":
        return now
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PlanError(f"{text!r} is not a time. Use ISO 8601 such as 2026-11-12T09:00, or a delay such as +90m.") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz or timezone.utc)
    return int(moment.timestamp())


class Tools:
    def __init__(self, planner: Planner, *, actor: Callable[[], str], refresh: Callable[[], None] = lambda: None,
                 clock: Callable[[], float] = time.time):
        self.planner, self.actor, self.refresh, self.clock = planner, actor, refresh, clock

    def _run(self, name: str, call: Callable[[], dict[str, Any]]) -> str:
        try:
            self.refresh()
            self.planner.run_due()  # a second trigger next to the timer: cheap when nothing is due
            return _ok(**call())
        except PlanError as exc:
            return _err(str(exc))
        except BoardError as exc:
            return _err(f"The Kanban board cannot be reached now ({exc}). Nothing was changed.")
        except Exception as exc:
            logger.exception("chronomancer: %s failed", name)
            return _err(f"{name} failed ({type(exc).__name__}). Nothing may have changed; read the plan again.")

    @staticmethod
    def _args(args: Any) -> dict[str, Any]:
        return args if isinstance(args, dict) else {}

    def plan(self, args: Any = None, **_: Any) -> str:
        a = self._args(args)
        return self._run("chrono_plan", lambda: views.agent_view(
            self.planner, self.actor(), horizon_key=a.get("horizon") or views.DEFAULT_HORIZON,
            board=a.get("board") or None, profile=str(a.get("profile") or ""), label=str(a.get("label") or "").lower()))

    def slot(self, args: Any = None, **_: Any) -> str:
        a = self._args(args)

        def call() -> dict[str, Any]:
            minutes = a.get("estimate_minutes")
            if minutes is not None and (isinstance(minutes, bool) or not isinstance(minutes, int)):
                raise PlanError("estimate_minutes is a whole number of minutes.")
            start = parse_time(a.get("start"), self.planner.tz(), int(self.clock()))
            change = self.planner.set_slot(str(a.get("card") or ""), start,
                                           estimate_s=minutes * 60 if minutes is not None else None,
                                           actor=self.actor(), board=a.get("board") or None,
                                           dry_run=a.get("dry_run") is True)
            return views.change_view(self.planner, change)

        return self._run("chrono_slot", call)

    def now(self, args: Any = None, **_: Any) -> str:
        a = self._args(args)
        return self._run("chrono_now", lambda: views.change_view(self.planner, self.planner.run_now(
            str(a.get("card") or ""), actor=self.actor(), board=a.get("board") or None)))

    def clear(self, args: Any = None, **_: Any) -> str:
        a = self._args(args)
        return self._run("chrono_clear", lambda: views.change_view(self.planner, self.planner.clear(
            str(a.get("card") or ""), actor=self.actor(), board=a.get("board") or None)))

    def label(self, args: Any = None, **_: Any) -> str:
        a = self._args(args)

        def call() -> dict[str, Any]:
            name, actor, board = a.get("label"), self.actor(), a.get("board") or None
            if a.get("delete") is True:
                self.planner.drop_label(name, actor=actor)
                return {"label": str(name).strip().lower(), "deleted": True}
            exclusive = a.get("one_at_a_time")
            if exclusive is not None and not isinstance(exclusive, bool):
                raise PlanError("one_at_a_time is true or false.")
            label = self.planner.label(name, exclusive=exclusive, description=a.get("description"), actor=actor)
            cards = {}
            for key, kwargs in (("cards", "add"), ("remove_cards", "remove")):
                ids = a.get(key) or []
                if not isinstance(ids, list):
                    raise PlanError(f"{key} is a list of card ids.")
                for task_id in ids:
                    cards[str(task_id)] = self.planner.tag(str(task_id), actor=actor, board=board, **{kwargs: [label.name]})
            return {"label": label.name, "one_at_a_time": label.exclusive, "description": label.description,
                    **({"cards": cards} if cards else {})}

        return self._run("chrono_label", call)

    def handlers(self) -> tuple:
        return ((PLAN, self.plan), (SLOT, self.slot), (NOW, self.now), (CLEAR, self.clear), (LABEL, self.label))
