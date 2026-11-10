"""The Desktop owner surface without a web framework: every route's logic and status code."""
import json
import sqlite3
from datetime import timezone

import pytest

from change_windows import desktop
from change_windows.desktop import DesktopApi, approval_enabled
from change_windows.store import Store
from change_windows.tg import Actions

NOW = 1_800_000_000
OWNER = 111111111
KEY = "desktop-key-00000001"


class Harness:
    def __init__(self, store, **settings):
        self.store = store
        self.settings = {"approver_telegram_ids": [OWNER], **settings}
        self.now = NOW
        self.wakes = 0
        self.actions = Actions(store, setting=lambda k, d=None: self.settings.get(k, d),
                               tz=lambda: timezone.utc, clock=lambda: self.now)
        self.api = DesktopApi(self.actions, wake=self._wake)

    def _wake(self):
        self.wakes += 1

    def propose(self, start=600, end=4200, **extra):
        values = dict(name="web v2", scope="k8s-prod", start_ts=NOW + start, end_ts=NOW + end, reason="roll out web v2",
                      proposed_by="agent:test", now=NOW, cards=[{"task_id": "t_ab12cd", "board": ""}])
        values.update(extra)
        return self.store.propose(**values)[0]


@pytest.fixture
def h(store):
    return Harness(store)


# -- list -------------------------------------------------------------------------------------------

def test_list_returns_display_fields_overlaps_cards_and_the_approval_flag(h):
    a = h.propose()
    b = h.propose(name="db", start=1200, end=1800)
    other = h.propose(name="infra", scope="infra")
    status, body = h.api.windows()
    assert status == 200 and body["now"] == NOW and body["approval"] is True
    rows = {w["id"]: w for w in body["windows"]}
    assert set(rows) == {a.id, b.id, other.id}
    assert rows[a.id]["overlaps"] == [b.id] and rows[b.id]["overlaps"] == [a.id] and rows[other.id]["overlaps"] == []
    assert rows[a.id]["cards"] == [{"task_id": "t_ab12cd", "board": ""}] and rows[a.id]["state"] == "pending"
    assert rows[a.id]["open"] is False and "UTC" in rows[a.id]["span"]
    # explicit projection: identities and close reasons stay in the store
    assert not {"proposed_by", "decided_by", "close_reason", "created_at"} & set(rows[a.id])
    json.dumps(body)


def test_list_marks_an_open_window_and_keeps_finished_ones(h):
    live = h.propose(start=0, end=3600)
    gone = h.propose(name="old", scope="infra", start=0, end=3600)
    h.store.decide(live.id, approve=True, by="telegram:1", now=NOW, via="telegram")
    h.store.decide(gone.id, approve=False, by="telegram:1", now=NOW, via="telegram")
    rows = {w["id"]: w for w in h.api.windows()[1]["windows"]}
    assert rows[live.id]["open"] is True and rows[live.id]["decided_via"] == "telegram"
    assert rows[gone.id]["state"] == "declined"


# -- actions ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("action,state", [("approve", "approved"), ("decline", "declined")])
def test_approve_and_decline_record_the_desktop_channel(h, action, state):
    w = h.propose()
    status, body = h.api.act(w.id, action, KEY)
    assert status == 200 and body["window"]["state"] == state and body["window"]["decided_via"] == "desktop"
    saved = h.store.get(w.id)
    assert (saved.state, saved.decided_via, saved.decided_by, saved.decided_at) == (state, "desktop", "desktop", NOW)
    assert h.wakes == 1


def test_approval_queues_the_same_timed_effects_as_telegram(h):
    w = h.propose()
    h.api.act(w.id, "approve", KEY)
    assert sorted(k for _, k, _ in h.store.claim_due_effects(NOW + 99999, kinds=("start", "end"))) == ["end", "start"]


