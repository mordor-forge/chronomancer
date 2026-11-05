"""Window store on SQLite (the plugin's ``plugin_db``). Pure stdlib so tests run without Hermes.

Times are Unix seconds (UTC). Intervals are half-open: a window is open for ``start <= now < end``.
Every writer runs in its own short transaction; WAL lets the gateway, kanban workers and the CLI
share one file.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Iterator, Optional

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS windows (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    scope         TEXT NOT NULL,
    start_ts      INTEGER NOT NULL,
    end_ts        INTEGER NOT NULL,
    exclusive     INTEGER NOT NULL DEFAULT 1,
    presence      INTEGER NOT NULL DEFAULT 0,
    state         TEXT NOT NULL CHECK (state IN ('pending','approved','declined','cancelled','closed')),
    kind          TEXT NOT NULL DEFAULT 'planned' CHECK (kind IN ('planned','emergency')),
    reason        TEXT NOT NULL DEFAULT '',
    cards         TEXT NOT NULL DEFAULT '[]',
    proposed_by   TEXT NOT NULL DEFAULT '',
    decided_by    TEXT,
    decided_at    INTEGER,
    checked_in_at INTEGER,
    closed_at     INTEGER,
    close_reason  TEXT,
    created_at    INTEGER NOT NULL,
    CHECK (end_ts > start_ts)
);
CREATE INDEX IF NOT EXISTS windows_scope_start ON windows (scope, start_ts);
CREATE TABLE IF NOT EXISTS audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         INTEGER NOT NULL,
    verdict    TEXT NOT NULL CHECK (verdict IN ('blocked','allowed','warned')),
    tool       TEXT NOT NULL,
    rule_ids   TEXT NOT NULL,
    scopes     TEXT NOT NULL,
    window_id  TEXT,
    preview    TEXT NOT NULL,
    session_id TEXT,
    task_id    TEXT
);
CREATE INDEX IF NOT EXISTS audit_ts ON audit (ts);
CREATE TABLE IF NOT EXISTS effects (
    window_id  TEXT NOT NULL,
    kind       TEXT NOT NULL,
    due_ts     INTEGER NOT NULL,
    claimed_ts INTEGER,
    attempts   INTEGER NOT NULL DEFAULT 0,
    done_ts    INTEGER,
    result     TEXT,
    PRIMARY KEY (window_id, kind)
);
"""

LIVE_STATES = ("pending", "approved")


@dataclass(frozen=True)
class Window:
    id: str
    name: str
    scope: str
    start_ts: int
    end_ts: int
    exclusive: bool
    presence: bool
    state: str
    kind: str
    reason: str
    cards: list[dict[str, str]]
    proposed_by: str
    decided_by: Optional[str]
    decided_at: Optional[int]
    checked_in_at: Optional[int]
    closed_at: Optional[int]
    close_reason: Optional[str]
    created_at: int

    def is_open(self, now: int) -> bool:
        """Approved, inside its interval, not closed early, and checked in when presence is required."""
        if self.state != "approved" or self.closed_at is not None:
            return False
        if not (self.start_ts <= now < self.end_ts):
            return False
        return not self.presence or self.checked_in_at is not None

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


class StoreError(RuntimeError):
    """A store operation the caller asked for cannot be done (unknown id, wrong state)."""


def _row_to_window(row: sqlite3.Row | tuple, cols: list[str]) -> Window:
    d = dict(zip(cols, row))
    d["exclusive"] = bool(d["exclusive"])
    d["presence"] = bool(d["presence"])
    try:
        d["cards"] = json.loads(d.get("cards") or "[]")
    except ValueError:
        d["cards"] = []
    return Window(**d)


