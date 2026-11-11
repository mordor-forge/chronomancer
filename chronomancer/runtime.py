"""One planner per plugin data directory in this process, and the timer that releases due cards.

Why a thread and not a hook: the Kanban dispatcher's tick hook reaches only the plugins of the
gateway's own profile, so a plugin that runs in another profile (the orchestrator's) would never
hear it. The thread wakes at the next slot start, at least once a minute, and calls
``Planner.run_due``. That call is safe to repeat and to run from several processes: each due slot
is leased to one caller in the store.

The thread never reads the configuration. Settings are read where Hermes has the right profile in
scope (plugin load, a tool call, a Desktop request) and kept in ``Settings``.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .board import Board
from .planner import Planner
from .store import Store
from .timezones import local_tz

logger = logging.getLogger(__name__)

DB_FILE = "plan.db"

SETTING_KEYS = ("board", "timezone", "default_estimate_minutes", "comment_on_release")
_MISSING = object()


class Settings:
    """The plugin's settings as last read in the right profile scope."""

    def __init__(self, read: Callable[[str, Any], Any]):
        self._read = read
        self._values: dict[str, Any] = {}

    def refresh(self) -> None:
        for key in SETTING_KEYS:
            try:
                value = self._read(key, _MISSING)
            except Exception:
                logger.debug("chronomancer: could not read setting %s", key, exc_info=True)
                continue
            if value is _MISSING:
                self._values.pop(key, None)
            else:
                self._values[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)


class Timer:
    """Calls ``tick`` when the next slot is due, and at least every ``idle_s`` seconds."""

    def __init__(self, tick: Callable[[], Optional[float]], *, idle_s: float = 60.0, first_s: float = 5.0,
                 name: str = "chronomancer-timer"):
        self._tick, self._idle_s, self._first_s, self._name = tick, idle_s, first_s, name
        self._wake = threading.Event()
        self._stopped = False
        self._thread: Optional[threading.Thread] = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stopped = False
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopped = True
        self._wake.set()

    def wake(self) -> None:
        """The plan changed: look at it again now."""
        self._wake.set()

    def _run(self) -> None:
        delay = self._first_s
        while True:
            self._wake.wait(delay)
            self._wake.clear()
            if self._stopped:
                return
            try:
                until = self._tick()
            except Exception:
                logger.exception("chronomancer: the timer pass failed")
                until = None
            delay = self._idle_s if until is None else min(self._idle_s, max(1.0, until))


@dataclass
class Runtime:
    planner: Any
    settings: Settings
    timer: Optional[Timer] = None


_LOCK = threading.Lock()
_RUNTIMES: dict[str, Runtime] = {}


def attach(key: str, planner: Any, settings: Settings, *, timer: bool, clock: Callable[[], float] = time.time) -> Runtime:
    """Make ``planner`` the one for ``key`` (a plugin data directory) in this process. A planner that
    was attached before is replaced and its timer stops."""

    def tick() -> Optional[float]:
        planner.run_due()
        due = planner.next_due()
        return None if due is None else due - clock()

    runtime = Runtime(planner, settings, Timer(tick) if timer else None)
    planner.wake = runtime.timer.wake if runtime.timer else (lambda: None)
    with _LOCK:
        old = _RUNTIMES.get(key)
        _RUNTIMES[key] = runtime
    if old and old.timer:
        old.timer.stop()
    if runtime.timer:
        runtime.timer.start()
    return runtime


def get(key: str) -> Optional[Runtime]:
    with _LOCK:
        return _RUNTIMES.get(key)


def detach(key: str, runtime: Optional[Runtime] = None) -> None:
    """Stop the planner for ``key``. With ``runtime``: only when it is still the attached one."""
    with _LOCK:
        old = _RUNTIMES.get(key)
        if old is None or (runtime is not None and old is not runtime):
            old = None
        else:
            del _RUNTIMES[key]
    target = old or runtime
    if target and target.timer:
        target.timer.stop()


def connect_file(path: Path) -> sqlite3.Connection:
    """A plain connection to the plan database. It sets no journal mode: the file keeps the mode
    that Hermes chose for it (see ``build``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(path, timeout=5)


def build(data_dir: Any, *, dispatch: Optional[Callable[[str, dict], Any]], read_setting: Callable[[str, Any], Any],
          timer: bool, prepare: Optional[Callable[[str], sqlite3.Connection]] = None) -> Runtime:
    """The planner for one plugin data directory, attached to this process.

    ``data_dir`` is resolved by the caller while Hermes has the right profile in scope. The path is
    kept as given, so the timer thread opens the same file whatever profile is in scope later.

    ``prepare`` is Hermes's ``plugin_db`` for this plugin. It is called once, here, in the right
    scope: Hermes creates the file with its own journal-mode rules (no WAL on a network file system
    or on an SQLite build with the WAL-reset bug). The mode is stored in the file, so the plain
    connections opened later follow it.
    """
    db = Path(data_dir) / DB_FILE
    if prepare is not None:
        try:
            prepare(DB_FILE).close()
        except Exception:
            logger.warning("chronomancer: Hermes could not prepare the plan database; using SQLite defaults", exc_info=True)
    settings = Settings(read_setting)
    settings.refresh()
    planner = Planner(Store(lambda: connect_file(db)), Board(dispatch), setting=settings.get,
                      tz=lambda: local_tz(settings.get("timezone") or None))
    return attach(str(data_dir), planner, settings, timer=timer)
