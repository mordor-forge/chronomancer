"""The plan of one Kanban board: slots for its cards, and the moves that change them.

The plan advises and does not enforce (docs/adr/0001). A move is refused only when it cannot be
carried out (the card is running, the card is done, the slot is pinned by the owner). A clash is
reported with the result and never refuses a move.

What a slot does to its card:

- A ``todo`` or ``ready`` card would start by itself, so a future slot parks it (``kanban_schedule``).
- A ``blocked`` or ``scheduled`` card already waits, so it is left as it is.
- At the slot's start the timer releases the card (``kanban_unblock``). From then on the Kanban
  dispatcher owns it, as for any other card.

Actors: ``owner`` (the Desktop page), ``agent:<profile>`` (the tools) and ``timer``.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

from . import clashes
from .board import CLOSED_STATUSES, PARKABLE, RELEASABLE, Board, BoardError, Card, clean
from .clashes import Clash, Span
from .store import FAILED, MISSED, PLANNED, RELEASED, Label, LogEntry, Slot, Store

logger = logging.getLogger(__name__)

MARK = "chronomancer"
OWNER, TIMER = "owner", "timer"
MIN_ESTIMATE_S, MAX_ESTIMATE_S = 60, 14 * 86400
DEFAULT_ESTIMATE_MIN = 60
PAST_TOLERANCE_S = 120      # a start this little in the past still means "now"
LEASE_S = 60                # one release attempt owns its slot this long
MAX_ATTEMPTS = 5
MIN_GRACE_S = 15 * 60       # a slot is still released this long after its start, or for its whole estimate
KEEP_S = 30 * 86400         # finished slots and log entries are kept this long
SHOWN_S = 8 * 86400         # finished slots older than this are on no horizon and are not read
MAX_LABELS, MAX_CARD_LABELS = 200, 12
_TASK_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_LABEL = re.compile(r"[a-z0-9][a-z0-9._/-]{0,31}(:[a-z0-9][a-z0-9._/-]{0,31})?")

# Phases: what the plan says about one card right now.
PLANNED_, DUE, WAITING, RUNNING, DONE = "planned", "due", "waiting", "running", "done"
MISSED_, FAILED_, UNPLANNED, GONE = "missed", "failed", "unplanned", "gone"
ACTIVE = (PLANNED_, DUE, WAITING, RUNNING)   # phases that take time in the plan, so they can clash


class PlanError(Exception):
    """A move that cannot be carried out. The message is a sentence for whoever asked."""


@dataclass(frozen=True)
class Item:
    """One card as the plan sees it: the card, its slot when it has one, and where it is in time."""
    task_id: str
    card: Optional[Card]
    slot: Optional[Slot]
    labels: tuple
    phase: str
    start: Optional[int]      # where its bar begins: the planned start, or the real start once it runs
    end: Optional[int]
    overrun_s: int = 0        # how far a running card is past its estimate

    @property
    def title(self) -> str:
        return self.card.title if self.card and self.card.title else self.task_id

    @property
    def assignee(self) -> str:
        return self.card.assignee if self.card else ""


@dataclass
class Snapshot:
    """The whole plan of one board at one moment."""
    now: int
    board: str
    items: list[Item]
    labels: list[Label]
    spans: list[Span]
    clashes: list[Clash]
    board_error: str = ""     # set when the board could not be read: cards are missing from ``items``

    def item(self, task_id: str) -> Optional[Item]:
        return next((i for i in self.items if i.task_id == task_id), None)

    def clashes_of(self, task_id: str) -> list[Clash]:
        return [c for c in self.clashes if c.involves(task_id)]


@dataclass
class Change:
    """The result of one move."""
    action: str                         # plan, move, now, clear, pin, unpin, preview
    task_id: str
    card: Optional[Card] = None
    slot: Optional[Slot] = None
    previous: Optional[Slot] = None
    parked: bool = False                # this move parked the card
    released: str = ""                  # the card's status after this move released it
    clashes: list[Clash] = field(default_factory=list)
    next_free: Optional[int] = None     # an earlier clash-free start does not exist; this is the next one
    note: str = ""


class Planner:
    def __init__(self, store: Store, board: Board, *, setting: Callable[..., Any], tz: Callable[[], Any],
                 clock: Callable[[], float] = time.time, wake: Callable[[], None] = lambda: None,
                 changed: Callable[[], None] = lambda: None):
        self.store, self.board = store, board
        self.setting, self.tz, self.clock, self.wake = setting, tz, clock, wake
        self.changed = changed          # called after every change of the plan, so open pages can read it again
        self._pruned_at = 0

    def _record(self, **entry: Any) -> None:
        """Write one change to the log and tell whoever watches the plan."""
        self.store.add_log(**entry)
        try:
            self.changed()
        except Exception:
            logger.debug("%s: could not announce a change", MARK, exc_info=True)

    # ---- settings and words ----

    def board_name(self, board: Optional[str] = None) -> str:
        """The board a call means: its own argument, else the ``board`` setting, else the active board."""
        return clean(board or self.setting("board", "") or "", 64)

    def default_estimate_s(self) -> int:
        try:
            minutes = int(self.setting("default_estimate_minutes", DEFAULT_ESTIMATE_MIN))
        except (TypeError, ValueError):
            minutes = DEFAULT_ESTIMATE_MIN
        return min(max(minutes * 60, MIN_ESTIMATE_S), MAX_ESTIMATE_S)

    def _zone(self) -> Any:
        return self.tz() or timezone.utc

    def fmt(self, ts: Optional[int]) -> str:
        """A moment in the plugin's time zone: "Wed 11 Nov 14:00"."""
        if ts is None:
            return ""
        return datetime.fromtimestamp(ts, self._zone()).strftime("%a %d %b %H:%M")

    def span_text(self, start: int, end: int) -> str:
        """"Wed 11 Nov 14:00 to 15:30"; the end repeats the day only when it differs."""
        a, b = datetime.fromtimestamp(start, self._zone()), datetime.fromtimestamp(end, self._zone())
        tail = b.strftime("%H:%M") if a.date() == b.date() else b.strftime("%a %d %b %H:%M")
        return f"{a.strftime('%a %d %b %H:%M')} to {tail}"

    def iso(self, ts: int) -> str:
        return datetime.fromtimestamp(ts, self._zone()).isoformat(timespec="minutes")

    @staticmethod
    def who(actor: str) -> str:
        """An actor in words: "the owner", "profile assistant", "the timer"."""
        if actor == OWNER:
            return "the owner"
        if actor == TIMER:
            return "the timer"
        return f"profile {actor.split(':', 1)[1]}" if actor.startswith("agent:") else actor

    def describe(self, clash: Clash, task_id: str) -> str:
        return clashes.describe(clash, task_id, self.span_text)

    # ---- reading the plan ----

    def snapshot(self, board: Optional[str] = None, *, now: Optional[int] = None) -> Snapshot:
        now = int(self.clock() if now is None else now)
        board = self.board_name(board)
        slots = {s.task_id: s for s in self.store.slots(board, since_ts=now - SHOWN_S)}
        labels, card_labels = self.store.labels(), self.store.card_labels(board)
        cards: dict[str, Card] = {}
        error = ""
        try:
            cards = {c.task_id: c for c in self.board.cards(board)}
            for task_id in slots:
                if task_id not in cards:  # a done card, or one that left the board
                    card = self.board.card(board, task_id)
                    if card is not None:
                        cards[task_id] = card
        except BoardError as exc:
            error = str(exc)
        default = self.default_estimate_s()
        items = [self._item(task_id, cards.get(task_id), slots.get(task_id),
                            self._labels(cards.get(task_id), card_labels.get(task_id, ())), now, default, bool(error))
                 for task_id in dict.fromkeys([*slots, *cards])]
        items.sort(key=lambda i: (i.start is None, i.start or 0, -(i.card.priority if i.card else 0), i.task_id))
        locks = frozenset(label.name for label in labels if label.exclusive)
        spans = [Span(i.task_id, i.title, i.assignee, i.start, i.end, locks & frozenset(i.labels))
                 for i in items if i.phase in ACTIVE and i.start is not None and i.end is not None and i.end > i.start]
        return Snapshot(now, board, items, labels, spans, clashes.find(spans), error)

    @staticmethod
    def _labels(card: Optional[Card], own: Iterable[str]) -> tuple:
        """The card's labels, plus its tenant and project from the board when it has them."""
        derived = []
        if card and card.tenant:
            derived.append(f"tenant:{card.tenant.lower()}")
        if card and card.project:
            derived.append(f"project:{card.project.lower()}")
        return tuple(dict.fromkeys([*own, *(d for d in derived if _LABEL.fullmatch(d))]))

    @staticmethod
    def _item(task_id: str, card: Optional[Card], slot: Optional[Slot], labels: tuple, now: int, default_s: int,
              unreadable: bool) -> Item:
        estimate = slot.estimate_s if slot else default_s
        if card is not None and card.status in CLOSED_STATUSES:
            start = card.started_at or (slot.start_ts if slot else card.completed_at)
            end = card.completed_at or start
            return Item(task_id, card, slot, labels, DONE, start, end)
        if card is not None and card.status == "running":
            start = card.started_at or (slot.released_at if slot else None) or (slot.start_ts if slot else now)
            start = min(start, now)
            over = max(0, now - (start + estimate))
            return Item(task_id, card, slot, labels, RUNNING, start, max(now, start + estimate), over)
        if slot is None:
            return Item(task_id, card, None, labels, UNPLANNED, None, None)
        if card is None and not unreadable:
            return Item(task_id, None, slot, labels, GONE, slot.start_ts, slot.end_ts)
        phase = {PLANNED: PLANNED_ if slot.start_ts > now else DUE, RELEASED: WAITING,
                 MISSED: MISSED_, FAILED: FAILED_}[slot.state]
        return Item(task_id, card, slot, labels, phase, slot.start_ts, slot.end_ts)

    def changes_since_last_look(self, actor: str, *, mark: bool = True, limit: int = 20) -> list[LogEntry]:
        """What others changed in the plan since ``actor`` last asked. Asking moves the marker."""
        key = f"seen:{actor}"
        try:
            seen = int(self.store.get_meta(key, "0"))
        except ValueError:
            seen = 0
        entries = [e for e in self.store.log_after(seen, limit=200) if e.actor != actor]
        if mark:
            self.store.set_meta(key, str(self.store.last_log_id()))
        return entries[-limit:]

    # ---- moves ----

    def set_slot(self, task_id: str, start_ts: int, *, estimate_s: Optional[int] = None, actor: str,
                 board: Optional[str] = None, dry_run: bool = False) -> Change:
        """Give a card a slot, or move its slot. ``dry_run`` only reports the clashes."""
        now, board, start_ts = int(self.clock()), self.board_name(board), int(start_ts)
        if start_ts < now - PAST_TOLERANCE_S:
            raise PlanError(f"{self.fmt(start_ts)} is in the past. Give a future start, or run the card now.")
        old = self.store.get_slot(board, self._task_id(task_id))
        estimate = self._estimate(estimate_s, old)
        self._check_pin(old, actor, "move")
        card = self._card(board, task_id)
        self._check_plannable(card)
        start_ts = max(start_ts, now)
        snap = self.snapshot(board, now=now)
        locks = frozenset(l.name for l in snap.labels if l.exclusive)
        item = snap.item(task_id)
        candidate = Span(task_id, card.title, card.assignee, start_ts, start_ts + estimate,
                         locks & frozenset(item.labels if item else ()))
        found = clashes.against(candidate, snap.spans)
        free = clashes.next_free(candidate, snap.spans) if any(c.kind == clashes.CLASH for c in found) else None
        if dry_run:
            return Change("preview", task_id, card, _preview(board, task_id, start_ts, estimate, old, actor, now),
                          old, clashes=found, next_free=free)
        if start_ts <= now:
            change = self.run_now(task_id, actor=actor, board=board, estimate_s=estimate)
            change.clashes, change.next_free = found, free
            return change
        parked = bool(old and old.parked)
        if card.status in PARKABLE:
            self._write(lambda: self.board.park(board, task_id, f"SCHEDULED_UNTIL={self.iso(start_ts)} ({MARK} slot)"))
            parked = True
        try:
            slot = self.store.put_slot(board, task_id, start_ts=start_ts, estimate_s=estimate, state=PLANNED,
                                       parked=parked, set_by=actor, now=now)
        except Exception:
            if card.status in PARKABLE:  # do not leave a card parked that has no slot
                self._quietly(lambda: self.board.release(board, task_id))
            raise
        self._record(ts=now, actor=actor, action="move" if old else "plan", board=board, task_id=task_id,
                           detail={"title": card.title, "start": start_ts, "estimate_s": estimate,
                                   "from": old.start_ts if old else None,
                                   "from_estimate_s": old.estimate_s if old else None,
                                   "clashes": sum(1 for c in found if c.kind == clashes.CLASH)})
        self.wake()
        return Change("move" if old else "plan", task_id, card, slot, old, parked=card.status in PARKABLE,
                      clashes=found, next_free=free)

    def run_now(self, task_id: str, *, actor: str, board: Optional[str] = None,
                estimate_s: Optional[int] = None) -> Change:
        """Release the card at once and move its slot to now."""
        now, board = int(self.clock()), self.board_name(board)
        old = self.store.get_slot(board, self._task_id(task_id))
        self._check_pin(old, actor, "run")
        card = self._card(board, task_id)
        self._check_plannable(card)
        released = self._write(lambda: self.board.release(board, task_id)) if card.status in RELEASABLE else ""
        note = f"released now: {_status_note(released)}" if released else f"the card was already {card.status}"
        slot = self.store.put_slot(board, task_id, start_ts=now, estimate_s=self._estimate(estimate_s, old),
                                   state=RELEASED, parked=False, set_by=actor, now=now, note=note, released_at=now)
        self._record(ts=now, actor=actor, action="now", board=board, task_id=task_id,
                           detail={"title": card.title, "start": now, "from": old.start_ts if old else None,
                                   "note": note})
        if released:
            self._comment(board, task_id, f"{MARK}: released now by {self.who(actor)}.")
        self.wake()
        return Change("now", task_id, card, slot, old, released=released, note=note)

    def clear(self, task_id: str, *, actor: str, board: Optional[str] = None) -> Change:
        """Take the card out of the plan. The card itself is not touched."""
        now, board = int(self.clock()), self.board_name(board)
        old = self.store.get_slot(board, self._task_id(task_id))
        if old is None:
            raise PlanError(f"Card {task_id} has no slot.")
        self._check_pin(old, actor, "clear")
        card = self._quietly(lambda: self.board.card(board, task_id, fresh=True))
        self.store.delete_slot(board, task_id)
        self._record(ts=now, actor=actor, action="clear", board=board, task_id=task_id,
                           detail={"title": card.title if card else task_id, "from": old.start_ts})
        note = ""
        if card and card.status in RELEASABLE:
            note = (f"The card stays {card.status}. It runs when it gets a new slot or when it is run now.")
        elif card and card.status in PARKABLE:
            note = f"The card is {card.status}: the Kanban dispatcher starts it without the plan."
        self.wake()
        return Change("clear", task_id, card, None, old, note=note)

    def pin(self, task_id: str, pinned: bool, *, actor: str, board: Optional[str] = None) -> Change:
        """Pin or unpin a slot. Only the owner can; an agent cannot move, run or clear a pinned slot."""
        if actor != OWNER:
            raise PlanError("Only the owner can pin or unpin a slot.")
        now, board = int(self.clock()), self.board_name(board)
        old = self.store.get_slot(board, self._task_id(task_id))
        if old is None:
            raise PlanError(f"Card {task_id} has no slot to pin.")
        slot = self.store.set_pinned(board, task_id, bool(pinned))
        card = self._quietly(lambda: self.board.card(board, task_id))
        if old.pinned != bool(pinned):
            self._record(ts=now, actor=actor, action="pin" if pinned else "unpin", board=board, task_id=task_id,
                               detail={"title": card.title if card else task_id, "start": old.start_ts})
        return Change("pin" if pinned else "unpin", task_id, card, slot, old)

    # ---- labels ----

    def label(self, name: Any, *, exclusive: Optional[bool] = None, description: Optional[str] = None,
              actor: str) -> Label:
        """Create a label or change it. ``exclusive`` marks it as "one card at a time"."""
        name = self._label_name(name)
        old = self.store.get_label(name)
        if old is None and len(self.store.labels()) >= MAX_LABELS:
            raise PlanError(f"The plan already has {MAX_LABELS} labels. Remove one first.")
        text = clean(description, 160) if description is not None else None
        label = self.store.put_label(name, exclusive=exclusive, description=text, by=actor, now=int(self.clock()))
        if old is None or old.exclusive != label.exclusive:
            self._record(ts=int(self.clock()), actor=actor, action="label",
                               detail={"label": name, "exclusive": label.exclusive, "new": old is None})
        return label

    def drop_label(self, name: Any, *, actor: str) -> None:
        name = self._label_name(name)
        if not self.store.delete_label(name):
            raise PlanError(f"There is no label {name}.")
        self._record(ts=int(self.clock()), actor=actor, action="unlabel", detail={"label": name})

    def tag(self, task_id: str, *, add: Iterable[str] = (), remove: Iterable[str] = (), actor: str,
            board: Optional[str] = None) -> list[str]:
        """Put labels on a card or take them off. A label that does not exist yet is created."""
        board = self.board_name(board)
        add, remove = [self._label_name(n) for n in add], [self._label_name(n) for n in remove]
        card = self._card(board, task_id)
        have = self.store.card_labels(board).get(task_id, [])
        if len(set(have) - set(remove) | set(add)) > MAX_CARD_LABELS:
            raise PlanError(f"A card carries at most {MAX_CARD_LABELS} labels.")
        for name in add:
            if self.store.get_label(name) is None:
                self.label(name, actor=actor)
        now_has = self.store.change_card_labels(board, task_id, add=add, remove=remove)
        if set(now_has) != set(have):
            self._record(ts=int(self.clock()), actor=actor, action="tag", board=board, task_id=task_id,
                               detail={"title": card.title, "labels": now_has})
        return now_has

    # ---- the timer ----

    def run_due(self, now: Optional[int] = None) -> list[Change]:
        """Release every card whose slot has started. Safe to call from several places at once:
        each due slot is leased to one caller."""
        now = int(self.clock() if now is None else now)
        done = [c for c in (self._release_due(slot, now)
                            for slot in self.store.claim_due(now, lease_s=LEASE_S, max_attempts=MAX_ATTEMPTS)) if c]
        for slot in self.store.exhausted(now, lease_s=LEASE_S, max_attempts=MAX_ATTEMPTS):
            note = f"not released after {MAX_ATTEMPTS} attempts: {slot.note or 'the board could not be reached'}"
            if self.store.finish(slot, FAILED, note, now):
                self._record(ts=now, actor=TIMER, action="failed", board=slot.board, task_id=slot.task_id,
                                   detail={"start": slot.start_ts, "note": note})
        if now - self._pruned_at > 3600:
            self._pruned_at = now
            self._quietly(lambda: (self.store.prune_slots(now - KEEP_S), self.store.prune_log(now - KEEP_S)))
        return done

    def next_due(self) -> Optional[int]:
        return self.store.next_due_ts(max_attempts=MAX_ATTEMPTS)

    def _release_due(self, slot: Slot, now: int) -> Optional[Change]:
        if now > slot.start_ts + max(slot.estimate_s, MIN_GRACE_S):
            state, note, released = MISSED, f"missed: the timer was not running at {self.fmt(slot.start_ts)}", ""
            card = self._quietly(lambda: self.board.card(slot.board, slot.task_id))  # for its title in the log
        else:
            try:
                card = self.board.card(slot.board, slot.task_id, fresh=True)
                released = ""
                if card is None:
                    state, note = FAILED, "the card is not on the board"
                elif card.status == "triage":
                    state, note = FAILED, "the card is in triage and cannot be released"
                elif card.status in RELEASABLE:
                    released = self.board.release(slot.board, slot.task_id)
                    state, note = RELEASED, f"released at its slot: {_status_note(released)}"
                else:
                    state, note = RELEASED, f"the card was already {card.status}"
            except BoardError as exc:
                self.store.note_attempt(slot, clean(str(exc), 200))
                logger.info("%s: could not release %s yet (%s)", MARK, slot.task_id, type(exc).__name__)
                return None
        if not self.store.finish(slot, state, note, now):
            return None  # the slot was moved or cleared meanwhile: the newer plan wins
        action = {RELEASED: "release", MISSED: "missed", FAILED: "failed"}[state]
        self._record(ts=now, actor=TIMER, action=action, board=slot.board, task_id=slot.task_id,
                           detail={"title": card.title if card else slot.task_id, "start": slot.start_ts, "note": note})
        if released:
            self._comment(slot.board, slot.task_id,
                          f"{MARK}: released at its slot (planned start {self.iso(slot.start_ts)}).")
        return Change(action, slot.task_id, card, self.store.get_slot(slot.board, slot.task_id), slot,
                      released=released, note=note)

    # ---- checks ----

    @staticmethod
    def _task_id(task_id: Any) -> str:
        if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
            raise PlanError("A card id looks like t_1a2b3c4d.")
        return task_id

    @staticmethod
    def _label_name(name: Any) -> str:
        text = str(name or "").strip().lower()
        if not _LABEL.fullmatch(text):
            raise PlanError("A label is lower-case letters, digits, dots, slashes or hyphens, with at most one "
                            "colon, for example repo:web or cluster:prod.")
        return text

    def _card(self, board: str, task_id: str) -> Card:
        try:
            card = self.board.card(board, self._task_id(task_id), fresh=True)
        except BoardError as exc:
            raise PlanError(f"The board cannot be reached now ({exc}). Nothing was changed.") from exc
        if card is None:
            raise PlanError(f"Card {task_id} is not on the board.")
        return card

    def _estimate(self, estimate_s: Optional[int], old: Optional[Slot]) -> int:
        if estimate_s is None:
            return old.estimate_s if old else self.default_estimate_s()
        if isinstance(estimate_s, bool) or not isinstance(estimate_s, int) or not MIN_ESTIMATE_S <= estimate_s <= MAX_ESTIMATE_S:
            raise PlanError("An estimate is between 1 minute and 14 days.")
        return estimate_s

    def _check_pin(self, slot: Optional[Slot], actor: str, verb: str) -> None:
        if slot is not None and slot.pinned and actor != OWNER:
            raise PlanError(f"The owner pinned this slot at {self.fmt(slot.start_ts)}. Only the owner can {verb} it. "
                            "Ask the owner, or plan around it.")

    @staticmethod
    def _check_plannable(card: Card) -> None:
        reasons = {
            "running": "is running now. A running card has no start to plan.",
            "review": "is in review. There is nothing to start.",
            "done": "is done.", "archived": "is archived.",
            "triage": "is in triage. Specify the card first; a triage card cannot be released.",
        }
        if card.status in reasons:
            raise PlanError(f'"{clashes.short(card.title)}" {reasons[card.status]}')

    @staticmethod
    def _write(call: Callable[[], Any]) -> Any:
        """A change on the board. When the Kanban refuses it, the move is refused with its reason."""
        try:
            return call()
        except BoardError as exc:
            raise PlanError(f"The Kanban did not accept the change ({exc}). The plan was not changed.") from exc

    def _comment(self, board: str, task_id: str, body: str) -> None:
        """A note on the card for whoever works it. A courtesy: a failure never undoes the move."""
        if self.setting("comment_on_release", True) is False:
            return
        try:
            self.board.comment(board, task_id, body)
        except BoardError as exc:
            logger.warning("%s: could not comment on %s (%s)", MARK, task_id, type(exc).__name__)

    @staticmethod
    def _quietly(call: Callable[[], Any]) -> Any:
        try:
            return call()
        except Exception as exc:
            logger.debug("%s: a best-effort step failed (%s)", MARK, type(exc).__name__)
            return None


def _status_note(status: str) -> str:
    return "todo, it waits for its parent cards" if status == "todo" else status


def _preview(board: str, task_id: str, start_ts: int, estimate_s: int, old: Optional[Slot], actor: str, now: int) -> Slot:
    return Slot(board, task_id, start_ts, estimate_s, bool(old and old.pinned), PLANNED, bool(old and old.parked),
                "", actor, now, None, 0)