def test_close_withdraws_a_pending_window_and_closes_an_approved_one(h):
    pending, approved = h.propose(), h.propose(name="b", scope="infra", start=0)
    h.store.decide(approved.id, approve=True, by="telegram:1", now=NOW, via="telegram")
    status, body = h.api.act(pending.id, "close", KEY)
    assert status == 200 and body["window"]["state"] == "cancelled"
    status, body = h.api.act(approved.id, "close", KEY + "b")
    assert status == 200 and body["window"]["state"] == "closed" and body["window"]["closed_at"] == NOW
    assert h.store.get(approved.id).close_reason == "desktop: closed from Desktop"
    assert h.store.get(approved.id).decided_via == "telegram"  # closing does not rewrite who approved
    assert h.store.open_windows("infra", NOW) == []


def test_a_repeated_key_replays_the_first_result_and_acts_once(h, db_path):
    w = h.propose()
    first = h.api.act(w.id, "approve", KEY)
    h.now += 30
    assert h.api.act(w.id, "approve", KEY) == first
    assert h.store.get(w.id).decided_at == NOW
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT user_id, action FROM api_actions").fetchall() == [(desktop.KEY_OWNER, "approve")]


def test_a_key_cannot_be_reused_for_another_action_or_window(h):
    w, other = h.propose(), h.propose(name="b", scope="infra")
    assert h.api.act(w.id, "approve", KEY)[0] == 200
    for window_id, action in ((w.id, "close"), (other.id, "approve")):
        status, body = h.api.act(window_id, action, KEY)
        assert status == 409 and "idempotency key" in body["detail"]
    assert h.store.get(w.id).state == "approved" and h.store.get(other.id).state == "pending"


def test_desktop_keys_do_not_collide_with_an_approvers_mini_app_keys(h):
    a, b = h.propose(), h.propose(name="b", scope="infra")
    assert h.store.api_action(OWNER, KEY, a.id, "approve", NOW)["decided_via"] == "miniapp"
    status, body = h.api.act(b.id, "decline", KEY)
    assert status == 200 and body["window"]["decided_via"] == "desktop"


def test_a_second_decision_with_a_new_key_is_a_conflict(h):
    w = h.propose()
    h.api.act(w.id, "approve", KEY)
    status, body = h.api.act(w.id, "decline", KEY + "b")
    assert status == 409 and "not pending" in body["detail"] and h.store.get(w.id).state == "approved"
    assert h.wakes == 1  # only the action that changed something wakes the scheduler


# -- bad input --------------------------------------------------------------------------------------

@pytest.mark.parametrize("key", [None, "", "short", "x" * 129, "has space 0000000000", 1234567890123456, ["k" * 16],
                                 "ключ-ключ-ключ-ключ", "key-with-newline-0000\n"])
def test_bad_or_missing_key_is_a_400_and_changes_nothing(h, key):
    w = h.propose()
    status, body = h.api.act(w.id, "approve", key)
    assert status == 400 and "key" in body["detail"] and h.store.get(w.id).state == "pending" and h.wakes == 0


@pytest.mark.parametrize("window_id", ["", "w123abc", "W123AB", "W123ABCD", "W123ABG", "../W123ABC", None, 7,
                                       "W123ABC\n", "W123ABC' OR 1=1"])
def test_malformed_window_id_is_a_400(h, window_id):
    assert h.api.act(window_id, "approve", KEY)[0] == 400


def test_unknown_window_is_a_404_and_stores_no_key(h, db_path):
    h.propose()
    status, body = h.api.act("WFFFFFF", "approve", KEY)
    assert status == 404 and body == {"detail": "Window not found."}
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT count(*) FROM api_actions").fetchone() == (0,)


@pytest.mark.parametrize("action", ["open", "approved", "APPROVE", "check_in", "", None, "approve/close"])
def test_unknown_action_is_a_404(h, action):
    w = h.propose()
    assert h.api.act(w.id, action, KEY)[0] == 404 and h.store.get(w.id).state == "pending"


# -- the same decision rules as the Telegram button ---------------------------------------------------

def _expired(h):
    w = h.propose(start=600, end=1200)
    h.now = NOW + 1200  # the end is exclusive: at end_ts the window is over
    return w


def _overlap_exclusive(h):
    first = h.propose()
    h.store.decide(first.id, approve=True, by="telegram:1", now=NOW, via="telegram")
    return h.propose(name="b", start=1200, end=1800)


