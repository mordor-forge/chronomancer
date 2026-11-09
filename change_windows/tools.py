"""Agent tools: propose, list, next, close. Handlers return JSON strings and never raise.

The agent can propose and close windows. It can never approve one: approval needs the owner,
through a Telegram button or the Mini App from an allow-listed Telegram user id.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .rules import SELF_SCOPE
from .store import Store, StoreError, Window

_SCOPE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_TASK_RE = re.compile(r"^t_[0-9a-f]{6,32}$")
# Proposals waiting for the owner, over all scopes. Each proposal is one Telegram message, so a
# model that keeps proposing after a block must not be able to flood the owner's chat.
MAX_PENDING = 10
_NO_END = 2 ** 62  # an end bound later than any window

PROPOSE = {
    "name": "window_propose",
    "description": (
        "Propose a change window: a time slot in which production-write tool calls for one scope "
        "(for example k8s-prod, gitops-merge, infra) are allowed. The window stays PENDING until the "
        "owner approves it; you cannot approve it. Use this when a call was blocked by change-windows, "
        "or before planned production work. Link Kanban cards to release them when the window opens."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Short title, e.g. 'Argo CD 3.2 upgrade'."},
            "scope": {"type": "string", "description": "Scope the window opens, e.g. k8s-prod."},
            "start": {"type": "string", "description": "Start, ISO 8601 with offset (2026-11-06T09:00+01:00)."},
            "end": {"type": "string", "description": "End, ISO 8601. Give end or duration_minutes."},
            "duration_minutes": {"type": "integer", "description": "Length in minutes if end is not given."},
            "reason": {"type": "string", "description": "What will change and why; the owner reads this."},
            "cards": {"type": "array", "items": {"type": "string"},
                      "description": "Kanban task ids to release (unblock) when the window opens."},
            "board": {"type": "string", "description": "Kanban board slug for the cards (default board if empty)."},
            "exclusive": {"type": "boolean", "description": "No other window in this scope may overlap (default true)."},
        },
        "required": ["name", "scope", "start", "reason"],
    },
}

LIST = {
    "name": "window_list",
    "description": "List change windows (pending, approved, closed) in a time range, optionally for one scope.",
    "parameters": {
        "type": "object",
        "properties": {
            "scope": {"type": "string"},
            "days": {"type": "integer", "description": "How many days ahead from now (default 7, max 31)."},
        },
    },
}

NEXT = {
    "name": "window_next",
    "description": "Is a change window open now for a scope? If not, the next approved one and any pending proposals.",
    "parameters": {"type": "object", "properties": {"scope": {"type": "string"}}, "required": ["scope"]},
}

CLOSE = {
    "name": "window_close",
    "description": "Close an open change window early when the work is done, or withdraw a pending proposal.",
    "parameters": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "reason": {"type": "string"}},
        "required": ["id", "reason"],
    },
}

SCHEMAS = (PROPOSE, LIST, NEXT, CLOSE)


def _ok(**data: Any) -> str:
    return json.dumps({"ok": True, **data}, ensure_ascii=False, default=str)


def _err(message: str) -> str:
    return json.dumps({"ok": False, "error": message}, ensure_ascii=False)


def parse_time(value: str, tz: Any) -> int:
    """ISO 8601 to Unix seconds; a time without an offset is read in the plugin's time zone."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("time is empty")
    dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz or timezone.utc)
    return int(dt.timestamp())


def _public(w: Window, tz: Any) -> dict[str, Any]:
    fmt = lambda ts: datetime.fromtimestamp(ts, tz or timezone.utc).isoformat(timespec="minutes")  # noqa: E731
    return {"id": w.id, "name": w.name, "scope": w.scope, "start": fmt(w.start_ts), "end": fmt(w.end_ts),
            "state": w.state, "kind": w.kind, "exclusive": w.exclusive, "presence_required": w.presence,
            "checked_in": w.checked_in_at is not None, "cards": w.cards, "reason": w.reason}


