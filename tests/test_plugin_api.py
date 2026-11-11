"""The dashboard routes: HTTP in, the Desktop API out, with a stand-in for Hermes's storage."""
import importlib.util
import sys
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from conftest import ROOT, at


@pytest.fixture
def client(tmp_path, monkeypatch, plugin_entry, kanban):
    """The dashboard backend next to a loaded agent half, as in the gateway."""
    plugin_entry({"timezone": "Europe/Prague"}, dispatch=kanban)
    spec = importlib.util.spec_from_file_location("chrono_dashboard_under_test", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/chronomancer")
    http = TestClient(app)
    http.module = module
    return http


BASE = "/api/plugins/chronomancer"


def test_the_backend_uses_the_planner_of_the_agent_half(client, plugin_entry, kanban):
    kanban.add("t_1", "Upgrade ingress", status="blocked", assignee="builder")
    runtime = plugin_entry.module.runtime.get(str(plugin_entry.data_dir))
    assert client.module._api().planner is runtime.planner
    response = client.get(f"{BASE}/plan?horizon=week")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["board_error"] == "" and [c["title"] for c in body["tray"]] == ["Upgrade ingress"]


def test_the_owner_plans_through_http(client, plugin_entry, kanban):
    kanban.add("t_1", "Upgrade ingress", status="ready", assignee="builder")
    start = at(14, day=11) + 400 * 86400
    made = client.post(f"{BASE}/cards/t_1/slot", json={"start": start, "estimate_minutes": 30})
    assert made.status_code == 200 and made.json()["change"]["action"] == "plan" and kanban.status("t_1") == "scheduled"
    assert client.post(f"{BASE}/cards/t_1/pin", json={"pinned": True}).json()["change"]["slot"]["pinned_by_owner"] is True
    assert client.post(f"{BASE}/labels", json={"name": "cluster:prod", "exclusive": True}).status_code == 200
    assert client.post(f"{BASE}/cards/t_1/labels", json={"add": ["cluster:prod"]}).json() == {"labels": ["cluster:prod"]}
    assert client.delete(f"{BASE}/labels/cluster:prod").json() == {"deleted": "cluster:prod"}
    assert client.post(f"{BASE}/cards/t_1/now").status_code == 200 and kanban.status("t_1") == "ready"
    assert client.delete(f"{BASE}/cards/t_1/slot").status_code == 200
    planner = plugin_entry.module.runtime.get(str(plugin_entry.data_dir)).planner
    assert planner.store.slots("") == [] and {e.actor for e in planner.store.log_tail()} == {"owner"}


def test_errors_are_http_answers(client, kanban):
    kanban.add("t_run", status="running")
    refused = client.post(f"{BASE}/cards/t_run/slot", json={"start": "2999-01-01T10:00"})
    assert refused.status_code == 409 and "is running now" in refused.json()["detail"]
    assert client.post(f"{BASE}/cards/t_run/pin", json={}).status_code == 400
    assert client.post(f"{BASE}/cards/t_9/now").status_code == 409
    assert client.post(f"{BASE}/cards/t_run/slot").status_code == 409           # no body


@pytest.fixture
def alone(tmp_path, monkeypatch, kanban):
    """The dashboard backend in its own process: no agent half, Hermes's modules as stand-ins."""
    storage = types.ModuleType("plugins.plugin_storage")
    storage.plugin_data_dir = lambda name: tmp_path / "plugin-data" / name
    storage.plugin_db = lambda name, filename="data.db": __import__("sqlite3").connect(tmp_path / "plugin-data" / name / filename)
    (tmp_path / "plugin-data" / "chronomancer").mkdir(parents=True)
    config = types.ModuleType("hermes_cli.config")
    config.load_config_readonly = lambda: {"plugins": {"entries": {"chronomancer": {"settings": {"timezone": "Europe/Prague", "board": "ops"}}}}}
    registry_module = types.ModuleType("tools.registry")
    registry_module.registry = types.SimpleNamespace(dispatch=lambda tool, args: kanban(tool, args))
    for name, module in (("plugins", types.ModuleType("plugins")), ("plugins.plugin_storage", storage),
                         ("hermes_cli", types.ModuleType("hermes_cli")), ("hermes_cli.config", config),
                         ("tools", types.ModuleType("tools")), ("tools.kanban_tools", types.ModuleType("tools.kanban_tools")),
                         ("tools.registry", registry_module)):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.delitem(sys.modules, "hermes_chronomancer_core", raising=False)
    spec = importlib.util.spec_from_file_location("chrono_dashboard_alone", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router, prefix=BASE)
    http = TestClient(app)
    http.module = module
    yield http
    for mod in [m for m in sys.modules if m.startswith("hermes_chronomancer_core")]:
        sys.modules.pop(mod, None)


def test_without_the_agent_half_the_backend_plans_through_the_kanban_tools_of_its_process(alone, kanban):
    kanban.add("t_1", "Upgrade ingress", status="ready", assignee="builder")
    body = alone.get(f"{BASE}/plan").json()
    assert (body["board"], body["board_error"]) == ("ops", "") and body["zone"] in ("CET", "CEST")
    assert [c["title"] for c in body["tray"]] == ["Upgrade ingress"]
    assert {args.get("board") for _, args in kanban.calls} == {"ops"}             # the configured board
    made = alone.post(f"{BASE}/cards/t_1/slot", json={"start": "2999-01-01T10:00"})
    assert made.status_code == 200 and kanban.status("t_1") == "scheduled"
    runtime = sys.modules["hermes_chronomancer_core.runtime"]
    (active,) = [r for r in runtime._RUNTIMES.values() if r.planner is alone.module._api().planner]
    assert active.timer is None                                                    # the timer stays in the gateway


def test_a_dashboard_process_without_kanban_tools_still_serves_the_plan(alone, kanban, monkeypatch):
    monkeypatch.delitem(sys.modules, "tools.kanban_tools")
    monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))           # importing the kanban tools fails
    body = alone.get(f"{BASE}/plan").json()
    assert "kanban_list failed (ModuleNotFoundError)" in body["board_error"] and body["items"] == [] and body["tray"] == []
    refused = alone.post(f"{BASE}/cards/t_1/slot", json={"start": "2999-01-01T10:00"})
    assert refused.status_code == 409 and "cannot be reached" in refused.json()["detail"]


def test_a_broken_store_is_a_503_without_details(client, monkeypatch):
    monkeypatch.setattr(client.module, "_api", lambda: (_ for _ in ()).throw(OSError("/secret/path")))
    response = client.get(f"{BASE}/plan")
    assert response.status_code == 503 and response.json() == {"detail": "The plan is unavailable; retry shortly."}