def _overlap_shared_against_exclusive(h):
    first = h.propose()
    h.store.decide(first.id, approve=True, by="telegram:1", now=NOW, via="telegram")
    return h.propose(name="b", start=1200, end=1800, exclusive=False)


def _already_declined(h):
    w = h.propose()
    h.store.decide(w.id, approve=False, by="telegram:1", now=NOW, via="telegram")
    return w


def _withdrawn(h):
    w = h.propose()
    h.store.close(w.id, by="agent:test", reason="not needed", now=NOW)
    return w


@pytest.mark.parametrize("scenario,fragment", [
    (_expired, "has already ended; ask for a new one"),
    (_overlap_exclusive, "overlaps approved"),
    (_overlap_shared_against_exclusive, "overlaps approved"),
    (_already_declined, "is declined, not pending"),
    (_withdrawn, "is cancelled, not pending"),
])
def test_approve_is_refused_exactly_where_the_telegram_button_refuses(tmp_path, scenario, fragment):
    def fresh(name):
        return Harness(Store(lambda: sqlite3.connect(tmp_path / name, timeout=5)))

    via_desktop, via_telegram = fresh("desktop.db"), fresh("telegram.db")
    d, t = scenario(via_desktop), scenario(via_telegram)
    before = via_desktop.store.get(d.id)
    status, body = via_desktop.api.act(d.id, "approve", KEY)
    reply = via_telegram.actions.on_button(OWNER, f"cw:approve:{t.id}")
    assert status == 409 and reply.alert
    assert fragment in body["detail"]
    # the same message, apart from the window ids
    assert body["detail"].replace(d.id, "W").split(" overlaps approved ")[0] == \
        reply.toast.replace(t.id, "W").split(" overlaps approved ")[0]
    assert via_desktop.store.get(d.id) == before
    assert via_desktop.store.get(d.id).state == via_telegram.store.get(t.id).state
    assert all(e[0] != d.id for e in via_desktop.store.claim_due_effects(NOW + 99999, kinds=("start", "end")))
    assert via_desktop.wakes == 0


def test_two_shared_windows_may_overlap_on_both_paths(h):
    first = h.propose(exclusive=False)
    second = h.propose(name="b", start=1200, end=1800, exclusive=False)
    assert h.api.act(first.id, "approve", KEY)[0] == 200
    assert h.api.act(second.id, "approve", KEY + "b")[0] == 200


def test_a_window_can_be_approved_one_second_before_its_end_but_not_at_it(h):
    w = h.propose(start=0, end=60)
    h.now = NOW + 60
    assert h.api.act(w.id, "approve", KEY)[0] == 409
    h.now = NOW + 59
    assert h.api.act(w.id, "approve", KEY + "b")[0] == 200


def test_an_expired_proposal_can_still_be_declined_or_withdrawn(h):
    a, b = _expired(h), None
    assert h.api.act(a.id, "decline", KEY)[0] == 200
    b = h.propose(scope="infra", start=600, end=1200)
    assert h.api.act(b.id, "close", KEY + "b")[1]["window"]["state"] == "cancelled"


# -- approval.desktop -------------------------------------------------------------------------------

@pytest.mark.parametrize("value", [False, "false", "False", "off", "no", 0, 1, "true", "yes", None, [], {}])
def test_anything_but_true_turns_desktop_approval_off(store, value):
    h = Harness(store, **{"approval.desktop": value})
    w = h.propose()
    assert h.api.windows()[1]["approval"] is False
    for action in ("approve", "decline"):
        status, body = h.api.act(w.id, action, f"{KEY}-{action}")
        assert status == 403 and "approval.desktop" in body["detail"]
    assert h.store.get(w.id).state == "pending" and h.wakes == 0


def test_close_still_works_with_desktop_approval_off(store):
    h = Harness(store, **{"approval.desktop": False})
    approved = h.propose(start=0)
    h.store.decide(approved.id, approve=True, by="telegram:1", now=NOW, via="telegram")
    status, body = h.api.act(approved.id, "close", KEY)
    assert status == 200 and body["window"]["state"] == "closed"


