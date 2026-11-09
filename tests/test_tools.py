import json

from change_windows.tools import Tools

NOW = 1_800_000_000


def make(store):
    return Tools(store, settings=lambda k, d=None: d, tz=lambda: None, actor=lambda: "agent:test", clock=lambda: NOW)


def call(fn, **args):
    return json.loads(fn(args))


def iso(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def test_propose_is_pending_and_flags_conflicts(store):
    t = make(store)
    a = call(t.propose, name="a", scope="k8s-prod", start=iso(NOW + 3600), duration_minutes=60, reason="upgrade")
    assert a["ok"] and a["window"]["state"] == "pending" and a["conflicts"] == []
    b = call(t.propose, name="b", scope="k8s-prod", start=iso(NOW + 3600), end=iso(NOW + 5400), reason="rotate",
             cards=["t_ab12cd34"])
    assert [c["id"] for c in b["conflicts"]] == [a["window"]["id"]]
    assert b["window"]["cards"] == [{"task_id": "t_ab12cd34", "board": ""}]


def test_propose_validation(store):
    t = make(store)
    base = dict(name="x", scope="k8s-prod", reason="r")
    assert not call(t.propose, **base, start=iso(NOW - 3600), duration_minutes=10)["ok"]
    assert not call(t.propose, **base, start=iso(NOW + 60), duration_minutes=60 * 24)["ok"]
    assert not call(t.propose, **{**base, "scope": "_self"}, start=iso(NOW + 60), duration_minutes=10)["ok"]
    assert not call(t.propose, **{**base, "scope": "Bad Scope"}, start=iso(NOW + 60), duration_minutes=10)["ok"]
    assert not call(t.propose, **base, start=iso(NOW + 60), duration_minutes=10, cards=["rm -rf"])["ok"]
    assert not call(t.propose, **base, start="tomorrow", duration_minutes=10)["ok"]


def test_next_and_close(store):
    t = make(store)
    w = call(t.propose, name="a", scope="k8s-prod", start=iso(NOW + 60), duration_minutes=30, reason="r")["window"]
    nxt = call(t.next, scope="k8s-prod")
    assert nxt["open"] == [] and nxt["next"] is None and [p["id"] for p in nxt["pending"]] == [w["id"]]
    closed = call(t.close, id=w["id"], reason="not needed")
    assert closed["window"]["state"] == "cancelled"


def test_no_tool_can_approve():
    from change_windows.tools import SCHEMAS
    assert not any("approve" in s["name"] or "open" in s["name"] for s in SCHEMAS)


def test_agent_cannot_propose_a_window_that_needs_a_check_in(store):
    # No surface lets the owner check in yet, so such a window could never open.
    from change_windows.tools import PROPOSE
    assert "presence_required" not in PROPOSE["parameters"]["properties"]
    t = make(store)
    w = call(t.propose, name="a", scope="k8s-prod", start=iso(NOW + 60), duration_minutes=30, reason="r",
             presence_required=True)["window"]
    assert w["presence_required"] is False
    store.decide(w["id"], approve=True, by="telegram:1", now=NOW)
    assert [o.id for o in store.open_windows("k8s-prod", NOW + 120)] == [w["id"]]
