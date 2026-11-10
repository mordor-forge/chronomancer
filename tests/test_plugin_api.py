"""``dashboard/plugin_api.py`` over real HTTP (FastAPI's test client), loaded by path as Hermes does.

Needs ``fastapi`` and ``httpx``, which Hermes ships; without them this file is skipped and
``test_desktop_api.py`` still covers the logic behind every route.
"""
import importlib.util
import sqlite3
import sys
import time
import types
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from change_windows.store import Store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
KEY = "route-key-0000000001"


class Env:
    def __init__(self, tmp_path, monkeypatch):
        self.home = tmp_path / "home"
        self.config = {"plugins": {"entries": {"change-windows": {"settings": {"timezone": "UTC"}}}}}
        self.config_error = None
        storage = types.ModuleType("plugins.plugin_storage")
        storage.plugin_data_dir = self._data_dir
        storage.plugin_db = lambda name: sqlite3.connect(self._data_dir(name) / "data.db", timeout=5,
                                                         check_same_thread=False)
        hermes_config = types.ModuleType("hermes_cli.config")
        hermes_config.load_config_readonly = self._load
        for name, module in (("plugins", types.ModuleType("plugins")), ("plugins.plugin_storage", storage),
                             ("hermes_cli", types.ModuleType("hermes_cli")), ("hermes_cli.config", hermes_config)):
            monkeypatch.setitem(sys.modules, name, module)
        monkeypatch.delitem(sys.modules, "hermes_change_windows_core", raising=False)
        spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_change-windows",
                                                      ROOT / "dashboard" / "plugin_api.py")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        app = FastAPI()
        app.include_router(self.module.router, prefix="/api/plugins/change-windows")
        self.client = TestClient(app)
        self.store = Store(lambda: sqlite3.connect(self._data_dir("change-windows") / "data.db", timeout=5))

    def _data_dir(self, name):
        path = self.home / "plugin-data" / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _load(self):
        if self.config_error:
            raise self.config_error
        return self.config

    @property
    def settings(self):
        return self.config["plugins"]["entries"]["change-windows"]["settings"]

    def propose(self, start=600, end=4200, **extra):
        now = int(time.time())
        values = dict(name="web v2", scope="k8s-prod", start_ts=now + start, end_ts=now + end, reason="roll out",
                      proposed_by="agent:test", now=now, cards=[{"task_id": "t_ab12cd", "board": ""}])
        values.update(extra)
        return self.store.propose(**values)[0]

    def get(self, path):
        return self.client.get(f"/api/plugins/change-windows{path}")

    def post(self, window_id, action, key=KEY, **kwargs):
        if "json" not in kwargs and "content" not in kwargs:
            kwargs["json"] = {"key": key}
        return self.client.post(f"/api/plugins/change-windows/windows/{window_id}/{action}", **kwargs)