def test_default_and_explicit_true_allow_desktop_approval(store):
    assert approval_enabled(lambda k, d=None: d) is True
    assert approval_enabled(lambda k, d=None: True) is True
    h = Harness(store, **{"approval.desktop": True})
    assert h.api.act(h.propose().id, "approve", KEY)[0] == 200


def test_unreadable_setting_turns_desktop_approval_off():
    def broken(key, default=None):
        raise OSError("config unreadable")
    assert approval_enabled(broken) is False


def test_turning_approval_off_also_stops_a_replay(store):
    h = Harness(store)
    w = h.propose()
    assert h.api.act(w.id, "approve", KEY)[0] == 200
    h.settings["approval.desktop"] = False
    assert h.api.act(w.id, "approve", KEY)[0] == 403


def test_403_does_not_reveal_whether_a_window_exists(store):
    h = Harness(store, **{"approval.desktop": False})
    assert h.api.act("WFFFFFF", "approve", KEY) == h.api.act(h.propose().id, "approve", KEY)


# -- audit ------------------------------------------------------------------------------------------

def test_audit_is_metadata_only_and_lists_decisions_with_their_channel(h):
    a, b, c = h.propose(), h.propose(name="b", scope="infra"), h.propose(name="c", scope="dns")
    h.store.record_audit(now=NOW, verdict="blocked", tool="terminal", rule_ids=["k8s-write"], scopes=["k8s-prod"],
                         preview="kubectl apply --token=SYNTHETIC-PREVIEW", session_id="s1", task_id="t1")
    h.store.decide(a.id, approve=True, by=f"telegram:{OWNER}", now=NOW + 1, via="telegram")
    h.store.api_action(OWNER, KEY, b.id, "decline", NOW + 2)
    h.now = NOW + 3
    h.api.act(c.id, "approve", KEY + "c")
    status, body = h.api.audit()
    assert status == 200
    assert [set(row) for row in body["audit"]] == [
        {"id", "ts", "verdict", "tool", "rule_ids", "scopes", "window_id", "when"}]
    assert "SYNTHETIC-PREVIEW" not in json.dumps(body)
    assert [(d["window_id"], d["state"], d["decided_via"], d["decided_by"]) for d in body["decisions"]] == [
        (c.id, "approved", "desktop", "desktop"),
        (b.id, "declined", "miniapp", f"telegram:{OWNER}"),
        (a.id, "approved", "telegram", f"telegram:{OWNER}"),
    ]


def test_a_failing_wake_does_not_fail_a_committed_action(store):
    h = Harness(store)
    h.api.wake = lambda: 1 / 0
    w = h.propose()
    assert h.api.act(w.id, "approve", KEY)[0] == 200 and h.store.get(w.id).state == "approved"


def test_audit_moments_are_written_in_the_plugins_time_zone_like_the_spans(h):
    """Found in the real app: the page showed these in the app's own zone, next to spans in the plugin's."""
    from datetime import datetime, timedelta
    zone = timezone(timedelta(hours=2), "CEST")
    h.actions.tz = lambda: zone
    w = h.propose()
    h.store.record_audit(now=NOW, verdict="blocked", tool="terminal", rule_ids=["k8s-write"], scopes=["k8s-prod"],
                         preview="", session_id="s1", task_id="t1")
    h.now = NOW + 60
    h.api.act(w.id, "approve", KEY)
    _, body = h.api.audit()
    assert body["audit"][0]["when"] == datetime.fromtimestamp(NOW, zone).strftime("%a %d %b %H:%M CEST")
    assert body["decisions"][0]["when"] == datetime.fromtimestamp(NOW + 60, zone).strftime("%a %d %b %H:%M CEST")
    assert body["decisions"][0]["when"].endswith(" CEST") and h.actions.span(w).endswith(" CEST")
    h.actions.tz = lambda: None  # no zone configured: UTC, as the spans do
    assert h.api.audit()[1]["audit"][0]["when"].endswith(" UTC")
