"""The plugin entry point (top-level ``__init__.py``) with a stand-in Hermes context."""
import json
import sqlite3

import pytest

KAPPLY = {"command": "kubectl apply -f deploy.yaml"}


@pytest.mark.parametrize("value", [False, "off", "OFF "])
def test_mode_off_survives_yaml_boolean_parsing(plugin_entry, value):
    # YAML 1.1 reads an unquoted ``mode: off`` as False; it must still switch the guard off.
    hook = plugin_entry({"mode": value}).hooks["pre_tool_call"]
    assert hook(tool_name="terminal", args=KAPPLY) is None


@pytest.mark.parametrize("value", [True, "on", "false", "enfroce", 1, None, ["off"]])
def test_any_other_mode_value_enforces(plugin_entry, value):
    hook = plugin_entry({"mode": value}).hooks["pre_tool_call"]
    assert hook(tool_name="terminal", args=KAPPLY)["action"] == "block"


def test_propose_works_on_a_context_without_profile_name(plugin_entry):
    ctx = plugin_entry()  # the stand-in context has no profile_name attribute
    out = json.loads(ctx.tools["window_propose"](
        {"name": "n", "scope": "k8s-prod", "start": "2999-01-01T10:00+00:00", "duration_minutes": 30, "reason": "r"}))
    assert out["ok"], out
    row = sqlite3.connect(ctx_db(ctx)).execute("SELECT proposed_by FROM windows").fetchone()
    assert row == ("agent:default",)


def ctx_db(ctx):
    return ctx.tools["window_propose"].__self__.store._connect().execute("PRAGMA database_list").fetchone()[2]
