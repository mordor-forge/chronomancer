"""Telegram layer: proposal messages, Approve / Decline / Close buttons, and ``/windows``.

Two parts:

- ``Actions``: who may act, what a button does, what a message says. It imports nothing from
  python-telegram-bot, so it is tested without Telegram.
- ``Runtime``: the PTB wiring, one per profile in this process (keyed by the plugin data dir).
  The Telegram adapter calls the factory on every connect and again when it rebuilds its
  ``Application``. Each call registers the handlers on that ``Application`` and rebinds the bot;
  only the first call starts the scheduler task.

The scheduler sends the proposal messages. Any process can propose (gateway chat, CLI chat, a
Kanban worker), so ``Store.propose`` queues an ``announce`` effect and the gateway's scheduler
sends it. Delivery is at least once: a crash between the send and ``finish_effect`` repeats it.

Messages are plain text (no parse mode): names and reasons are agent-written and must not become
markup. Only Telegram user ids in ``approver_telegram_ids`` can approve, decline or close, and
only they get an answer to ``/windows``. The plugin's handlers run before the core handlers, so
the core user allow-list does not protect them; this check does.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
import weakref
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from .store import Store, StoreError, Window

logger = logging.getLogger(__name__)

CALLBACK_PATTERN = r"^cw:"  # scoped, so the core button flows keep working
_CALLBACK_RE = re.compile(r"cw:(approve|decline|close):(W[0-9A-F]{6})")  # used with fullmatch
ANNOUNCE = "announce"
ANNOUNCE_LEASE_S = 120
SCHEDULER_INTERVAL_S = 10.0
_MAX_TEXT = 3500  # Telegram's limit is 4096; keep room for the header


def _ids(value: Any) -> list[int]:
    """Telegram user or chat ids from a setting: ints, or strings of digits (an optional minus).
    0 means unset (no Telegram user or chat has id 0)."""
    if value is None or value == "" or value == 0:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    out: list[int] = []
    for item in items:
        if isinstance(item, bool):
            continue
        if isinstance(item, int):
            if item != 0:
                out.append(item)
        elif isinstance(item, str) and re.fullmatch(r"-?\d{1,20}", item.strip()):
            if int(item.strip()) != 0:
                out.append(int(item.strip()))
        else:
            logger.warning("change-windows: ignoring a Telegram id that is not a number")
    return out


@dataclass
class Reply:
    """What the bot does after a button press."""

    toast: str                      # answerCallbackQuery text (Telegram shows up to 200 characters)
    alert: bool = False             # show it as a dialog, not a toast
    text: Optional[str] = None      # new message text; None leaves the message as it is
    buttons: list[list[tuple[str, str]]] = field(default_factory=list)  # rows of (label, callback data)


class Actions:
    def __init__(self, store: Store, *, setting: Callable[..., Any], tz: Callable[[], Any],
                 clock: Callable[[], float] = time.time):
        self.store = store
        self.setting = setting
        self.tz = tz
        self.clock = clock

    # -- who ------------------------------------------------------------------------------------
    def approvers(self) -> frozenset[int]:
        return frozenset(_ids(self.setting("approver_telegram_ids", [])))

    def is_approver(self, user_id: Any) -> bool:
        return isinstance(user_id, int) and not isinstance(user_id, bool) and user_id in self.approvers()

    def owner_chat(self) -> Optional[int]:
        """Chat for proactive messages: ``owner_chat_id``, else the first approver's private chat."""
        chat = _ids(self.setting("owner_chat_id", None))
        if chat:
            return chat[0]
        approvers = _ids(self.setting("approver_telegram_ids", []))
        return approvers[0] if approvers else None

    # -- what a message says ----------------------------------------------------------------------
    def _fmt(self, ts: int, *, time_only: bool = False) -> str:
        dt = datetime.fromtimestamp(ts, self.tz() or timezone.utc)
        return dt.strftime("%H:%M" if time_only else "%a %d %b %H:%M")

    def _span(self, w: Window) -> str:
        tz = self.tz() or timezone.utc
        same_day = (datetime.fromtimestamp(w.start_ts, tz).date() == datetime.fromtimestamp(w.end_ts, tz).date())
        end = self._fmt(w.end_ts, time_only=same_day)
        zone = datetime.fromtimestamp(w.start_ts, tz).strftime("%Z")
        return f"{self._fmt(w.start_ts)}-{end} {zone}".rstrip()

    def _overlaps(self, w: Window) -> list[Window]:
        return self.store.conflicts(w.scope, w.start_ts, w.end_ts, w.exclusive, exclude_id=w.id)

    def window_message(self, w: Window, now: int) -> tuple[str, list[list[tuple[str, str]]]]:
        """Text and buttons for one window, from its current state."""
        if w.state == "approved" and w.closed_at is None:
            head = "Window OPEN" if w.is_open(now) else ("Window ended" if now >= w.end_ts else "Window approved")
        else:
            head = {"pending": "Window proposal", "declined": "Window declined", "cancelled": "Proposal withdrawn",
                    "closed": "Window closed"}.get(w.state, f"Window {w.state}")
        lines = [f"{head}: {w.name}", f"{w.id} · scope {w.scope}", self._span(w),
                 "exclusive" if w.exclusive else "shared (other windows may overlap)"]
        if w.cards:
            lines.append("Cards: " + ", ".join(c.get("task_id", "?") for c in w.cards))
        lines.append(f"Proposed by {w.proposed_by}")
        lines.append(f"Reason: {w.reason}")
        if w.state in ("pending", "approved") and w.closed_at is None:
            overlaps = self._overlaps(w)
            if overlaps:
                lines.append("OVERLAPS: " + ", ".join(f"{o.id} ({o.state}, {o.name})" for o in overlaps))
        if w.decided_by and w.state != "pending":
            lines.append(f"Decided by {w.decided_by}")
        if w.close_reason:
            lines.append(f"Closed: {w.close_reason}")
        text = "\n".join(lines)
        if len(text) > _MAX_TEXT:
            text = text[:_MAX_TEXT] + " [...]"
        buttons: list[list[tuple[str, str]]] = []
        if w.state == "pending" and w.end_ts > now:
            buttons = [[("Approve", f"cw:approve:{w.id}"), ("Decline", f"cw:decline:{w.id}")]]
        elif w.state == "approved" and w.closed_at is None and w.end_ts > now:
            buttons = [[("Close now", f"cw:close:{w.id}")]]
        return text, buttons

    def windows_text(self) -> str:
        """``/windows``: today's and tomorrow's windows in the plugin's time zone."""
        now = int(self.clock())
        tz = self.tz() or timezone.utc
        today = datetime.fromtimestamp(now, tz).replace(hour=0, minute=0, second=0, microsecond=0)
        start, end = int(today.timestamp()), int((today + timedelta(days=2)).timestamp())
        rows = self.store.list_windows(start, end)
        if not rows:
            return "No change windows today or tomorrow."
        lines = [f"Change windows, today and tomorrow ({today.strftime('%Z') or 'UTC'}):"]
        for w in rows:
            state = "open" if w.is_open(now) else w.state
            lines.append(f"{w.id} {state} · {self._span(w)} · {w.scope} · {w.name}")
            if w.state in ("pending", "approved") and w.closed_at is None:
                overlaps = self._overlaps(w)
                if overlaps:
                    lines.append("   overlaps " + ", ".join(o.id for o in overlaps))
        text = "\n".join(lines)
        return text if len(text) <= _MAX_TEXT else text[:_MAX_TEXT] + " [...]"

    # -- what a button does -----------------------------------------------------------------------
    def on_button(self, user_id: Any, data: Any) -> Reply:
        match = _CALLBACK_RE.fullmatch(data if isinstance(data, str) else "")
        if not match:
            return Reply("Unknown change-windows action.", alert=True)
        if not self.is_approver(user_id):
            return Reply("Only an approver can do this (setting approver_telegram_ids).", alert=True)
        action, window_id = match.groups()
        now = int(self.clock())
        by = f"telegram:{user_id}"
        try:
            if action == "approve":
                current = self.store.get(window_id)
                if current is not None and current.state == "pending" and current.end_ts <= now:
                    raise StoreError(f"window {window_id} has already ended; ask for a new one")
                window = self.store.decide(window_id, approve=True, by=by, now=now)
                toast = "Approved."
            elif action == "decline":
                window = self.store.decide(window_id, approve=False, by=by, now=now)
                toast = "Declined."
            else:
                window = self.store.close(window_id, by=by, reason="closed from Telegram", now=now)
                toast = "Closed. Matched calls are blocked again." if window.state == "closed" else "Withdrawn."
        except StoreError as exc:
            current = self.store.get(window_id)
            if current is None:
                return Reply(str(exc)[:200], alert=True)
            text, buttons = self.window_message(current, now)
            return Reply(str(exc)[:200], alert=True, text=text, buttons=buttons)
        text, buttons = self.window_message(window, now)
        return Reply(toast, text=text, buttons=buttons)


