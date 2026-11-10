"""Telegram decisions without Telegram: who may act, what a button does, what a message says."""
from datetime import timezone

import pytest

from change_windows.tg import Actions, _ids

NOW = 1_800_000_000
OWNER = 111111111
STRANGER = 222222222


def make(store, approvers=(OWNER,), clock=lambda: NOW, **extra):
    settings = {"approver_telegram_ids": list(approvers), **extra}
    return Actions(store, setting=lambda k, d=None: settings.get(k, d), tz=lambda: timezone.utc, clock=clock)


def propose(store, start=NOW + 600, end=NOW + 4200, scope="k8s-prod", name="web v2", exclusive=True):
    w, _ = store.propose(name=name, scope=scope, start_ts=start, end_ts=end, reason="roll out web v2",
                         proposed_by="agent:test", now=NOW, exclusive=exclusive, cards=[{"task_id": "t_ab12cd", "board": ""}])
    return w


def test_ids_parsing():
    assert _ids([OWNER, str(STRANGER), " 42 ", True, "x", None, 0, "0", -100123]) == [OWNER, STRANGER, 42, -100123]
    assert _ids("") == [] and _ids(None) == [] and _ids(0) == [] and _ids(7) == [7]


def test_empty_approver_list_means_nobody(store):
    w = propose(store)
    actions = make(store, approvers=())
    reply = actions.on_button(OWNER, f"cw:approve:{w.id}")
    assert reply.alert and "Only an approver" in reply.toast and reply.text is None
    assert store.get(w.id).state == "pending"
    assert actions.owner_chat() is None


def test_stranger_cannot_approve_decline_or_close(store):
    w = propose(store)
    actions = make(store)
    for verb in ("approve", "decline", "close"):
        reply = actions.on_button(STRANGER, f"cw:{verb}:{w.id}")
        assert reply.alert and reply.text is None
    for bad_user in (None, str(OWNER), True):
        assert make(store).on_button(bad_user, f"cw:approve:{w.id}").alert
    assert store.get(w.id).state == "pending"


@pytest.mark.parametrize("data", ["cw:approve", "cw:approve:w123abc", "cw:open:W123ABC", "cw:approve:W123ABC:x",
                                  "", None, 42, "cw:approve:W123ABC\n"])
def test_malformed_callback_data(store, data):
    reply = make(store).on_button(OWNER, data)
    assert reply.alert and "Unknown" in reply.toast


def test_approve_then_close(store):
    w = propose(store)
    actions = make(store)
    reply = actions.on_button(OWNER, f"cw:approve:{w.id}")
    assert reply.toast == "Approved." and not reply.alert
    assert store.get(w.id).state == "approved" and store.get(w.id).decided_by == f"telegram:{OWNER}"
    assert reply.buttons == [[("Close now", f"cw:close:{w.id}")]]
    assert "Window approved" in reply.text and f"Decided by telegram:{OWNER}" in reply.text

    open_actions = make(store, clock=lambda: NOW + 700)
    assert store.get(w.id).is_open(NOW + 700)
    reply = open_actions.on_button(OWNER, f"cw:close:{w.id}")
    assert reply.toast.startswith("Closed.") and reply.buttons == []
    assert store.get(w.id).state == "closed" and not store.get(w.id).is_open(NOW + 701)


def test_decline(store):
    w = propose(store)
    reply = make(store).on_button(OWNER, f"cw:decline:{w.id}")
    assert reply.toast == "Declined." and reply.buttons == [] and "Window declined" in reply.text
    assert store.get(w.id).state == "declined"


def test_stale_button_reports_and_redraws_from_the_store(store):
    w = propose(store)
    actions = make(store)
    actions.on_button(OWNER, f"cw:decline:{w.id}")
    reply = actions.on_button(OWNER, f"cw:approve:{w.id}")  # an old message still shows Approve
    assert reply.alert and "declined, not pending" in reply.toast
    assert reply.buttons == [] and "Window declined" in reply.text
    assert store.get(w.id).state == "declined"


def test_unknown_window(store):
    reply = make(store).on_button(OWNER, "cw:approve:WABCDEF")
    assert reply.alert and "unknown window" in reply.toast and reply.text is None


def test_approval_refuses_an_overlap_with_an_approved_window(store):
    a, b = propose(store, name="a"), propose(store, name="b")
    actions = make(store)
    actions.on_button(OWNER, f"cw:approve:{a.id}")
    reply = actions.on_button(OWNER, f"cw:approve:{b.id}")
    assert reply.alert and reply.toast.startswith("Can't approve 'b': it overlaps 'a' (approved, ")
    assert store.get(b.id).state == "pending"
    assert f"Overlaps 'a' (approved, Fri 15 Jan 08:10-09:10 UTC, proposed by test): overlaps 08:10-09:10, 1 h." in reply.text
    assert reply.buttons[-1] == [("Conflicts (1)", f"cw:conflicts:{b.id}")]


def test_an_ended_proposal_cannot_be_approved(store):
    w = propose(store)
    reply = make(store, clock=lambda: NOW + 5000).on_button(OWNER, f"cw:approve:{w.id}")
    assert reply.alert and "already ended" in reply.toast and reply.buttons == []
    assert store.get(w.id).state == "pending"


