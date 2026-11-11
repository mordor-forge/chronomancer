"""The plugin entry point (top-level ``__init__.py``) with a stand-in Hermes context, the timer
thread, and the manifest."""
import json
import re
import threading
import time
from pathlib import Path

import yaml

import chronomancer
from chronomancer.runtime import Settings, Timer
from chronomancer.tools import SCHEMAS

ROOT = Path(__file__).resolve().parents[1]


def wait_for(check, seconds=3.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.01)
    return False


# ---- register ----

def test_register_wires_the_five_tools_to_one_planner(plugin_entry, kanban):
    kanban.add("t_1", "Upgrade ingress")
    ctx = plugin_entry({"timezone": "Europe/Prague"}, dispatch=kanban, profile_name="assistant")
    assert sorted(ctx.tools) == sorted(s["name"] for s in SCHEMAS) and ctx.hooks == {}
    out = json.loads(ctx.tools["chrono_slot"]({"card": "t_1", "start": "2999-01-01T10:00"}))
    assert out["ok"] and out["slot"]["start"] == "2999-01-01T10:00+01:00" and kanban.status("t_1") == "scheduled"
    plan = json.loads(ctx.tools["chrono_plan"]({"horizon": "week"}))
    assert plan["ok"] and plan["unplanned"] == [] and plan["slots"] == []     # the slot is far beyond a week
    runtime = plugin_entry.module.runtime.get(str(plugin_entry.data_dir))
    (entry,) = runtime.planner.store.log_tail()
    assert entry.actor == "agent:assistant"


def test_the_plan_database_is_prepared_by_hermes_and_lives_in_the_plugin_data_dir(plugin_entry):
    plugin_entry()
    assert plugin_entry.storage.opened == [plugin_entry.data_dir / "plan.db"]
    assert (plugin_entry.data_dir / "plan.db").exists()


def test_a_context_without_profile_name_or_on_unload_still_registers(plugin_entry, kanban):
    kanban.add("t_1", status="blocked")
    ctx = plugin_entry(dispatch=kanban)
    del type(ctx).on_unload
    try:
        ctx = plugin_entry(dispatch=kanban)
    finally:
        type(ctx).on_unload = lambda self, callback: self.unload.append(callback)
    assert json.loads(ctx.tools["chrono_now"]({"card": "t_1"}))["ok"]
    runtime = plugin_entry.module.runtime.get(str(plugin_entry.data_dir))
    assert runtime.planner.store.log_tail()[0].actor == "agent:default"
    runtime.timer.stop()


def test_settings_changed_after_load_apply_on_the_next_call(plugin_entry, kanban):
    kanban.add("t_1", status="blocked")
    ctx = plugin_entry({"default_estimate_minutes": 30}, dispatch=kanban)
    ctx.config["default_estimate_minutes"] = 15
    out = json.loads(ctx.tools["chrono_slot"]({"card": "t_1", "start": "2999-01-01T10:00+00:00"}))
    assert out["slot"]["estimate_minutes"] == 15


def test_the_timer_runs_in_this_process_but_not_in_a_kanban_worker(plugin_entry, monkeypatch):
    plugin_entry()
    runtime = plugin_entry.module.runtime
    key = str(plugin_entry.data_dir)
    first = runtime.get(key)
    assert first.timer is not None and first.timer.running
    plugin_entry()                                                   # a reload replaces the planner and its timer
    second = runtime.get(key)
    assert second is not first and second.timer.running
    assert wait_for(lambda: not first.timer.running)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")
    ctx = plugin_entry()
    assert runtime.get(key).timer is None and sorted(ctx.tools) == sorted(s["name"] for s in SCHEMAS)
    assert wait_for(lambda: not second.timer.running)


def test_unload_stops_the_timer_of_its_own_planner_only(plugin_entry):
    old_ctx = plugin_entry()
    runtime = plugin_entry.module.runtime
    key = str(plugin_entry.data_dir)
    plugin_entry()
    newer = runtime.get(key)
    old_ctx.unload[0]()                                              # the old registration unloads late
    assert runtime.get(key) is newer and newer.timer.running


def test_the_timer_releases_a_due_card_without_any_call(plugin_entry, kanban):
    kanban.add("t_1", status="scheduled")
    ctx = plugin_entry(dispatch=kanban)
    runtime = plugin_entry.module.runtime.get(str(plugin_entry.data_dir))
    planner = runtime.planner
    start = int(time.time()) + 1
    planner.set_slot("t_1", start, actor="owner")                    # set_slot wakes the timer; it sleeps until the start
    assert wait_for(lambda: kanban.status("t_1") == "ready", seconds=8)
    # the pass records the release after the card is released: wait for the record, not only the card
    assert wait_for(lambda: planner.store.log_tail()[0].actor == "timer")
    assert planner.store.get_slot("", "t_1").state == "released" and ctx.hooks == {}