# -- PTB wiring -------------------------------------------------------------------------------------

def _markup(rows: list[list[tuple[str, str]]]) -> Any:
    if not rows:
        return None
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=data) for label, data in row]
                                 for row in rows])


class Runtime:
    """One per profile in this process: the current Application, the handlers, the scheduler task."""

    def __init__(self, actions: Actions, *, interval_s: float = SCHEDULER_INTERVAL_S,
                 clock: Optional[Callable[[], float]] = None):
        self.actions = actions
        self.interval_s = interval_s
        self.clock = clock or actions.clock
        self.app: Any = None
        self.task: Optional[asyncio.Task] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._wake: Optional[asyncio.Event] = None
        self._wired: "weakref.WeakSet[Any]" = weakref.WeakSet()
        self._warned_no_chat = False
        self.binds = 0

    # -- factory side ----------------------------------------------------------------------------
    def bind(self, app: Any) -> None:
        """Register the handlers on ``app`` (once per Application) and make it the one we send with."""
        from telegram.ext import CallbackQueryHandler, CommandHandler

        if app not in self._wired:
            app.add_handler(CallbackQueryHandler(self._on_button, pattern=CALLBACK_PATTERN))
            app.add_handler(CommandHandler("windows", self._on_windows))
            self._wired.add(app)
        self.app = app
        self.binds += 1

    def ensure_scheduler(self, spawn: Callable[..., Any]) -> bool:
        """Start the scheduler unless one is running. Needs a running event loop (the adapter's)."""
        loop = asyncio.get_running_loop()
        if self.task is not None and not self.task.done() and self.loop is loop:
            return False
        self.loop = loop
        self._wake = asyncio.Event()
        self.task = spawn(self.run(), name="change-windows:scheduler")
        return True

    def wake(self) -> None:
        """Run a scheduler pass soon. Safe from any thread (a tool handler calls it)."""
        loop, event = self.loop, self._wake
        if loop is None or event is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:
            pass  # the loop closed between the check and the call

    def ready(self) -> bool:
        """Sends wait until the adapter has started the Application."""
        return self.app is not None and bool(getattr(self.app, "running", False))

    # -- scheduler -------------------------------------------------------------------------------
    async def run(self) -> None:
        assert self._wake is not None
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("change-windows: scheduler pass failed: %s", type(exc).__name__)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.interval_s)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def tick(self) -> int:
        """One pass: send due proposal messages. Returns how many were sent."""
        if not self.ready():
            return 0
        chat = await asyncio.to_thread(self.actions.owner_chat)
        if chat is None:
            if not self._warned_no_chat:
                logger.warning("change-windows: no approver_telegram_ids or owner_chat_id set; "
                               "proposals are not sent to Telegram")
                self._warned_no_chat = True
            return 0
        store = self.actions.store
        now = int(self.clock())
        claimed = await asyncio.to_thread(store.claim_due_effects, now, lease_s=ANNOUNCE_LEASE_S, kinds=(ANNOUNCE,))
        sent = 0
        for window_id, kind, _due in claimed:
            try:
                window = await asyncio.to_thread(store.get, window_id)
                if window is None or window.state != "pending":
                    await asyncio.to_thread(store.finish_effect, window_id, kind,
                                            f"skipped: {window.state if window else 'gone'}", now=now)
                    continue
                text, buttons = await asyncio.to_thread(self.actions.window_message, window, now)
                await self.app.bot.send_message(chat_id=chat, text=text, reply_markup=_markup(buttons))
                await asyncio.to_thread(store.finish_effect, window_id, kind, "sent", now=int(self.clock()))
                sent += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # the lease expires and the next pass retries (at most 3 attempts)
                logger.warning("change-windows: could not announce %s: %s", window_id, type(exc).__name__)
        return sent

    # -- handlers --------------------------------------------------------------------------------
    async def _on_button(self, update: Any, context: Any) -> None:
        query = getattr(update, "callback_query", None)
        if query is None:
            return
        user_id = query.from_user.id if query.from_user else None
        reply = await asyncio.to_thread(self.actions.on_button, user_id, query.data)
        await query.answer(reply.toast[:200], show_alert=reply.alert)
        if reply.text is not None and query.message is not None:
            try:
                await query.edit_message_text(reply.text, reply_markup=_markup(reply.buttons))
            except Exception as exc:  # "message is not modified", or an old message: the store is right
                logger.debug("change-windows: could not edit the message: %s", type(exc).__name__)

    async def _on_windows(self, update: Any, context: Any) -> None:
        user = getattr(update, "effective_user", None)
        message = getattr(update, "effective_message", None)
        if user is None or message is None:
            return
        if not await asyncio.to_thread(self.actions.is_approver, user.id):
            return  # say nothing to people who are not approvers
        await message.reply_text(await asyncio.to_thread(self.actions.windows_text))