def test_proposal_message_shows_overlaps_and_buttons(store):
    a = propose(store, name="a")
    b = propose(store, name="b")
    text, buttons = make(store).window_message(store.get(b.id), NOW)
    assert text.startswith("Window proposal: b")
    # a person reads the name, the state, the span, the proposer, the shared span and the rule; the id is last
    assert (f"Overlaps 'a' (pending, Fri 15 Jan 08:10-09:10 UTC, proposed by test): overlaps 08:10-09:10, 1 h. "
            f"Why: same scope k8s-prod; both windows are exclusive. [{a.id}]") in text
    assert "Cards: t_ab12cd" in text and "Reason: roll out web v2" in text and "Proposed by agent:test" in text
    assert text.splitlines()[1] == "scope k8s-prod" and text.splitlines()[-1] == f"id {b.id}"
    assert buttons == [[("Approve", f"cw:approve:{b.id}"), ("Decline", f"cw:decline:{b.id}")],
                       [("Conflicts (1)", f"cw:conflicts:{b.id}")]]
    alone = propose(store, scope="db-prod", name="alone")
    assert make(store).window_message(alone, NOW)[1] == [[("Approve", f"cw:approve:{alone.id}"),
                                                         ("Decline", f"cw:decline:{alone.id}")]]


def test_conflicts_button_explains_each_conflict_and_show_posts_that_window(store):
    a, b, c = propose(store, name="a"), propose(store, name="b"), propose(store, name="c", start=NOW + 3000, end=NOW + 9000)
    actions = make(store)
    actions.on_button(OWNER, f"cw:approve:{a.id}")
    reply = actions.on_button(OWNER, f"cw:conflicts:{b.id}")
    assert not reply.alert and reply.toast == "2 conflicts explained below." and len(reply.messages) == 2
    text, buttons = reply.messages[0]
    assert text.splitlines() == [
        "Conflict 1 of 2 for 'b'",
        "Other window: 'a' (approved)",
        "Fri 15 Jan 08:10-09:10 UTC · scope k8s-prod · proposed by test",
        "Shared span: 08:10-09:10, 1 h",
        "Rule: same scope k8s-prod; both windows are exclusive",
        "What it means: Approving this window is refused while 'a' is approved.",
        "Ways out: Decline one of them, or ask the agent to propose another time.",
        f"id {a.id}"]
    assert buttons == [[("Show", f"cw:show:{a.id}")]]
    assert "Shared span: 08:50-09:10, 20 min" in reply.messages[1][0] and reply.messages[1][1] == [[("Show", f"cw:show:{c.id}")]]
    # Show posts the other window's own message with its own buttons
    shown = actions.on_button(OWNER, f"cw:show:{a.id}")
    assert not shown.alert and shown.text is None and len(shown.messages) == 1
    text, buttons = shown.messages[0]
    assert text.startswith("Window approved: a") and buttons[0] == [("Close now", f"cw:close:{a.id}")]
    # nothing was decided by looking
    assert [store.get(w.id).state for w in (a, b, c)] == ["approved", "pending", "pending"]
    actions.on_button(OWNER, f"cw:decline:{c.id}")
    actions.on_button(OWNER, f"cw:close:{a.id}")
    gone = actions.on_button(OWNER, f"cw:conflicts:{b.id}")
    assert gone.toast == "No conflict any more." and gone.messages == [] and len(gone.buttons) == 1
    assert actions.on_button(OWNER, "cw:show:WABCDEF").alert


def test_conflicts_and_show_are_for_approvers_only(store):
    a, b = propose(store, name="a"), propose(store, name="b")
    for data in (f"cw:conflicts:{b.id}", f"cw:show:{a.id}"):
        reply = make(store).on_button(STRANGER, data)
        assert reply.alert and "Only an approver" in reply.toast and reply.text is None and reply.messages == []


def test_card_titles_are_plain_text_next_to_the_id(store, monkeypatch):
    from change_windows import cards
    monkeypatch.setattr(cards, "_reader", cards.CardReader(lambda tool, args: {"task": {
        "title": "<b>Upgrade</b> *Postgres*\n_now_", "status": "blocked", "assignee": "dba"}}))
    text, _ = make(store).window_message(propose(store), NOW)
    assert "Cards: '<b>Upgrade</b> *Postgres* _now_' (blocked, dba) [t_ab12cd]" in text  # no parse mode: markup stays text


def test_message_text_is_bounded(store):
    w, _ = store.propose(name="n", scope="k8s-prod", start_ts=NOW + 60, end_ts=NOW + 120, reason="r" * 9000,
                         proposed_by="agent:test", now=NOW)
    text, _ = make(store).window_message(w, NOW)
    assert len(text) <= 3510 and text.endswith("[...]")


def test_owner_chat_setting(store):
    assert make(store).owner_chat() == OWNER
    assert make(store, owner_chat_id=-100555).owner_chat() == -100555
    assert make(store, owner_chat_id=0).owner_chat() == OWNER


def test_windows_text_today_and_tomorrow(store):
    actions = make(store)
    assert actions.windows_text() == "No change windows today or tomorrow."
    a = propose(store, name="a")
    b = propose(store, name="b")
    later = propose(store, start=NOW + 5 * 86400, end=NOW + 5 * 86400 + 600, name="later")
    actions.on_button(OWNER, f"cw:approve:{a.id}")
    text = actions.windows_text()
    assert f"approved · Fri 15 Jan 08:10-09:10 UTC · k8s-prod · a · {a.id}" in text
    assert f"· b · {b.id}" in text and "pending · " in text and later.id not in text
    assert "   overlaps 'a' 08:10-09:10, 1 h (same scope k8s-prod; both windows are exclusive)" in text
    assert "open · " in make(store, clock=lambda: NOW + 700).windows_text()


def test_store_errors_other_than_state_propagate(store):
    """A broken store must not look like a decision: the handler raises and PTB logs it."""
    class Broken:
        def get(self, _):
            raise RuntimeError("disk")

        decide = close = get

    actions = Actions(Broken(), setting=lambda k, d=None: [OWNER] if k == "approver_telegram_ids" else d,
                      tz=lambda: None)
    with pytest.raises(RuntimeError):
        actions.on_button(OWNER, "cw:approve:WABCDEF")
