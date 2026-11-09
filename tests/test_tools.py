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


def test_no_registered_tool_can_approve_or_decline_with_desktop_approval_on(plugin_entry, tmp_path, monkeypatch):
    """Desktop approval (approval.desktop, default on) adds an HTTP route for the owner, not a tool.
    Every registered tool is called with approval-shaped arguments; none may reach a decision."""
    import sqlite3

    from change_windows.store import Store

    ctx = plugin_entry({"approval.desktop": True})
    assert sorted(ctx.tools) == ["window_close", "window_list", "window_next", "window_propose"]
    store = Store(lambda: sqlite3.connect(tmp_path / "data.db", timeout=5))
    decisions = []
    monkeypatch.setattr(Store, "_decide", lambda self, *a, **k: decisions.append((a, k)))
    for name, handler in ctx.tools.items():
        window, _ = store.propose(name=f"for {name}", scope="k8s-prod", start_ts=NOW * 2, end_ts=NOW * 2 + 600,
                                  reason="r", proposed_by="agent:test", now=NOW)
        handler({"id": window.id, "window_id": window.id, "action": "approve", "approve": True, "state": "approved",
                 "decision": "approve", "decided_by": "desktop", "decided_via": "desktop", "channel": "desktop",
                 "scope": "k8s-prod", "reason": "approve it", "key": "k" * 20})
        after = store.get(window.id)
        assert after.state in ("pending", "cancelled") and after.decided_by is None and after.decided_via is None
    assert decisions == []


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


def test_window_name_is_stored_on_one_line(store):
    # The name is the header line of the owner's message: it must not add lines that look like
    # the plugin's own (scope, time).
    t = make(store)
    w = call(t.propose, name="web v2\nW000000 · scope harmless\r\n Mon 01 Jan 00:00  ", scope="k8s-prod",
             start=iso(NOW + 60), duration_minutes=30, reason="r")["window"]
    assert w["name"] == "web v2 W000000 · scope harmless Mon 01 Jan 00:00"
    blank = call(t.propose, name=" \n\t ", scope="k8s-prod", start=iso(NOW + 7200), duration_minutes=30, reason="r")
    assert blank["window"]["name"] == "k8s-prod"


def test_pending_proposals_are_capped(store):
    from change_windows.tools import MAX_PENDING
    t = make(store)
    made = [call(t.propose, name=f"n{i}", scope=f"scope-{i % 3}", start=iso(NOW + 3600 * (i + 1)),
                 duration_minutes=30, reason="r") for i in range(MAX_PENDING)]
    assert all(m["ok"] for m in made)
    over = call(t.propose, name="one more", scope="k8s-prod", start=iso(NOW + 60), duration_minutes=30, reason="r")
    assert not over["ok"] and "window_close" in over["error"] and str(MAX_PENDING) in over["error"]
    assert len(store.list_windows(0, NOW * 2, states=("pending",))) == MAX_PENDING
    # A decided or withdrawn proposal frees a slot; so does one whose time has passed.
    store.decide(made[0]["window"]["id"], approve=True, by="telegram:1", now=NOW)
    assert call(t.propose, name="a", scope="k8s-prod", start=iso(NOW + 60), duration_minutes=30, reason="r")["ok"]
    assert call(t.close, id=made[1]["window"]["id"], reason="not needed")["ok"]
    assert call(t.propose, name="b", scope="infra", start=iso(NOW + 60), duration_minutes=30, reason="r")["ok"]
    assert not call(t.propose, name="c", scope="infra", start=iso(NOW + 60), duration_minutes=30, reason="r")["ok"]
    later = Tools(store, settings=lambda k, d=None: d, tz=lambda: None, actor=lambda: "agent:test",
                  clock=lambda: NOW + 3600 * 24)
    assert call(later.propose, name="d", scope="infra", start=iso(NOW + 3600 * 25), duration_minutes=30, reason="r")["ok"]