# -- one runtime per profile in this process ----------------------------------------------------------

_RUNTIMES: dict[str, Runtime] = {}
_REGISTRY_LOCK = threading.Lock()


def runtime_for(key: str, make: Callable[[], Runtime]) -> Runtime:
    with _REGISTRY_LOCK:
        runtime = _RUNTIMES.get(key)
        if runtime is None:
            runtime = _RUNTIMES[key] = make()
        return runtime


def wake_all() -> None:
    """A proposal was stored in this process: let the scheduler send it now, not at the next pass."""
    with _REGISTRY_LOCK:
        runtimes = list(_RUNTIMES.values())
    for runtime in runtimes:
        runtime.wake()


def make_factory(*, key: Callable[[], str], actions: Actions,
                 spawn: Callable[..., Any]) -> Callable[[Any, Any], None]:
    """The ``register_platform_handler("telegram", ...)`` factory. ``key`` names the profile
    (the plugin data dir); ``spawn`` is ``ctx.spawn_task``."""

    def change_windows_telegram(native: Any, adapter: Any = None) -> None:
        if native is None:
            return
        runtime = runtime_for(key(), lambda: Runtime(actions))
        runtime.bind(native)
        try:
            if runtime.ensure_scheduler(spawn):
                logger.info("change-windows: Telegram handlers and scheduler started")
        except RuntimeError as exc:  # no running loop: buttons work, proposals are not sent
            logger.warning("change-windows: scheduler not started (%s); proposals are not sent to Telegram", exc)

    return change_windows_telegram
