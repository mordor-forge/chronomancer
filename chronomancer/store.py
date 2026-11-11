"""The plan store: slots, labels and the change log, in one SQLite file.

A slot is the planned start and the estimate of one card. The store keeps no card data: titles,
statuses and assignees stay on the Kanban board and are read from there.

Every method opens its own connection, so one ``Store`` is safe to share between the timer thread,
the agent tools and the Desktop backend.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Iterator, Optional

SCHEMA_VERSION = 1
PLANNED, RELEASED, MISSED, FAILED = "planned", "released", "missed", "failed"
STATES = (PLANNED, RELEASED, MISSED, FAILED)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS slots (
    board       TEXT NOT NULL DEFAULT '',
    task_id     TEXT NOT NULL,
    start_ts    INTEGER NOT NULL,
    estimate_s  INTEGER NOT NULL CHECK (estimate_s > 0),
    pinned      INTEGER NOT NULL DEFAULT 0,
    state       TEXT NOT NULL DEFAULT 'planned' CHECK (state IN ('planned','released','missed','failed')),
    parked      INTEGER NOT NULL DEFAULT 0,
    note        TEXT NOT NULL DEFAULT '',
    set_by      TEXT NOT NULL,
    set_at      INTEGER NOT NULL,
    released_at INTEGER,
    attempts    INTEGER NOT NULL DEFAULT 0,
    claimed_ts  INTEGER,
    PRIMARY KEY (board, task_id)
);
CREATE INDEX IF NOT EXISTS slots_due ON slots (state, start_ts);
CREATE TABLE IF NOT EXISTS labels (
    name        TEXT PRIMARY KEY,
    exclusive   INTEGER NOT NULL DEFAULT 0,
    description TEXT NOT NULL DEFAULT '',
    created_by  TEXT NOT NULL,
    created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS card_labels (
    board   TEXT NOT NULL DEFAULT '',
    task_id TEXT NOT NULL,
    label   TEXT NOT NULL,
    PRIMARY KEY (board, task_id, label)
);
CREATE TABLE IF NOT EXISTS log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      INTEGER NOT NULL,
    actor   TEXT NOT NULL,
    action  TEXT NOT NULL,
    board   TEXT NOT NULL DEFAULT '',
    task_id TEXT NOT NULL DEFAULT '',
    detail  TEXT NOT NULL DEFAULT '{}'
);
"""

@dataclass(frozen=True)
class Slot:
    board: str
    task_id: str
    start_ts: int
    estimate_s: int
    pinned: bool
    state: str
    parked: bool          # True when the plugin parked the card for this slot
    note: str             # what the last release attempt did
    set_by: str           # "owner", or "agent:<profile>"
    set_at: int
    released_at: Optional[int]
    attempts: int

    @property
    def end_ts(self) -> int:
        return self.start_ts + self.estimate_s


@dataclass(frozen=True)
class Label:
    name: str
    exclusive: bool       # True: one card at a time. Two overlapping slots that share it clash.
    description: str
    created_by: str
    created_at: int


@dataclass(frozen=True)
class LogEntry:
    id: int
    ts: int
    actor: str            # "owner", "agent:<profile>" or "timer"
    action: str
    board: str
    task_id: str
    detail: dict[str, Any]


def _slot(row: sqlite3.Row) -> Slot:
    return Slot(board=row["board"], task_id=row["task_id"], start_ts=row["start_ts"], estimate_s=row["estimate_s"],
                pinned=bool(row["pinned"]), state=row["state"], parked=bool(row["parked"]), note=row["note"],
                set_by=row["set_by"], set_at=row["set_at"], released_at=row["released_at"], attempts=row["attempts"])


def _log(row: sqlite3.Row) -> LogEntry:
    try:
        detail = json.loads(row["detail"])
    except ValueError:
        detail = {}
    return LogEntry(id=row["id"], ts=row["ts"], actor=row["actor"], action=row["action"], board=row["board"],
                    task_id=row["task_id"], detail=detail if isinstance(detail, dict) else {})


