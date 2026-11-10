"""Overlaps as a person reads them: the other window by name, the shared span, the rule, the way out."""
from datetime import timezone

import pytest

from change_windows import cards, overlaps
from change_windows.desktop import DesktopApi
from change_windows.store import OverlapError, StoreError
from change_windows.tg import Actions
from change_windows.views import list_view

NOW = 1_794_319_200  # Tue 10 Nov 2026 14:00 UTC
OWNER = 100200300
KEY = "k" * 32


@pytest.fixture
def actions(store):
    settings = {"approver_telegram_ids": [OWNER]}
    return Actions(store, setting=lambda k, d=None: settings.get(k, d), tz=lambda: timezone.utc, clock=lambda: NOW)


def propose(store, name, start, end, *, scope="k8s-prod", exclusive=True, by="agent:ops"):
    return store.propose(name=name, scope=scope, start_ts=NOW + start, end_ts=NOW + end, reason="r",
                         proposed_by=by, now=NOW, exclusive=exclusive)[0]


def test_a_conflict_names_the_other_window_the_span_the_rule_and_the_way_out(store, actions):
    argo = propose(store, "Argo CD upgrade", 0, 3600, by="agent:platform")
    store.decide(argo.id, approve=True, by=f"telegram:{OWNER}", now=NOW - 60, via="telegram")
    web = propose(store, "web v2 rollout", 1800, 5400)
    [fact] = overlaps.describe(actions, store.get(web.id), NOW)
    assert (fact["id"], fact["name"], fact["scope"], fact["state"]) == (argo.id, "Argo CD upgrade", "k8s-prod", "open")
    assert fact["span"] == "Tue 10 Nov 14:00-15:00 UTC" and fact["proposer"] == "platform"
    assert fact["text"] == "overlaps 14:30-15:00, 30 min" and (fact["start_ts"], fact["end_ts"]) == (NOW + 1800, NOW + 3600)
    assert fact["why"] == "same scope k8s-prod; both windows are exclusive"
    assert fact["consequence"] == "Approving this window is refused while 'Argo CD upgrade' is approved."
    assert fact["way_out"] == "Decline one of them, or ask the agent to propose another time."
    # seen from the approved window, the consequence is about the proposal
    [back] = overlaps.describe(actions, store.get(argo.id), NOW)
    assert back["name"] == "web v2 rollout" and back["state"] == "pending"
    assert back["consequence"] == "Approving 'web v2 rollout' is refused while this window is approved."
    # the wording every surface lays out: a chip, a message line, a list row, a panel, a timeline tag
    assert fact["label"] == "Overlaps 'Argo CD upgrade' 14:30-15:00, 30 min"
    assert fact["line"] == ("Overlaps 'Argo CD upgrade' (open, Tue 10 Nov 14:00-15:00 UTC, proposed by profile 'platform'): "
                            f"overlaps 14:30-15:00, 30 min. Why: same scope k8s-prod; both windows are exclusive. [{argo.id}]")
    assert fact["brief"] == "overlaps 'Argo CD upgrade' 14:30-15:00, 30 min (same scope k8s-prod; both windows are exclusive)"
    assert fact["title"] == "This window and 'Argo CD upgrade' overlap"
    assert fact["rows"] == [
        ["Other window", "Argo CD upgrade · open · Tue 10 Nov 14:00-15:00 UTC · scope k8s-prod · proposed by profile 'platform'"],
        ["Shared span", "14:30-15:00, 30 min"],
        ["Rule", "same scope k8s-prod; both windows are exclusive"],
        ["What it means", "Approving this window is refused while 'Argo CD upgrade' is approved."],
        ["Ways out", "Decline one of them, or ask the agent to propose another time."]]
    assert fact["pair"] == "Overlap: 'web v2 rollout' and 'Argo CD upgrade', 14:30-15:00, 30 min" and fact["note"] == "overlap 30 min"


def test_one_function_writes_the_line_the_why_and_the_refusal(store, actions):
    """``explain`` is the only place the wording lives: ``describe`` and ``refusal`` return its parts."""
    from change_windows.store import OverlapError
    a, b = propose(store, "a", 0, 3600), propose(store, "b", 1800, 5400)
    store.decide(a.id, approve=True, by="telegram:1", now=NOW, via="telegram")
    a, b = store.get(a.id), store.get(b.id)
    whole = overlaps.explain(actions, b, [a], NOW)
    assert set(whole) == {"conflicts", "refusal"}
    assert overlaps.describe(actions, b, NOW) == whole["conflicts"]
    assert overlaps.refusal(actions, OverlapError(b, [a]), NOW) == whole["refusal"]
    assert whole["refusal"].startswith("Can't approve 'b': it overlaps 'a' (") and whole["refusal"].endswith(
        ") for 30 min. Decline one of them, or ask the agent to propose another time.")
    assert overlaps.explain(actions, b, [], NOW) == {"conflicts": [], "refusal": ""}
    assert not hasattr(overlaps, "line") and not hasattr(overlaps, "facts")


