"""Titles of linked Kanban cards, read through the public plugin API (``ctx.dispatch_tool`` with
``kanban_show``), never through the board's files or core internals.

The plugin entry sets the dispatcher once per process. Without it (a process that loaded only the
Desktop backend, the tests) every lookup answers None and the surfaces show the card id alone.
Answers are cached for a short time: the owner surfaces poll every few seconds. Titles are
user- or agent-written text: surfaces must show them as plain text.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import unicodedata
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

TTL_S = 30.0
_TITLE_MAX = 120


def _text(value: Any, limit: int) -> str:
    # Control and format characters (line breaks, bidi overrides, zero-width marks) become spaces.
    raw = "".join(" " if unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") else ch for ch in str(value or ""))
    text = " ".join(raw.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


class CardReader:
    def __init__(self, dispatch: Optional[Callable[[str, dict], Any]] = None, *, ttl_s: float = TTL_S,
                 clock: Callable[[], float] = time.monotonic):
        self.dispatch = dispatch
        self.ttl_s = ttl_s
        self.clock = clock
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str], tuple[float, Optional[dict[str, str]]]] = {}

    def info(self, card: Any) -> Optional[dict[str, str]]:
        """``{"task_id", "title", "status", "assignee"}`` for a linked card, or None when the board
        cannot be read or does not know the card. Never raises."""
        if self.dispatch is None or not isinstance(card, dict):
            return None
        task_id, board = str(card.get("task_id") or ""), str(card.get("board") or "")
        if not task_id:
            return None
        key, now = (board, task_id), self.clock()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < self.ttl_s:
                return hit[1]
        found = self._read(task_id, board)
        with self._lock:
            if len(self._cache) > 500:
                self._cache.clear()
            self._cache[key] = (now, found)
        return found

    def _read(self, task_id: str, board: str) -> Optional[dict[str, str]]:
        args = {"task_id": task_id, **({"board": board} if board else {})}
        try:
            raw = self.dispatch("kanban_show", args)  # type: ignore[misc]
            shown = raw if isinstance(raw, dict) else json.loads(raw)
            task = shown.get("task") if isinstance(shown, dict) else None
            if not isinstance(task, dict) or not task.get("title"):
                return None
            return {"task_id": task_id, "title": _text(task.get("title"), _TITLE_MAX),
                    "status": _text(task.get("status"), 24), "assignee": _text(task.get("assignee"), 48)}
        except Exception as exc:  # the kanban tools are not loaded, the board is locked, ...
            logger.debug("change-windows: could not read card %s (%s)", task_id, type(exc).__name__)
            return None


_reader = CardReader()


def configure(dispatch: Optional[Callable[[str, dict], Any]]) -> None:
    """Called by the plugin entry: from now on this process reads cards with ``dispatch``."""
    global _reader
    _reader = CardReader(dispatch)


def details(cards: Any) -> list[dict[str, str]]:
    """Details for the linked cards the board knows, in the order they are linked."""
    return [info for info in (_reader.info(card) for card in cards or []) if info]
