"""Every owner decision records the channel it came through: telegram, miniapp or desktop."""
import sqlite3
from datetime import timezone

import pytest

from change_windows.store import CHANNELS, Store, StoreError
from change_windows.tg import Actions, decided_line

NOW = 1_800_000_000
OWNER = 111111111
KEY = "channel-key-00000001"


def propose(store, start=NOW + 600, end=NOW + 4200, **extra):
    values = dict(name="web v2", scope="k8s-prod", start_ts=start, end_ts=end, reason="roll out",
                  proposed_by="agent:test", now=NOW)
    values.update(extra)
    return store.propose(**values)[0]


def actions(store):
    settings = {"approver_telegram_ids": [OWNER]}
    return Actions(store, setting=lambda k, d=None: settings.get(k, d), tz=lambda: timezone.utc, clock=lambda: NOW)


@pytest.mark.parametrize("verb,state", [("approve", "approved"), ("decline", "declined")])
def test_telegram_button_records_the_telegram_channel(store, verb, state):
    w = propose(store)
    reply = actions(store).on_button(OWNER, f"cw:{verb}:{w.id}")
    saved = store.get(w.id)
    assert (saved.state, saved.decided_via, saved.decided_by) == (state, "telegram", f"telegram:{OWNER}")
    assert f"Decided by telegram:{OWNER} in Telegram" in reply.text


def test_mini_app_action_records_the_miniapp_channel(store):
    w = propose(store)
    result = store.api_action(OWNER, KEY, w.id, "approve", NOW)
    assert result["decided_via"] == "miniapp" and result["decided_by"] == f"telegram:{OWNER}"
    text, _ = actions(store).window_message(store.get(w.id), NOW)
    assert f"Decided by telegram:{OWNER} in the Mini App" in text


def test_desktop_action_records_the_desktop_channel_and_actor(store):
    w = propose(store)
    result = store.api_action(0, KEY, w.id, "decline", NOW, channel="desktop", by="desktop")
    assert (result["state"], result["decided_via"], result["decided_by"]) == ("declined", "desktop", "desktop")
    text, _ = actions(store).window_message(store.get(w.id), NOW)
    assert "Decided by desktop in Hermes Desktop" in text


def test_close_reason_names_the_channel(store):
    for channel, phrase in (("miniapp", "telegram:7: closed from Mini App"), ("desktop", "desktop: closed from Desktop")):
        w = propose(store, scope=f"scope-{channel}")
        by = "desktop" if channel == "desktop" else None
        result = store.api_action(0 if by else 7, f"{KEY}-{channel}", w.id, "close", NOW, channel=channel, by=by)
        assert result["state"] == "cancelled" and result["close_reason"] == phrase
        assert result["decided_via"] is None  # a withdrawal is not an approve/decline decision


def test_unknown_channel_is_refused_and_changes_nothing(store):
    w = propose(store)
    with pytest.raises(StoreError):
        store.api_action(0, KEY, w.id, "approve", NOW, channel="agent", by="agent:x")
    with pytest.raises(StoreError):
        store.decide(w.id, approve=True, by="x", now=NOW, via="tool")
    assert store.get(w.id).state == "pending" and CHANNELS == ("telegram", "miniapp", "desktop")


def test_recent_decisions_are_newest_first_and_skip_undecided(store):
    a, b, c = (propose(store, scope=f"s{i}") for i in range(3))
    store.decide(a.id, approve=True, by="telegram:1", now=NOW + 1, via="telegram")
    store.decide(b.id, approve=False, by="desktop", now=NOW + 2, via="desktop")
    assert [(w.id, w.decided_via) for w in store.recent_decisions()] == [(b.id, "desktop"), (a.id, "telegram")]
    assert [w.id for w in store.recent_decisions(limit=1)] == [b.id] and c.id


def test_v3_database_gains_the_channel_column_and_keeps_old_decisions(store, db_path):
    w = propose(store)
    store.decide(w.id, approve=True, by="telegram:1", now=NOW)
    with sqlite3.connect(db_path) as conn:
        conn.execute("ALTER TABLE windows DROP COLUMN decided_via")
        conn.execute("UPDATE meta SET v='3' WHERE k='schema_version'")
    upgraded = Store(lambda: sqlite3.connect(db_path))
    old = upgraded.get(w.id)
    assert old.state == "approved" and old.decided_via is None
    assert decided_line(old) == "Decided by telegram:1"  # no channel is invented for old rows
    fresh = propose(upgraded, scope="other")
    assert upgraded.decide(fresh.id, approve=False, by="desktop", now=NOW, via="desktop").decided_via == "desktop"
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone() == ("4",)