def test_the_surfaces_hold_no_copy_of_the_wording():
    """Desktop, the Mini App and Telegram lay the explanation out; the sentences are in overlaps.py only."""
    from pathlib import Path
    root = Path(overlaps.__file__).parent
    for path in (root / "miniapp.html", root / "tg.py", root / "miniapp.py", root / "desktop.py", root / "views.py",
                 root.parent / "desktop" / "plugin.js"):
        text = path.read_text()
        for words in ("Overlaps '", "overlaps '", "Shared span", "Other window", "What it means", "Ways out", "This window and",
                      "Can't approve", "is exclusive", "same scope"):
            assert words not in text, (path.name, words)
        if path.suffix in (".js", ".html"):
            assert "roposed by" not in text, path.name  # served as ``proposed``


@pytest.mark.parametrize("mine,theirs,rule", [(True, False, "this window is exclusive"),
                                              (False, True, "that window is exclusive")])
def test_the_rule_says_which_side_is_exclusive(store, actions, mine, theirs, rule):
    propose(store, "a", 0, 3600, exclusive=theirs)
    w = propose(store, "b", 600, 1200, exclusive=mine)
    [fact] = overlaps.describe(actions, w, NOW)
    assert fact["why"] == f"same scope k8s-prod; {rule}" and fact["length"] == "10 min"
    assert fact["consequence"].startswith("Only one of them can be approved")


def test_shared_windows_and_finished_windows_have_no_conflicts(store, actions):
    a = propose(store, "a", 0, 3600, exclusive=False)
    b = propose(store, "b", 600, 1200, exclusive=False)
    assert overlaps.describe(actions, a, NOW) == [] and overlaps.describe(actions, b, NOW) == []
    c = propose(store, "c", 0, 3600, scope="infra")
    d = propose(store, "d", 0, 3600, scope="infra")
    declined = store.decide(d.id, approve=False, by="telegram:1", now=NOW, via="telegram")
    assert overlaps.describe(actions, declined, NOW) == [] and overlaps.describe(actions, store.get(c.id), NOW) == []


def test_an_overlap_on_another_day_names_the_day_and_long_spans_use_hours(store, actions):
    propose(store, "a", 30 * 3600, 34 * 3600)
    w = propose(store, "b", 31 * 3600, 33 * 3600 + 1800)
    [fact] = overlaps.describe(actions, w, NOW)
    assert fact["text"] == "overlaps Wed 11 Nov 21:00-23:30, 2 h 30 min"
    assert overlaps.duration(3600) == "1 h" and overlaps.duration(20) == "1 min"


def test_the_refusal_is_a_sentence_about_names_not_ids(store, actions):
    argo = propose(store, "Argo CD upgrade", 0, 3600)
    store.decide(argo.id, approve=True, by="telegram:1", now=NOW, via="telegram")
    web = propose(store, "web v2 rollout", 1800, 5400)
    with pytest.raises(OverlapError) as caught:
        store.decide(web.id, approve=True, by="telegram:1", now=NOW, via="telegram")
    assert isinstance(caught.value, StoreError)
    assert str(caught.value) == f"window {web.id} overlaps approved {argo.id}"  # the id form stays for logs
    text = overlaps.refusal(actions, caught.value, NOW)
    assert text == ("Can't approve 'web v2 rollout': it overlaps 'Argo CD upgrade' (open, 14:00-15:00, k8s-prod) "
                    "for 30 min. Decline one of them, or ask the agent to propose another time.")
    # every owner channel answers with it
    assert DesktopApi(actions).act(web.id, "approve", KEY) == (409, {"detail": text})
    reply = actions.on_button(OWNER, f"cw:approve:{web.id}")
    assert reply.alert and reply.toast == text[:200]
    assert store.get(web.id).state == "pending"


def test_a_refusal_with_two_clashes_and_a_long_name_stays_readable(store, actions):
    one = propose(store, "n" * 100, 0, 1800, exclusive=False)
    two = propose(store, "second", 1800, 3600, exclusive=False)
    for w in (one, two):
        store.decide(w.id, approve=True, by="telegram:1", now=NOW, via="telegram")
    late = propose(store, "late", 900, 2700)
    with pytest.raises(OverlapError) as caught:
        store.decide(late.id, approve=True, by="telegram:1", now=NOW, via="telegram")
    text = overlaps.refusal(actions, caught.value, NOW)
    assert "…' (open, 14:00-14:30, k8s-prod) for 15 min and 'second' (approved, 14:30-15:00, k8s-prod) for 15 min." in text
    assert "n" * 60 not in text


