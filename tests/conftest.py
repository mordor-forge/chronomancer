"""Make the plugin's core package importable without Hermes, and build a planner on a fake board."""
import importlib.util
import sqlite3
import sys
import types
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from chronomancer.board import Board  # noqa: E402
from chronomancer.planner import Planner  # noqa: E402
from chronomancer.store import Store  # noqa: E402
from fake_kanban import FakeKanban  # noqa: E402

ZONE = ZoneInfo("Europe/Prague")
NOW = int(datetime(2026, 11, 11, 10, 0, tzinfo=ZONE).timestamp())   # a Wednesday, 10:00 local
HOUR = 3600


def at(hour, minute=0, day=11):
    """A local time on 2026-11-<day> as Unix seconds."""
    return int(datetime(2026, 11, day, hour, minute, tzinfo=ZONE).timestamp())


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self.now


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "plan.db"

    def connect():
        conn = sqlite3.connect(db, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    return Store(connect)


@pytest.fixture
def kanban():
    return FakeKanban()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def config():
    return {}


@pytest.fixture
def planner(store, kanban, clock, config):
    board = Board(kanban, list_ttl_s=0, card_ttl_s=0, closed_ttl_s=0)
    return Planner(store, board, setting=lambda key, default=None: config.get(key, default),
                   tz=lambda: ZONE, clock=clock)


class EntryCtx:
    """A stand-in Hermes plugin context that keeps the registered tools and hooks."""

    def __init__(self, config=None, dispatch=None, **attrs):
        self.config = dict(config or {})
        self.tools, self.hooks, self.unload = {}, {}, []
        self.dispatch = dispatch
        self.__dict__.update(attrs)

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def register_tool(self, name, handler=None, **_):
        self.tools[name] = handler

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def dispatch_tool(self, tool, args, **_):
        if self.dispatch is None:
            raise RuntimeError("Unknown tool")
        return self.dispatch(tool, args)

    def on_unload(self, callback):
        self.unload.append(callback)


@pytest.fixture
def plugin_entry(tmp_path, monkeypatch):
    """Import the plugin the way Hermes does; ``plugin_entry(config, dispatch) -> ctx`` after ``register``."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    storage = types.ModuleType("plugins.plugin_storage")
    storage.plugin_data_dir = lambda name: tmp_path / "plugin-data" / name
    storage.opened = []

    def plugin_db(name, filename="data.db"):
        path = tmp_path / "plugin-data" / name / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        storage.opened.append(path)
        return sqlite3.connect(path, timeout=5)

    storage.plugin_db = plugin_db
    monkeypatch.setitem(sys.modules, "plugins", types.ModuleType("plugins"))
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", storage)
    # Hermes loads a plugin as ``hermes_plugins.<name>``; the dashboard backend finds the agent half by that.
    monkeypatch.setitem(sys.modules, "hermes_plugins", types.ModuleType("hermes_plugins"))
    name = "hermes_plugins.chrono_entry_under_test"
    spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    made = []

    def run(config=None, dispatch=None, **attrs):
        ctx = EntryCtx(config, dispatch, **attrs)
        module.register(ctx)
        made.append(ctx)
        return ctx

    run.module = module
    run.storage = storage
    run.data_dir = tmp_path / "plugin-data" / "chronomancer"
    yield run
    for ctx in made:
        for callback in ctx.unload:
            callback()
    for mod in [m for m in sys.modules if m == name or m.startswith(name + ".")]:
        sys.modules.pop(mod, None)
