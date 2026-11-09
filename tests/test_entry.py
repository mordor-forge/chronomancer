"""The plugin entry point (top-level ``__init__.py``) with a stand-in Hermes context."""
import importlib.util
import json
import sqlite3
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
KAPPLY = {"command": "kubectl apply -f deploy.yaml"}


class Ctx:
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
def register(tmp_path, monkeypatch):
    """Import the plugin the way Hermes does and return ``register(ctx) -> ctx``."""
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

    def run(ctx):
        module.register(ctx)
        return ctx

    yield run
    for mod in [m for m in sys.modules if m == name or m.startswith(name + ".")]:
        sys.modules.pop(mod, None)


@pytest.mark.parametrize("value", [False, "off", "OFF "])
def test_mode_off_survives_yaml_boolean_parsing(register, value):
    # YAML 1.1 reads an unquoted ``mode: off`` as False; it must still switch the guard off.
    hook = register(Ctx({"mode": value})).hooks["pre_tool_call"]
    assert hook(tool_name="terminal", args=KAPPLY) is None


@pytest.mark.parametrize("value", [True, "on", "false", "enfroce", 1, None, ["off"]])
def test_any_other_mode_value_enforces(register, value):
    hook = register(Ctx({"mode": value})).hooks["pre_tool_call"]
    assert hook(tool_name="terminal", args=KAPPLY)["action"] == "block"


def test_propose_works_on_a_context_without_profile_name(register):
    ctx = register(Ctx())  # no profile_name attribute at all
    out = json.loads(ctx.tools["window_propose"](
        {"name": "n", "scope": "k8s-prod", "start": "2999-01-01T10:00+00:00", "duration_minutes": 30, "reason": "r"}))
    assert out["ok"], out
    row = sqlite3.connect(ctx_db(ctx)).execute("SELECT proposed_by FROM windows").fetchone()
    assert row == ("agent:default",)


def ctx_db(ctx):
    return ctx.tools["window_propose"].__self__.store._connect().execute("PRAGMA database_list").fetchone()[2]