def test_the_owner_view_carries_conflicts_proposer_stamps_and_the_two_days(store, actions):
    a = propose(store, "a", 0, 3600)
    b = propose(store, "b", 1800, 5400, by="owner")
    store.decide(a.id, approve=True, by="desktop", now=NOW - 120, via="desktop")
    data = list_view(actions)
    rows = {w["id"]: w for w in data["windows"]}
    assert rows[b.id]["overlaps"] == [a.id] and rows[b.id]["conflicts"][0]["name"] == "a"
    assert rows[a.id]["proposer"] == "ops" and rows[b.id]["proposer"] == "owner"
    # "Proposed by default" read as "by default": an agent profile is named as a profile, in quotes
    assert rows[a.id]["proposed"] == "proposed by profile 'ops'" and rows[b.id]["proposed"] == "proposed by owner"
    assert overlaps.proposed("agent:default") == "proposed by profile 'default'" and overlaps.proposed("") == ""
    assert rows[a.id]["starts"] == "Tue 10 Nov 14:00" and rows[a.id]["decided_when"] == "Tue 10 Nov 13:58"
    assert rows[b.id]["decided_when"] == "" and rows[b.id]["closed_when"] == "" and rows[b.id]["card_details"] == []
    assert data["zone"] == "UTC" and [d["label"] for d in data["days"]] == ["Tue 10 Nov", "Wed 11 Nov"]
    assert data["days"][0]["end"] - data["days"][0]["start"] == 86400 and data["days"][0]["start"] == data["start"]
    assert data["days"][1]["start"] == data["days"][0]["end"]


# -- linked cards -------------------------------------------------------------------------------------

def test_card_titles_come_from_kanban_show_and_are_cached(monkeypatch):
    calls = []
    clock = [0.0]

    def dispatch(tool, args):
        calls.append((tool, args))
        if args["task_id"] == "t_gone":
            return '{"error": "task t_gone not found"}'
        return {"task": {"id": args["task_id"], "title": "Upgrade‮ Postgres\n16.4 " + "x" * 200, "status": "blocked",
                         "assignee": "dba", "body": "never shown"}}

    reader = cards.CardReader(dispatch, clock=lambda: clock[0])
    info = reader.info({"task_id": "t_aa0001", "board": "ops"})
    assert calls == [("kanban_show", {"task_id": "t_aa0001", "board": "ops"})]
    assert set(info) == {"task_id", "title", "status", "assignee"} and info["status"] == "blocked" and info["assignee"] == "dba"
    assert info["title"].startswith("Upgrade Postgres 16.4 x") and len(info["title"]) == 120 and info["title"].endswith("…")
    assert reader.info({"task_id": "t_aa0001", "board": "ops"}) == info and len(calls) == 1  # cached
    assert reader.info({"task_id": "t_gone", "board": ""}) is None
    assert calls[-1] == ("kanban_show", {"task_id": "t_gone"})
    clock[0] = cards.TTL_S + 1
    reader.info({"task_id": "t_aa0001", "board": "ops"})
    assert len(calls) == 3
    monkeypatch.setattr(cards, "_reader", reader)
    assert [c["task_id"] for c in cards.details([{"task_id": "t_aa0001", "board": "ops"}, {"task_id": "t_gone"}])] == ["t_aa0001"]


def test_without_the_kanban_tools_cards_keep_their_ids_only():
    def broken(tool, args):
        raise RuntimeError("kanban tools are not loaded")

    assert cards.CardReader(None).info({"task_id": "t_aa0001"}) is None
    assert cards.CardReader(broken).info({"task_id": "t_aa0001"}) is None
    assert cards.CardReader(lambda *_: "not json").info({"task_id": "t_aa0001"}) is None
    assert cards.CardReader(lambda *_: {}).info("t_aa0001") is None
    assert cards.details(None) == []


def test_the_plugin_entry_wires_the_card_reader_to_dispatch_tool(plugin_entry, monkeypatch):
    seen = []
    monkeypatch.setattr(cards, "_reader", cards.CardReader())
    ctx = plugin_entry({}, dispatch_tool=lambda tool, args: seen.append((tool, args)) or
                       {"task": {"title": "Card title", "status": "ready", "assignee": None}})
    import sys
    module = next(m for name, m in sys.modules.items() if name.endswith(".change_windows.cards") and "cw_entry" in name)
    assert module.details([{"task_id": "t_aa0001", "board": ""}]) == [
        {"task_id": "t_aa0001", "title": "Card title", "status": "ready", "assignee": ""}]
    assert seen == [("kanban_show", {"task_id": "t_aa0001"})] and ctx.tools
