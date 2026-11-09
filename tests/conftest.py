"""Make the plugin's core package importable without Hermes."""
import importlib.util
import sqlite3
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from change_windows.store import Store  # noqa: E402


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "data.db"

    def connect():
        conn = sqlite3.connect(db, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    return Store(connect)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "data.db"


class EntryCtx:
    """A stand-in Hermes plugin context that keeps the registered tools and hooks."""

    def __init__(self, config=None, **attrs):
        self.config = dict(config or {})
        self.tools, self.hooks = {}, {}
        self.__dict__.update(attrs)

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def register_tool(self, name, handler=None, **_):
        self.tools[name] = handler

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_platform_handler(self, platform, factory):
        pass


@pytest.fixture
def plugin_entry(tmp_path, monkeypatch):
    """Import the plugin the way Hermes does; ``plugin_entry(config) -> ctx`` after ``register``."""
    storage = types.ModuleType("plugins.plugin_storage")
    storage.plugin_data_dir = lambda name: tmp_path / "plugin-data" / name
    storage.plugin_db = lambda name: sqlite3.connect(tmp_path / "data.db", timeout=5)
    monkeypatch.setitem(sys.modules, "plugins", types.ModuleType("plugins"))
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", storage)
    name = "cw_entry_under_test"
    spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)

    def run(config=None, **attrs):
        ctx = EntryCtx(config, **attrs)
        module.register(ctx)
        return ctx

    yield run
    for mod in [m for m in sys.modules if m == name or m.startswith(name + ".")]:
        sys.modules.pop(mod, None)