class Tools:
    def __init__(self, store: Store, *, settings: Callable[[str, Any], Any], tz: Callable[[], Any],
                 actor: Callable[[], str], clock: Callable[[], float] = time.time,
                 on_proposed: Optional[Callable[[Window, list[Window]], None]] = None):
        self.store = store
        self.settings = settings
        self.tz = tz
        self.actor = actor
        self.clock = clock
        self.on_proposed = on_proposed

    def propose(self, args: dict[str, Any], **_: Any) -> str:
        try:
            tz = self.tz()
            now = int(self.clock())
            scope = str(args.get("scope") or "").strip()
            if not _SCOPE_RE.match(scope) or scope == SELF_SCOPE:
                return _err("scope must be a short lowercase id like k8s-prod")
            start = parse_time(args.get("start", ""), tz)
            if args.get("end"):
                end = parse_time(args["end"], tz)
            elif args.get("duration_minutes"):
                end = start + 60 * int(args["duration_minutes"])
            else:
                return _err("give end or duration_minutes")
            max_hours = float(self.settings("max_window_hours", 8))
            if end <= start:
                return _err("end must be after start")
            if end - start > max_hours * 3600:
                return _err(f"a window may last at most {max_hours:g} hours (setting max_window_hours)")
            if start < now - 300:
                return _err("start is in the past")
            board = str(args.get("board") or "").strip()
            cards = []
            for tid in args.get("cards") or []:
                if not _TASK_RE.match(str(tid)):
                    return _err(f"not a Kanban task id: {tid!r}")
                cards.append({"task_id": str(tid), "board": board})
            reason = str(args.get("reason") or "").strip()
            if not reason:
                return _err("reason is required; the owner reads it before approving")
            # One line: the name heads the owner's message and must not add lines of its own.
            name = " ".join(str(args.get("name") or "").split())[:120] or scope
            pending = self.store.list_windows(now, _NO_END, states=("pending",))
            if len(pending) >= MAX_PENDING:
                return _err(f"{MAX_PENDING} proposals are already waiting for the owner "
                            f"({', '.join(w.id for w in pending[:MAX_PENDING])}). Do not propose more: wait for a "
                            "decision, or withdraw one you no longer need with window_close.")
            window, conflicts = self.store.propose(
                name=name, scope=scope, start_ts=start, end_ts=end,
                reason=reason[:2000], proposed_by=self.actor(), now=now,
                exclusive=bool(args.get("exclusive", True)), cards=cards)  # no presence: no check-in surface yet
            if self.on_proposed is not None:
                try:
                    self.on_proposed(window, conflicts)
                except Exception:
                    pass  # notification is best effort; the proposal is stored
            return _ok(window=_public(window, tz), conflicts=[_public(c, tz) for c in conflicts],
                       note="PENDING: the owner must approve this window before it opens. Do not retry the "
                            "blocked work until window_next shows it open.")
        except (ValueError, StoreError) as exc:
            return _err(str(exc))
        except Exception as exc:  # never raise into the agent loop
            return _err(f"window_propose failed: {type(exc).__name__}")

    def list(self, args: dict[str, Any], **_: Any) -> str:
        try:
            tz = self.tz()
            now = int(self.clock())
            days = max(1, min(int(args.get("days") or 7), 31))
            rows = self.store.list_windows(now - 3600, now + days * 86400, scope=(args.get("scope") or None))
            return _ok(windows=[_public(w, tz) for w in rows])
        except Exception as exc:
            return _err(f"window_list failed: {type(exc).__name__}: {exc}")

    def next(self, args: dict[str, Any], **_: Any) -> str:
        try:
            tz = self.tz()
            now = int(self.clock())
            scope = str(args.get("scope") or "").strip()
            open_now = self.store.open_windows(scope, now)
            nxt = self.store.next_window(scope, now)
            pending = self.store.list_windows(now, now + 31 * 86400, scope=scope, states=("pending",))
            return _ok(scope=scope, open=[_public(w, tz) for w in open_now],
                       next=_public(nxt, tz) if nxt and not open_now else None,
                       pending=[_public(w, tz) for w in pending])
        except Exception as exc:
            return _err(f"window_next failed: {type(exc).__name__}: {exc}")

    def close(self, args: dict[str, Any], **_: Any) -> str:
        try:
            window = self.store.close(str(args.get("id") or ""), by=self.actor(),
                                      reason=str(args.get("reason") or "")[:500], now=int(self.clock()))
            return _ok(window=_public(window, self.tz()))
        except (StoreError, ValueError) as exc:
            return _err(str(exc))
        except Exception as exc:
            return _err(f"window_close failed: {type(exc).__name__}")
