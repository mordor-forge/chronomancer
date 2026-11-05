"""Window store on SQLite (the plugin's ``plugin_db``). Pure stdlib so tests run without Hermes.

Times are Unix seconds (UTC). Intervals are half-open: a window is open for ``start <= now < end``.
Every writer runs in its own short transaction; WAL lets the gateway, kanban workers and the CLI
share one file.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

SCHEMA_VERSION = 1

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
    window_id TEXT NOT NULL,
    kind      TEXT NOT NULL,
    due_ts    INTEGER NOT NULL,
    done_ts   INTEGER,
    result    TEXT,
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
        if not self._ready:
            conn.executescript(_SCHEMA)
            conn.execute("INSERT OR IGNORE INTO meta (k, v) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            conn.commit()
            self._ready = True
        return conn

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[Window]:
        conn = self._conn()
        try:
            cur = conn.execute(sql, tuple(params))
            cols = [c[0] for c in cur.description]
            return [_row_to_window(r, cols) for r in cur.fetchall()]
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
        finally:
            conn.close()
        window = self.get(window_id)
        assert window is not None
        return window, conflicts

    def decide(self, window_id: str, *, approve: bool, by: str, now: int) -> Window:
        """Owner decision on a pending window. Approving re-checks conflicts against approved windows."""
        window = self.get(window_id)
        if window is None:
            raise StoreError(f"unknown window {window_id}")
        if window.state != "pending":
            raise StoreError(f"window {window_id} is {window.state}, not pending")
        if approve:
            clash = [w for w in self.conflicts(window.scope, window.start_ts, window.end_ts, window.exclusive,
                                               exclude_id=window.id) if w.state == "approved"]
            if clash:
                raise StoreError(f"window {window_id} overlaps approved {', '.join(w.id for w in clash)}")
        conn = self._conn()
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE windows SET state = ?, decided_by = ?, decided_at = ? WHERE id = ? AND state = 'pending'",
                    ("approved" if approve else "declined", by, now, window_id))
                if cur.rowcount != 1:
                    raise StoreError(f"window {window_id} changed while deciding")
        finally:
            conn.close()
        return self.get(window_id)  # type: ignore[return-value]

    def check_in(self, window_id: str, *, by: str, now: int) -> Window:
        conn = self._conn()
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE windows SET checked_in_at = ? WHERE id = ? AND state = 'approved' "
                    "AND closed_at IS NULL AND checked_in_at IS NULL", (now, window_id))
                if cur.rowcount != 1:
                    raise StoreError(f"window {window_id} cannot be checked in")
        finally:
            conn.close()
        return self.get(window_id)  # type: ignore[return-value]

    def close(self, window_id: str, *, by: str, reason: str, now: int) -> Window:
        """Close early (approved) or cancel (pending). Closing only removes permission."""
        window = self.get(window_id)
        if window is None:
            raise StoreError(f"unknown window {window_id}")
        conn = self._conn()
        try:
            with conn:
                if window.state == "pending":
                    conn.execute("UPDATE windows SET state = 'cancelled', closed_at = ?, close_reason = ? "
                                 "WHERE id = ? AND state = 'pending'", (now, f"{by}: {reason}", window_id))
                elif window.state == "approved" and window.closed_at is None:
                    conn.execute("UPDATE windows SET state = 'closed', closed_at = ?, close_reason = ? "
                                 "WHERE id = ? AND state = 'approved'", (now, f"{by}: {reason}", window_id))
                else:
                    raise StoreError(f"window {window_id} is {window.state}; nothing to close")
        finally:
            conn.close()
        return self.get(window_id)  # type: ignore[return-value]

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

    def claim_due_effects(self, now: int) -> list[tuple[str, str, int]]:
        """Claim every due, undone effect exactly once (safe across processes and restarts)."""
        conn = self._conn()
        claimed: list[tuple[str, str, int]] = []
        try:
            due = conn.execute("SELECT window_id, kind, due_ts FROM effects WHERE done_ts IS NULL AND due_ts <= ? "
                               "ORDER BY due_ts", (now,)).fetchall()
            for window_id, kind, due_ts in due:
                with conn:
                    cur = conn.execute("UPDATE effects SET done_ts = ? WHERE window_id = ? AND kind = ? "
                                       "AND done_ts IS NULL", (now, window_id, kind))
                if cur.rowcount == 1:
                    claimed.append((window_id, kind, due_ts))
        finally:
            conn.close()
        return claimed

    def finish_effect(self, window_id: str, kind: str, result: str) -> None:
        conn = self._conn()
        try:
            with conn:
                conn.execute("UPDATE effects SET result = ? WHERE window_id = ? AND kind = ?",
                             (result[:400], window_id, kind))
        finally:
            conn.close()
