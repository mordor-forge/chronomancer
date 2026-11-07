"""What the scheduler does for a due effect. No Telegram imports, so it is tested without Telegram.

Effects are rows in the store, leased by ``Store.claim_due_effects`` (at least once, at most
``MAX_ATTEMPTS`` attempts, ``LEASE_S`` per attempt):

- ``announce`` (queued by ``Store.propose``): the proposal message with Approve / Decline.
- ``start`` (queued by an approval): release the linked Kanban cards, then the "open" notice.
- ``end`` (queued by an approval): the "ended" notice, unless the window was closed early.

Every run re-reads the window and each card first, so a retry after a crash does not unblock or
comment twice. ``Retry`` means a transient failure (kanban tools not loaded, a board error): the
effect stays unfinished and the next lease tries again. Permanent problems (an unknown card, a
triage card) are reported in the notice instead.

Kanban comments carry only the window id, the scope and the end time. The window's name and
reason are agent-written and comments reach future workers' prompts, so they stay out.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .store import Store, Window

logger = logging.getLogger(__name__)

ANNOUNCE, START, END = "announce", "start", "end"
KINDS = (ANNOUNCE, START, END)
LEASE_S = 120
MAX_ATTEMPTS = 3
RELEASABLE = ("blocked", "scheduled")         # what kanban_unblock accepts
ELIGIBLE = ("ready", "todo", "review", "running")
MARK = "change-windows"


class Retry(Exception):
    """A transient failure: leave the effect unfinished so the lease retries it."""


@dataclass
class Outcome:
    result: str                                   # stored in effects.result
    text: Optional[str] = None                    # message to the owner chat; None sends nothing
    buttons: list[list[tuple[str, str]]] = field(default_factory=list)


@dataclass
class CardResult:
    task_id: str
    released: bool        # True when the card is eligible now (released by us or already eligible)
    note: str             # "ready", "todo (parents open)", "not found", ...


def _parse(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        out = json.loads(raw)
    except (TypeError, ValueError):
        return {"error": "unreadable tool result"}
    return out if isinstance(out, dict) else {"error": "unexpected tool result"}


def _status_note(status: str) -> str:
    return "todo (parents still open)" if status == "todo" else status


class Release:
    """Card release through the kanban tools (``ctx.dispatch_tool``)."""

    def __init__(self, dispatch: Optional[Callable[[str, dict], Any]]):
        self.dispatch = dispatch

    def _call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if self.dispatch is None:
            raise Retry("kanban tools are not available in this process")
        return _parse(self.dispatch(tool, args))

    def card(self, window: Window, card: dict[str, str], *, until: str) -> CardResult:
        task_id = str(card.get("task_id") or "")
        base: dict[str, Any] = {"task_id": task_id}
        if card.get("board"):
            base["board"] = card["board"]
        shown = self._call("kanban_show", base)
        if "error" in shown:
            error = str(shown["error"])
            if "not found" in error:
                return CardResult(task_id, False, "not found")
            raise Retry(f"kanban_show {task_id}: {error[:160]}")
        task = shown.get("task") if isinstance(shown.get("task"), dict) else {}
        status = str(task.get("status") or "")
        if status in RELEASABLE:
            out = self._call("kanban_unblock", base)
            if "error" in out:
                raise Retry(f"kanban_unblock {task_id}: {str(out['error'])[:160]}")
            status = str(out.get("status") or "ready")
        elif status == "triage":
            return CardResult(task_id, False, "triage cards cannot be released")
        elif status not in ELIGIBLE:
            return CardResult(task_id, False, f"card is {status or 'unknown'}")
        marker = f"{MARK}: window {window.id}"
        comments = shown.get("comments") if isinstance(shown.get("comments"), list) else []
        if not any(marker in str(c.get("body", "")) for c in comments if isinstance(c, dict)):
            body = (f"{marker} for scope {window.scope} is open until {until}. "
                    "This card is eligible now.")
            out = self._call("kanban_comment", {**base, "body": body})
            if "error" in out:  # the release stands; the comment is a courtesy
                logger.warning("change-windows: could not comment on %s: %s", task_id, str(out["error"])[:160])
        return CardResult(task_id, True, _status_note(status))


class Effects:
    def __init__(self, store: Store, actions: Any, *, dispatch: Optional[Callable[[str, dict], Any]] = None):
        self.store = store
        self.actions = actions  # tg.Actions: formatting and the owner's buttons
        self.release = Release(dispatch)

    def _until(self, w: Window) -> str:
        tz = self.actions.tz() or timezone.utc
        return datetime.fromtimestamp(w.end_ts, tz).isoformat(timespec="minutes")

    def run(self, window_id: str, kind: str, now: int) -> Outcome:
        """Carry out one claimed effect. Raises ``Retry`` (or anything else) to leave it unfinished."""
        w = self.store.get(window_id)
        if w is None:
            return Outcome("skipped: unknown window")
        if kind == ANNOUNCE:
            return self._announce(w, now)
        if kind == START:
            return self._start(w, now)
        if kind == END:
            return self._end(w)
        return Outcome(f"skipped: unknown kind {kind}")

    def _announce(self, w: Window, now: int) -> Outcome:
        if w.state != "pending":
            return Outcome(f"skipped: {w.state}")
        if w.end_ts <= now:
            return Outcome("skipped: ended while pending")
        text, buttons = self.actions.window_message(w, now)
        return Outcome("sent", text, buttons)

    def _start(self, w: Window, now: int) -> Outcome:
        if w.state != "approved" or w.closed_at is not None:
            return Outcome(f"skipped: {w.state}")
        head = f"{w.id} · scope {w.scope} · {self.actions.span(w)}"
        if now >= w.end_ts:
            cards = ", ".join(c.get("task_id", "?") for c in w.cards)
            text = (f"Window missed: {w.name}\n{head}\nIt ended before the scheduler could open it "
                    "(was the gateway down?).") + (f"\nCards not released: {cards}" if cards else "")
            return Outcome("missed: ended before the start ran", text)
        if w.presence and w.checked_in_at is None:
            return Outcome("skipped: waiting for check-in")
        results = [self.release.card(w, c, until=self._until(w)) for c in w.cards]
        lines = [f"Window OPEN: {w.name}", head,
                 f"Matched calls for {w.scope} are allowed until {self.actions.fmt(w.end_ts)}."]
        eligible = [f"{r.task_id} {r.note}" for r in results if r.released]
        missing = [f"{r.task_id} ({r.note})" for r in results if not r.released]
        if eligible:
            lines.append("Cards eligible: " + ", ".join(eligible))
        if missing:
            lines.append("Not released: " + ", ".join(missing))
        result = "opened" + (f"; eligible: {', '.join(eligible)}" if eligible else "") + \
                 (f"; not released: {', '.join(missing)}" if missing else "")
        return Outcome(result, "\n".join(lines), [[("Close now", f"cw:close:{w.id}")]])

    def _end(self, w: Window) -> Outcome:
        if w.state != "approved" or w.closed_at is not None:
            return Outcome(f"skipped: {w.state}")
        return Outcome("notified", f"Window ended: {w.name}\n{w.id} · scope {w.scope} · {self.actions.span(w)}\n"
                                   f"Matched calls for {w.scope} are blocked again.")

    @staticmethod
    def failure_message(window_id: str, kind: str, attempts: int, last: Optional[str]) -> str:
        what = {ANNOUNCE: "send the proposal message for", START: "open (release the cards of)",
                END: "send the end notice for"}.get(kind, f"run {kind} for")
        return (f"change-windows could not {what} {window_id} after {attempts} attempts.\n"
                f"Last error: {last or 'unknown'}")
