"""The Kanban board, read and written only through the public kanban tools.

``dispatch(tool, args)`` is ``ctx.dispatch_tool`` of the plugin entry. The board's files and the
core's Python modules are never touched. Without a dispatcher (a process that loaded only the
Desktop backend, the tests) every call raises ``BoardError``.

Card titles are user- or agent-written text. They are cleaned of control and format characters
here; surfaces must still show them as plain text.
"""
from __future__ import annotations

import json
import threading
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable, Optional

OPEN_STATUSES = ("triage", "todo", "scheduled", "ready", "running", "blocked", "review")
CLOSED_STATUSES = ("done", "archived")
PARKABLE = ("todo", "ready")              # dispatchable (or about to be): a future slot must park them
RELEASABLE = ("blocked", "scheduled")     # what kanban_unblock accepts
LIST_LIMIT = 200                          # the most kanban_list returns in one call
_TITLE_MAX = 200


class BoardError(Exception):
    """The board could not be read or written now (tools not loaded, board locked, tool error)."""


@dataclass(frozen=True)
class Card:
    task_id: str
    title: str
    status: str
    assignee: str
    priority: int = 0
    tenant: str = ""
    project: str = ""
    model: str = ""
    created_at: Optional[int] = None
    started_at: Optional[int] = None
    completed_at: Optional[int] = None


def clean(value: Any, limit: int) -> str:
    """One line of plain text: control and format characters (line breaks, bidi overrides,
    zero-width marks) become spaces."""
    raw = "".join(" " if unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") else ch for ch in str(value or ""))
    text = " ".join(raw.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _int(value: Any) -> Optional[int]:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _card(row: dict[str, Any]) -> Card:
    return Card(task_id=str(row.get("id") or ""), title=clean(row.get("title"), _TITLE_MAX),
                status=clean(row.get("status"), 24), assignee=clean(row.get("assignee"), 48),
                priority=_int(row.get("priority")) or 0, tenant=clean(row.get("tenant"), 48),
                project=clean(row.get("project_id"), 48), model=clean(row.get("model_override"), 80),
                created_at=_int(row.get("created_at")), started_at=_int(row.get("started_at")),
                completed_at=_int(row.get("completed_at")))


class Board:
    def __init__(self, dispatch: Optional[Callable[[str, dict], Any]], *, list_ttl_s: float = 8.0,
                 card_ttl_s: float = 30.0, closed_ttl_s: float = 3600.0, clock: Callable[[], float] = time.monotonic):
        self.dispatch = dispatch
        self.list_ttl_s, self.card_ttl_s, self.closed_ttl_s, self.clock = list_ttl_s, card_ttl_s, closed_ttl_s, clock
        self._lock = threading.Lock()
        self._lists: dict[str, tuple[float, list[Card]]] = {}
        self._cards: dict[tuple[str, str], tuple[float, Optional[Card]]] = {}

    @property
    def available(self) -> bool:
        return self.dispatch is not None

    def _call(self, tool: str, args: dict[str, Any], board: str) -> dict[str, Any]:
        if self.dispatch is None:
            raise BoardError("The Kanban tools are not loaded in this process.")
        try:
            raw = self.dispatch(tool, {**args, **({"board": board} if board else {})})
            out = raw if isinstance(raw, dict) else json.loads(raw)
        except BoardError:
            raise
        except Exception as exc:
            raise BoardError(f"{tool} failed ({type(exc).__name__}).") from exc
        if not isinstance(out, dict):
            raise BoardError(f"{tool} returned an unexpected result.")
        return out

    def forget(self) -> None:
        """Drop cached reads, after a write or when a fresh picture is needed."""
        with self._lock:
            self._lists.clear()
            self._cards = {k: v for k, v in self._cards.items() if v[1] and v[1].status in CLOSED_STATUSES}

    def cards(self, board: str) -> list[Card]:
        """Every open card on the board (all statuses but done and archived)."""
        now = self.clock()
        with self._lock:
            hit = self._lists.get(board)
            if hit and now - hit[0] < self.list_ttl_s:
                return hit[1]
        found: list[Card] = []
        for status in OPEN_STATUSES:
            out = self._call("kanban_list", {"status": status, "limit": LIST_LIMIT}, board)
            if "error" in out:
                raise BoardError(f"kanban_list: {clean(out['error'], 160)}")
            found += [_card(row) for row in out.get("tasks") or [] if isinstance(row, dict) and row.get("id")]
        with self._lock:
            self._lists[board] = (now, found)
            for card in found:
                self._cards[(board, card.task_id)] = (now, card)
        return found

    def card(self, board: str, task_id: str, *, fresh: bool = False) -> Optional[Card]:
        """One card, or None when the board does not know it. Done cards are found here too."""
        key, now = (board, task_id), self.clock()
        if not fresh:
            with self._lock:
                hit = self._cards.get(key)
                closed = bool(hit and hit[1] and hit[1].status in CLOSED_STATUSES)  # a closed card does not change
                if hit and now - hit[0] < (self.closed_ttl_s if closed else self.card_ttl_s):
                    return hit[1]
        out = self._call("kanban_show", {"task_id": task_id}, board)
        if "error" in out:
            error = str(out["error"])
            if "not found" not in error:  # anything else ("Unknown tool", a locked board) is not an answer
                raise BoardError(f"kanban_show {task_id}: {clean(error, 160)}")
            found = None
        else:
            task = out.get("task")
            found = _card(task) if isinstance(task, dict) and task.get("id") else None
        with self._lock:
            if len(self._cards) > 2000:
                self._cards.clear()
            self._cards[key] = (now, found)
        return found

    def park(self, board: str, task_id: str, reason: str) -> str:
        """Move a todo or ready card to ``scheduled``, so the dispatcher leaves it alone."""
        out = self._call("kanban_schedule", {"task_id": task_id, "reason": reason}, board)
        self.forget()
        if "error" in out:
            raise BoardError(f"kanban_schedule {task_id}: {clean(out['error'], 160)}")
        return clean(out.get("status") or "scheduled", 24)

    def release(self, board: str, task_id: str) -> str:
        """Return a blocked or scheduled card to ``ready`` (``todo`` while its parents are open)."""
        out = self._call("kanban_unblock", {"task_id": task_id}, board)
        self.forget()
        if "error" in out:
            raise BoardError(f"kanban_unblock {task_id}: {clean(out['error'], 160)}")
        return clean(out.get("status") or "ready", 24)

    def comment(self, board: str, task_id: str, body: str) -> None:
        out = self._call("kanban_comment", {"task_id": task_id, "body": body}, board)
        if "error" in out:
            raise BoardError(f"kanban_comment {task_id}: {clean(out['error'], 160)}")