class Store:
    """Thin data layer. ``connect`` returns a new ``sqlite3.Connection`` (``plugin_db`` in Hermes)."""

    def __init__(self, connect: Callable[[], sqlite3.Connection]):
        self._connect = connect
        self._ready = False

    # -- plumbing -------------------------------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        conn = self._connect()
        conn.execute("PRAGMA busy_timeout = 5000")
        if not self._ready:
            conn.executescript(_SCHEMA)
            self._migrate(conn)
            self._ready = True
        return conn

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """v1 -> v2: leased effects. Serialized with BEGIN IMMEDIATE, so two processes that open a v1
        file at the same time cannot both add a column. Adds columns in place; never drops data."""
        level = conn.isolation_level
        conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cols = {row[1] for row in conn.execute("PRAGMA table_info(effects)")}
                if "claimed_ts" not in cols:
                    conn.execute("ALTER TABLE effects ADD COLUMN claimed_ts INTEGER")
                if "attempts" not in cols:
                    conn.execute("ALTER TABLE effects ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
                conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('schema_version', ?)",
                             (str(SCHEMA_VERSION),))
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.isolation_level = level

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """One ``BEGIN IMMEDIATE`` transaction: read-check-write without a race between processes."""
        conn = self._conn()
        conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    @staticmethod
    def _rows(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[Window]:
        cur = conn.execute(sql, tuple(params))
        cols = [c[0] for c in cur.description]
        return [_row_to_window(r, cols) for r in cur.fetchall()]

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[Window]:
        conn = self._conn()
        try:
            return self._rows(conn, sql, params)
        finally:
            conn.close()

    def get(self, window_id: str) -> Optional[Window]:
        rows = self._query("SELECT * FROM windows WHERE id = ?", (window_id,))
        return rows[0] if rows else None

    # -- reads ----------------------------------------------------------------------------------
    def list_windows(self, start_ts: int, end_ts: int, *, scope: Optional[str] = None,
                     states: Iterable[str] = ("pending", "approved", "closed")) -> list[Window]:
        states = tuple(states)
        sql = (f"SELECT * FROM windows WHERE start_ts < ? AND end_ts > ? "
               f"AND state IN ({','.join('?' * len(states))})")
        params: list[Any] = [end_ts, start_ts, *states]
        if scope:
            sql += " AND scope = ?"
            params.append(scope)
        return self._query(sql + " ORDER BY start_ts, id", params)

    def conflicts(self, scope: str, start_ts: int, end_ts: int, exclusive: bool,
                  exclude_id: Optional[str] = None) -> list[Window]:
        """Live windows in the same scope whose interval overlaps, where either side is exclusive."""
        rows = self.list_windows(start_ts, end_ts, scope=scope, states=LIVE_STATES)
        return [w for w in rows if w.id != exclude_id and w.closed_at is None and (exclusive or w.exclusive)]

    def open_windows(self, scope: str, now: int) -> list[Window]:
        rows = self.list_windows(now, now + 1, scope=scope, states=("approved",))
        return [w for w in rows if w.is_open(now)]

    def next_window(self, scope: str, now: int) -> Optional[Window]:
        rows = self._query(
            "SELECT * FROM windows WHERE scope = ? AND state = 'approved' AND closed_at IS NULL "
            "AND end_ts > ? ORDER BY start_ts LIMIT 1", (scope, now))
        return rows[0] if rows else None

    # -- writes ---------------------------------------------------------------------------------
    def propose(self, *, name: str, scope: str, start_ts: int, end_ts: int, reason: str,
                proposed_by: str, now: int, exclusive: bool = True, presence: bool = False,
                cards: Optional[list[dict[str, str]]] = None, kind: str = "planned",
                state: str = "pending", decided_by: Optional[str] = None) -> tuple[Window, list[Window]]:
        if end_ts <= start_ts:
            raise StoreError("end must be after start")
        conflicts = self.conflicts(scope, start_ts, end_ts, exclusive)
        window_id = "W" + secrets.token_hex(3).upper()
        conn = self._conn()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO windows (id, name, scope, start_ts, end_ts, exclusive, presence, state, kind, "
                    "reason, cards, proposed_by, decided_by, decided_at, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (window_id, name, scope, start_ts, end_ts, int(exclusive), int(presence), state, kind,
                     reason, json.dumps(cards or []), proposed_by, decided_by,
                     now if decided_by else None, now))
                if state == "pending":
                    # Announce to the owner. Any process can propose (gateway, CLI chat, Kanban
                    # worker); the gateway's scheduler sends the message, so it is queued here.
                    conn.execute("INSERT OR IGNORE INTO effects (window_id, kind, due_ts) VALUES (?, 'announce', ?)",
                                 (window_id, now))
        finally:
            conn.close()
        window = self.get(window_id)
        assert window is not None
        return window, conflicts

    def decide(self, window_id: str, *, approve: bool, by: str, now: int) -> Window:
        """Owner decision on a pending window. Approval re-checks overlaps in the same transaction."""
        with self._write() as conn:
            rows = self._rows(conn, "SELECT * FROM windows WHERE id = ?", (window_id,))
            if not rows:
                raise StoreError(f"unknown window {window_id}")
            window = rows[0]
            if window.state != "pending":
                raise StoreError(f"window {window_id} is {window.state}, not pending")
            if approve:
                clash = [w for w in self._rows(
                    conn, "SELECT * FROM windows WHERE scope = ? AND state = 'approved' AND closed_at IS NULL "
                          "AND start_ts < ? AND end_ts > ? AND id != ?",
                    (window.scope, window.end_ts, window.start_ts, window.id)) if window.exclusive or w.exclusive]
                if clash:
                    raise StoreError(f"window {window_id} overlaps approved {', '.join(w.id for w in clash)}")
            conn.execute("UPDATE windows SET state = ?, decided_by = ?, decided_at = ? WHERE id = ?",
                         ("approved" if approve else "declined", by, now, window_id))
        return self.get(window_id)  # type: ignore[return-value]

    def check_in(self, window_id: str, *, by: str, now: int) -> Window:
        with self._write() as conn:
            cur = conn.execute(
                "UPDATE windows SET checked_in_at = ? WHERE id = ? AND state = 'approved' "
                "AND closed_at IS NULL AND checked_in_at IS NULL", (now, window_id))
            if cur.rowcount != 1:
                raise StoreError(f"window {window_id} cannot be checked in")
        return self.get(window_id)  # type: ignore[return-value]

    def close(self, window_id: str, *, by: str, reason: str, now: int) -> Window:
        """Close early (approved) or cancel (pending), atomically. Closing only removes permission."""
        with self._write() as conn:
            rows = self._rows(conn, "SELECT * FROM windows WHERE id = ?", (window_id,))
            if not rows:
                raise StoreError(f"unknown window {window_id}")
            window = rows[0]
            if window.state == "pending":
                new_state = "cancelled"
            elif window.state == "approved" and window.closed_at is None:
                new_state = "closed"
            else:
                raise StoreError(f"window {window_id} is {window.state}; nothing to close")
            conn.execute("UPDATE windows SET state = ?, closed_at = ?, close_reason = ? WHERE id = ?",
                         (new_state, now, f"{by}: {reason}", window_id))
        closed = self.get(window_id)
        assert closed is not None and not closed.is_open(now)
        return closed

    def record_audit(self, *, now: int, verdict: str, tool: str, rule_ids: list[str], scopes: list[str],
                     preview: str, window_id: Optional[str] = None, session_id: str = "", task_id: str = "") -> None:
        conn = self._conn()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO audit (ts, verdict, tool, rule_ids, scopes, window_id, preview, session_id, task_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (now, verdict, tool, json.dumps(rule_ids), json.dumps(scopes), window_id, preview[:400],
                     session_id, task_id))
        finally:
            conn.close()

    def purge_previews(self, *, before_ts: Optional[int] = None) -> int:
        """Owner action: blank stored command previews (all, or older than ``before_ts``). Audit rows
        stay; only the preview text goes. SQLite may keep old pages until VACUUM."""
        with self._write() as conn:
            sql = "UPDATE audit SET preview = '[purged]' WHERE preview != '[purged]'"
            params: tuple[Any, ...] = ()
            if before_ts is not None:
                sql += " AND ts < ?"
                params = (before_ts,)
            return conn.execute(sql, params).rowcount

    def audit_tail(self, limit: int = 50) -> list[dict[str, Any]]:
        conn = self._conn()
        try:
            cur = conn.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        finally:
            conn.close()

    # -- timed effects (reminders, release at start, miss report at end) ------------------------
    def ensure_effects(self, window: Window, *, reminder_s: int) -> None:
        plan = [("remind", window.start_ts - reminder_s), ("start", window.start_ts), ("end", window.end_ts)]
        conn = self._conn()
        try:
            with conn:
                conn.executemany("INSERT OR IGNORE INTO effects (window_id, kind, due_ts) VALUES (?,?,?)",
                                 [(window.id, k, due) for k, due in plan])
        finally:
            conn.close()

    def claim_due_effects(self, now: int, *, lease_s: int = 300, max_attempts: int = 3,
                          kinds: Optional[Iterable[str]] = None) -> list[tuple[str, str, int]]:
        """Lease due, unfinished effects. A lease that is not finished within ``lease_s`` (crash,
        timeout) is claimed again, up to ``max_attempts``. This is at-least-once with bounded
        retries: a remote side effect (a message, a comment) can repeat after a crash between the
        remote call and ``finish_effect``. Callers re-read current state before acting.
        ``kinds`` limits the claim to the effect kinds the caller can carry out."""
        claimed: list[tuple[str, str, int]] = []
        sql = ("SELECT window_id, kind, due_ts FROM effects WHERE done_ts IS NULL AND due_ts <= ? "
               "AND attempts < ? AND (claimed_ts IS NULL OR claimed_ts <= ?)")
        params: list[Any] = [now, max_attempts, now - lease_s]
        if kinds is not None:
            kinds = tuple(kinds)
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            params.extend(kinds)
        with self._write() as conn:
            due = conn.execute(sql + " ORDER BY due_ts", params).fetchall()
            for window_id, kind, due_ts in due:
                conn.execute("UPDATE effects SET claimed_ts = ?, attempts = attempts + 1 "
                             "WHERE window_id = ? AND kind = ?", (now, window_id, kind))
                claimed.append((window_id, kind, due_ts))
        return claimed

    def finish_effect(self, window_id: str, kind: str, result: str, *, now: int) -> None:
        with self._write() as conn:
            conn.execute("UPDATE effects SET done_ts = ?, result = ? WHERE window_id = ? AND kind = ?",
                         (now, result[:400], window_id, kind))

    def failed_effects(self, now: int, *, lease_s: int = 300, max_attempts: int = 3) -> list[tuple[str, str, int]]:
        """Effects that used every attempt and whose last lease expired unfinished: report them."""
        conn = self._conn()
        try:
            return [tuple(r) for r in conn.execute(
                "SELECT window_id, kind, attempts FROM effects WHERE done_ts IS NULL AND attempts >= ? "
                "AND claimed_ts <= ?", (max_attempts, now - lease_s)).fetchall()]
        finally:
            conn.close()