@pytest.fixture
def env(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    yield env
    for name in [m for m in sys.modules if m == "hermes_change_windows_core" or m.startswith("hermes_change_windows_core.")]:
        sys.modules.pop(name, None)


def test_the_router_has_exactly_the_documented_routes(env):
    routes = sorted((sorted(r.methods - {"HEAD"}), r.path) for r in env.module.router.routes)
    assert routes == [(["GET"], "/audit"), (["GET"], "/windows"), (["POST"], "/windows/{window_id}/{action}")]


def test_get_windows(env):
    w = env.propose()
    response = env.get("/windows")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["approval"] is True and [row["id"] for row in body["windows"]] == [w.id]
    assert body["windows"][0]["span"].endswith("UTC") and body["windows"][0]["cards"][0]["task_id"] == "t_ab12cd"


@pytest.mark.parametrize("action,state", [("approve", "approved"), ("decline", "declined")])
def test_post_approve_and_decline_record_the_desktop_channel(env, action, state):
    w = env.propose()
    response = env.post(w.id, action)
    assert response.status_code == 200 and response.json()["window"]["state"] == state
    saved = env.store.get(w.id)
    assert (saved.state, saved.decided_via, saved.decided_by) == (state, "desktop", "desktop")


def test_post_close(env):
    w = env.propose(start=-60)
    assert env.post(w.id, "approve").status_code == 200
    response = env.post(w.id, "close", key=KEY + "b")
    assert response.status_code == 200 and response.json()["window"]["state"] == "closed"
    assert env.store.get(w.id).close_reason == "desktop: closed from Desktop"


def test_post_is_idempotent_per_key(env):
    w = env.propose()
    first, second = env.post(w.id, "approve"), env.post(w.id, "approve")
    assert first.status_code == second.status_code == 200 and first.json() == second.json()
    reused = env.post(w.id, "close")
    assert reused.status_code == 409 and "idempotency key" in reused.json()["detail"]
    assert env.store.get(w.id).state == "approved"


@pytest.mark.parametrize("value", [False, "false", 0])
def test_approval_desktop_false_is_a_403_for_approve_and_decline_but_not_close(env, value):
    env.settings["approval"] = {"desktop": value}
    w = env.propose()
    assert env.get("/windows").json()["approval"] is False
    for action in ("approve", "decline"):
        response = env.post(w.id, action, key=f"{KEY}-{action}")
        assert response.status_code == 403 and "approval.desktop" in response.json()["detail"]
    assert env.store.get(w.id).state == "pending"
    assert env.post(w.id, "close").json()["window"]["state"] == "cancelled"


def test_approval_setting_is_also_read_as_one_literal_key_and_from_the_legacy_subtree(env):
    w = env.propose()
    env.settings["approval.desktop"] = False
    assert env.post(w.id, "approve").status_code == 403
    del env.settings["approval.desktop"]
    env.config["plugins"]["entries"]["change-windows"]["config"] = {"approval": {"desktop": False}}
    assert env.post(w.id, "approve", key=KEY + "b").status_code == 403
    env.settings["approval"] = {"desktop": True}  # ``settings`` wins over the legacy subtree
    assert env.post(w.id, "approve", key=KEY + "c").status_code == 200


def test_unreadable_config_turns_approval_off_and_reads_report_503(env):
    w = env.propose()
    env.config_error = OSError("synthetic: config unreadable")
    assert env.post(w.id, "approve").status_code == 403
    response = env.get("/windows")  # the time zone cannot be read either
    assert response.status_code == 503 and "synthetic" not in response.text
    assert env.store.get(w.id).state == "pending"


@pytest.mark.parametrize("kwargs", [{"json": {}}, {"json": {"key": "short"}}, {"json": {"key": 12345678901234567}},
                                    {"json": ["k" * 20]}, {"json": "k" * 20}, {"content": b""},
                                    {"json": {"Key": "k" * 20}}])
def test_bad_or_missing_key_is_a_400(env, kwargs):
    w = env.propose()
    response = env.post(w.id, "approve", **kwargs)
    assert response.status_code == 400 and env.store.get(w.id).state == "pending"


def test_a_body_that_is_not_json_is_refused_before_the_route(env):
    w = env.propose()
    response = env.post(w.id, "approve", content=b"{not json", headers={"Content-Type": "application/json"})
    assert response.status_code == 422 and env.store.get(w.id).state == "pending"


def test_unknown_id_bad_id_and_unknown_action(env):
    w = env.propose()
    assert env.post("WFFFFFF", "approve").status_code == 404
    assert env.post("not-an-id", "approve").status_code == 400
    assert env.post(w.id, "open").status_code == 404
    assert env.client.get(f"/api/plugins/change-windows/windows/{w.id}/approve").status_code == 405
    assert env.client.post("/api/plugins/change-windows/windows", json={"name": "x"}).status_code == 405
    assert env.store.get(w.id).state == "pending"


def test_an_expired_proposal_cannot_be_approved(env):
    w = env.propose(start=-600, end=-1)
    response = env.post(w.id, "approve")
    assert response.status_code == 409 and "has already ended" in response.json()["detail"]
    assert env.store.get(w.id).state == "pending"


def test_a_proposal_that_overlaps_an_approved_exclusive_window_cannot_be_approved(env):
    first, second = env.propose(), env.propose(name="b", start=1200, end=1800, exclusive=False)
    assert env.post(first.id, "approve").status_code == 200
    response = env.post(second.id, "approve", key=KEY + "b")
    detail = response.json()["detail"]
    assert response.status_code == 409 and detail.startswith("Can't approve 'b': it overlaps 'web v2' (approved, ")
    assert "k8s-prod) for 10 min. Decline one of them" in detail and first.id not in detail
    assert env.store.get(second.id).state == "pending"
    assert env.post(second.id, "decline", key=KEY + "c").status_code == 200


def test_get_audit_is_metadata_only_with_decisions(env):
    w = env.propose()
    env.store.record_audit(now=int(time.time()), verdict="blocked", tool="terminal", rule_ids=["k8s-write"],
                           scopes=["k8s-prod"], preview="kubectl apply SYNTHETIC-PREVIEW")
    env.post(w.id, "approve")
    response = env.get("/audit")
    body = response.json()
    assert response.status_code == 200 and "SYNTHETIC-PREVIEW" not in response.text
    assert body["audit"][0]["tool"] == "terminal" and "preview" not in body["audit"][0]
    assert [(d["window_id"], d["decided_via"]) for d in body["decisions"]] == [(w.id, "desktop")]


def test_each_profile_data_directory_gets_its_own_store(env, tmp_path):
    first = env.propose()
    assert [w["id"] for w in env.get("/windows").json()["windows"]] == [first.id]
    env.home = tmp_path / "other-profile"
    assert env.get("/windows").json()["windows"] == []  # a fresh file: the schema is created for it too
    assert env.post(first.id, "approve").status_code == 404


def test_a_loaded_agent_half_of_this_checkout_is_reused_so_its_scheduler_is_woken(env, monkeypatch):
    name = "hermes_plugins.change_windows"
    spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    agent_half = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "hermes_plugins", types.ModuleType("hermes_plugins"))
    monkeypatch.setitem(sys.modules, name, agent_half)
    spec.loader.exec_module(agent_half)
    try:
        assert env.module._core_package() == f"{name}.change_windows"
        tg = sys.modules[f"{name}.change_windows.tg"]
        woken = []
        monkeypatch.setattr(tg, "wake_all", lambda: woken.append(1))
        env.module._APIS.clear()
        w = env.propose()
        assert env.post(w.id, "approve").status_code == 200 and woken == [1]
    finally:
        for mod in [m for m in sys.modules if m.startswith(name + ".")]:
            sys.modules.pop(mod, None)


def test_another_plugin_under_the_same_namespace_is_not_mistaken_for_this_one(env, monkeypatch, tmp_path):
    other = types.ModuleType("hermes_plugins.change_windows")
    other.__file__ = str(tmp_path / "elsewhere" / "__init__.py")
    monkeypatch.setitem(sys.modules, "hermes_plugins.change_windows", other)
    assert env.module._core_package() == "hermes_change_windows_core"