def test_announce_does_nothing_without_hermes_and_sends_one_event_with_it(plugin_entry, monkeypatch):
    import sys
    import types
    plugin_entry()
    runtime = plugin_entry.module.runtime
    runtime.announce()                                               # no hermes_cli here: silently nothing
    sent = []
    events = types.ModuleType("hermes_cli.plugin_events")
    events.broadcast_plugin_event = lambda plugin, event, payload=None: sent.append((plugin, event, payload))
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.plugin_events", events)
    runtime.get(str(plugin_entry.data_dir)).planner.label("repo:web", actor="owner")
    assert sent == [("chronomancer", "plan.changed", {})]


# ---- the timer thread ----

def test_timer_ticks_on_wake_sleeps_until_the_next_due_and_stops():
    ticks, gate = [], threading.Event()

    def tick():
        ticks.append(time.monotonic())
        gate.set()
        return 0.05 if len(ticks) == 2 else None                     # after the second tick: next due in 50 ms

    timer = Timer(tick, idle_s=30, first_s=0.01)
    timer.start()
    assert gate.wait(2) and len(ticks) == 1                          # the first pass, soon after the start
    timer.wake()
    assert wait_for(lambda: len(ticks) >= 3)                         # woken, then again when the next slot is due
    count = len(ticks)
    timer.stop()
    assert wait_for(lambda: not timer.running)
    time.sleep(0.05)
    assert len(ticks) == count


def test_a_failing_pass_does_not_stop_the_timer():
    calls = []

    def tick():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("the board is locked")
        return None

    timer = Timer(tick, idle_s=0.02, first_s=0.01)
    timer.start()
    assert wait_for(lambda: len(calls) >= 3)
    timer.stop()


def test_settings_keep_the_last_values_read_in_scope():
    source = {"board": "ops", "timezone": "Europe/Prague"}
    settings = Settings(lambda key, default: source.get(key, default))
    assert settings.get("board") is None                             # nothing read yet
    settings.refresh()
    assert (settings.get("board"), settings.get("default_estimate_minutes", 60)) == ("ops", 60)
    source.clear()

    def broken(key, default):
        raise RuntimeError("no profile in scope")

    settings._read = broken
    settings.refresh()                                               # an unreadable config keeps the old values
    assert settings.get("board") == "ops"


# ---- the manifest ----

def test_manifest_declares_exactly_what_register_provides():
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    assert manifest["name"] == "chronomancer"
    assert sorted(manifest["provides_tools"]) == sorted(s["name"] for s in SCHEMAS)
    assert manifest["provides_hooks"] == []
    assert manifest["requires_hermes"].startswith(">=")
    assert sorted(manifest["config_schema"]) == sorted(["board", "timezone", "default_estimate_minutes", "comment_on_release"])
    from chronomancer.runtime import SETTING_KEYS
    assert sorted(SETTING_KEYS) == sorted(manifest["config_schema"])


def test_versions_agree():
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    version = str(manifest["version"])
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), "a release version, not a dev marker"
    assert chronomancer.__version__ == version
    dashboard = json.loads((ROOT / "dashboard" / "manifest.json").read_text())
    assert (dashboard["name"], dashboard["version"], dashboard["api"]) == ("chronomancer", version, "plugin_api.py")


def test_the_readme_matches_the_manifest_and_states_the_limits():
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    readme = (ROOT / "README.md").read_text()
    assert f"({manifest['version']})" in readme.split("## Limits")[0]
    for name, spec in manifest["config_schema"].items():
        default = '`""`' if spec["default"] == "" else f"`{str(spec['default']).lower()}`"
        assert f"| `{name}` | {default} |" in readme, name
    tools = readme.split("## Agent tools")[1].split("## The Desktop page")[0]
    assert all(f"`{schema['name']}`" in tools for schema in SCHEMAS)
    assert "hermes -p <profile> plugins enable chronomancer" in readme
    limits = readme.split("## Limits")[1].split("## Install")[0]
    for phrase in ("It advises. It does not enforce.", "The timer needs a running gateway.", "A pin is a courtesy, not a lock.",
                   "An estimate is a guess."):
        assert phrase in limits, phrase
    disclosure = readme.split("## Disclosure")[1].split("## Development")[0]
    for phrase in ("**Network:** none.", "tools.kanban_tools", "card titles", "one daemon thread"):
        assert phrase in disclosure, phrase


def test_the_docs_speak_the_glossary():
    # The words the glossary avoids for the plan's own concepts stay out of what an agent or a person reads.
    from chronomancer import tools as tool_module
    served = " ".join(schema["description"] for schema in tool_module.SCHEMAS).lower()
    for avoided in ("conflict", "time window", "ticket", "wake job"):
        assert avoided not in served, avoided
    glossary = (ROOT / "GLOSSARY.md").read_text()
    for term in ("**Slot**", "**Clash**", "**Horizon**", "**Pin**", "**Release**", "**Label**"):
        assert term in glossary
    assert sorted(p.name[:4] for p in (ROOT / "docs" / "adr").glob("*.md")) == [f"{n:04d}" for n in range(1, 9)]