class Store:
    def __init__(self, connect: Callable[[], sqlite3.Connection]):
        self._connect = connect
        self._ready = False
        self._lock = threading.Lock()

    # ---- connections ----

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        conn = self._open()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        conn = self._open()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _open(self) -> sqlite3.Connection:
        conn = self._connect()
        conn.isolation_level = None  # explicit transactions only
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        if not self._ready:
            with self._lock:
                if not self._ready:
                    conn.executescript(_SCHEMA)
                    conn.execute("INSERT OR IGNORE INTO meta (k, v) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
                    self._ready = True
        return conn

    # ---- slots ----

    def get_slot(self, board: str, task_id: str) -> Optional[Slot]:
        with self._read() as conn:
            row = conn.execute("SELECT * FROM slots WHERE board = ? AND task_id = ?", (board, task_id)).fetchone()
        return _slot(row) if row else None

    def slots(self, board: str, *, since_ts: Optional[int] = None) -> list[Slot]:
        """The slots of a board. With ``since_ts``: the planned ones, and the finished ones that started since."""
        sql, params = "SELECT * FROM slots WHERE board = ?", [board]
        if since_ts is not None:
            sql, params = sql + " AND (state = 'planned' OR start_ts >= ?)", [board, int(since_ts)]
        with self._read() as conn:
            rows = conn.execute(sql + " ORDER BY start_ts, task_id", params).fetchall()
        return [_slot(r) for r in rows]

    def put_slot(self, board: str, task_id: str, *, start_ts: int, estimate_s: int, state: str, parked: bool,
                 set_by: str, now: int, note: str = "", released_at: Optional[int] = None) -> Slot:
        """Create the slot of a card or replace its time, estimate and state. ``pinned`` survives."""
        with self._write() as conn:
            conn.execute(
                "INSERT INTO slots (board, task_id, start_ts, estimate_s, state, parked, note, set_by, set_at, released_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (board, task_id) DO UPDATE SET start_ts = excluded.start_ts, "
                "estimate_s = excluded.estimate_s, state = excluded.state, parked = excluded.parked, "
                "note = excluded.note, set_by = excluded.set_by, set_at = excluded.set_at, "
                "released_at = excluded.released_at, attempts = 0, claimed_ts = NULL",
                (board, task_id, int(start_ts), int(estimate_s), state, int(parked), note, set_by, int(now), released_at))
            row = conn.execute("SELECT * FROM slots WHERE board = ? AND task_id = ?", (board, task_id)).fetchone()
        return _slot(row)

    def delete_slot(self, board: str, task_id: str) -> bool:
        with self._write() as conn:
            return conn.execute("DELETE FROM slots WHERE board = ? AND task_id = ?", (board, task_id)).rowcount == 1

    def set_pinned(self, board: str, task_id: str, pinned: bool) -> Optional[Slot]:
        with self._write() as conn:
            conn.execute("UPDATE slots SET pinned = ? WHERE board = ? AND task_id = ?", (int(pinned), board, task_id))
            row = conn.execute("SELECT * FROM slots WHERE board = ? AND task_id = ?", (board, task_id)).fetchone()
        return _slot(row) if row else None

    def claim_due(self, now: int, *, lease_s: int, max_attempts: int) -> list[Slot]:
        """Planned slots whose start has come, leased to the caller for ``lease_s`` seconds. Each
        claim counts as one attempt, so a slot that keeps failing stops being claimed."""
        with self._write() as conn:
            rows = conn.execute(
                "SELECT * FROM slots WHERE state = 'planned' AND start_ts <= ? AND attempts < ? "
                "AND (claimed_ts IS NULL OR claimed_ts <= ?) ORDER BY start_ts, task_id",
                (now, max_attempts, now - lease_s)).fetchall()
            for row in rows:
                conn.execute("UPDATE slots SET claimed_ts = ?, attempts = attempts + 1 WHERE board = ? AND task_id = ?",
                             (now, row["board"], row["task_id"]))
        return [replace(_slot(r), attempts=r["attempts"] + 1) for r in rows]

    def finish(self, slot: Slot, state: str, note: str, now: int) -> bool:
        """Close a claimed slot. False when the slot changed since the claim (moved or cleared):
        the newer plan wins and this result is dropped."""
        with self._write() as conn:
            done = conn.execute(
                "UPDATE slots SET state = ?, note = ?, claimed_ts = NULL, "
                "released_at = CASE WHEN ? = 'released' THEN ? ELSE released_at END "
                "WHERE board = ? AND task_id = ? AND state = 'planned' AND start_ts = ?",
                (state, note, state, now, slot.board, slot.task_id, slot.start_ts)).rowcount
        return done == 1

    def note_attempt(self, slot: Slot, note: str) -> None:
        with self._write() as conn:
            conn.execute("UPDATE slots SET note = ? WHERE board = ? AND task_id = ? AND state = 'planned' AND start_ts = ?",
                         (note, slot.board, slot.task_id, slot.start_ts))

    def exhausted(self, now: int, *, lease_s: int, max_attempts: int) -> list[Slot]:
        """Planned slots that used every attempt and whose last lease ran out."""
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM slots WHERE state = 'planned' AND attempts >= ? AND (claimed_ts IS NULL OR claimed_ts <= ?)",
                (max_attempts, now - lease_s)).fetchall()
        return [_slot(r) for r in rows]

    def next_due_ts(self, *, max_attempts: int) -> Optional[int]:
        with self._read() as conn:
            row = conn.execute("SELECT MIN(start_ts) FROM slots WHERE state = 'planned' AND attempts < ?",
                               (max_attempts,)).fetchone()
        return row[0] if row and row[0] is not None else None

    def prune_slots(self, before_ts: int) -> int:
        """Drop finished slots that started before ``before_ts``."""
        with self._write() as conn:
            return conn.execute("DELETE FROM slots WHERE state != 'planned' AND start_ts < ?", (before_ts,)).rowcount

    # ---- labels ----

    def labels(self) -> list[Label]:
        with self._read() as conn:
            rows = conn.execute("SELECT * FROM labels ORDER BY name").fetchall()
        return [Label(r["name"], bool(r["exclusive"]), r["description"], r["created_by"], r["created_at"]) for r in rows]

    def get_label(self, name: str) -> Optional[Label]:
        with self._read() as conn:
            r = conn.execute("SELECT * FROM labels WHERE name = ?", (name,)).fetchone()
        return Label(r["name"], bool(r["exclusive"]), r["description"], r["created_by"], r["created_at"]) if r else None

    def put_label(self, name: str, *, exclusive: Optional[bool], description: Optional[str], by: str, now: int) -> Label:
        """Create a label, or change the given fields of an existing one."""
        with self._write() as conn:
            conn.execute("INSERT OR IGNORE INTO labels (name, exclusive, description, created_by, created_at) "
                         "VALUES (?, 0, '', ?, ?)", (name, by, int(now)))
            if exclusive is not None:
                conn.execute("UPDATE labels SET exclusive = ? WHERE name = ?", (int(bool(exclusive)), name))
            if description is not None:
                conn.execute("UPDATE labels SET description = ? WHERE name = ?", (description, name))
            r = conn.execute("SELECT * FROM labels WHERE name = ?", (name,)).fetchone()
        return Label(r["name"], bool(r["exclusive"]), r["description"], r["created_by"], r["created_at"])

    def delete_label(self, name: str) -> bool:
        with self._write() as conn:
            conn.execute("DELETE FROM card_labels WHERE label = ?", (name,))
            return conn.execute("DELETE FROM labels WHERE name = ?", (name,)).rowcount == 1

    def card_labels(self, board: str) -> dict[str, list[str]]:
        with self._read() as conn:
            rows = conn.execute("SELECT task_id, label FROM card_labels WHERE board = ? ORDER BY task_id, label",
                                (board,)).fetchall()
        out: dict[str, list[str]] = {}
        for r in rows:
            out.setdefault(r["task_id"], []).append(r["label"])
        return out

    def change_card_labels(self, board: str, task_id: str, *, add: Iterable[str] = (), remove: Iterable[str] = ()) -> list[str]:
        with self._write() as conn:
            for name in remove:
                conn.execute("DELETE FROM card_labels WHERE board = ? AND task_id = ? AND label = ?", (board, task_id, name))
            for name in add:
                conn.execute("INSERT OR IGNORE INTO card_labels (board, task_id, label) VALUES (?, ?, ?)", (board, task_id, name))
            rows = conn.execute("SELECT label FROM card_labels WHERE board = ? AND task_id = ? ORDER BY label",
                                (board, task_id)).fetchall()
        return [r["label"] for r in rows]

    # ---- change log ----

    def add_log(self, *, ts: int, actor: str, action: str, board: str = "", task_id: str = "",
                detail: Optional[dict[str, Any]] = None) -> int:
        with self._write() as conn:
            cur = conn.execute("INSERT INTO log (ts, actor, action, board, task_id, detail) VALUES (?, ?, ?, ?, ?, ?)",
                               (int(ts), actor, action, board, task_id,
                                json.dumps(detail or {}, ensure_ascii=False, sort_keys=True, default=str)))
            return int(cur.lastrowid or 0)

    def log_tail(self, limit: int = 30, *, board: Optional[str] = None) -> list[LogEntry]:
        """The newest entries first."""
        sql, params = "SELECT * FROM log", []
        if board is not None:
            sql, params = sql + " WHERE board = ?", [board]
        with self._read() as conn:
            rows = conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*params, int(limit))).fetchall()
        return [_log(r) for r in rows]

    def log_after(self, after_id: int, *, limit: int = 50) -> list[LogEntry]:
        """Entries newer than ``after_id``, oldest first."""
        with self._read() as conn:
            rows = conn.execute("SELECT * FROM log WHERE id > ? ORDER BY id LIMIT ?", (int(after_id), int(limit))).fetchall()
        return [_log(r) for r in rows]

    def last_log_id(self) -> int:
        with self._read() as conn:
            row = conn.execute("SELECT MAX(id) FROM log").fetchone()
        return int(row[0] or 0)

    def prune_log(self, before_ts: int) -> int:
        with self._write() as conn:
            return conn.execute("DELETE FROM log WHERE ts < ?", (before_ts,)).rowcount

    # ---- meta ----

    def get_meta(self, key: str, default: str = "") -> str:
        with self._read() as conn:
            row = conn.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._write() as conn:
            conn.execute("INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT (k) DO UPDATE SET v = excluded.v", (key, value))
