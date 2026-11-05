import pytest

from change_windows.store import Store, StoreError

H = 3600


def mk(store, start, end, scope="k8s-prod", exclusive=True, presence=False):
    w, conflicts = store.propose(name="w", scope=scope, start_ts=start, end_ts=end, reason="r",
                                 proposed_by="agent:test", now=0, exclusive=exclusive, presence=presence)
    return w, conflicts


def test_half_open_and_approval(store):
    w, _ = mk(store, 1000, 1000 + H)
    assert w.state == "pending" and not w.is_open(1500)
    w = store.decide(w.id, approve=True, by="owner:1", now=10)
    assert w.is_open(1000) and w.is_open(1000 + H - 1) and not w.is_open(1000 + H)
    assert [x.id for x in store.open_windows("k8s-prod", 1500)] == [w.id]
    assert store.open_windows("infra", 1500) == []


def test_conflicts_exclusive_vs_shared(store):
    a, _ = mk(store, 0, 2 * H)
    _, c = mk(store, H, 3 * H)
    assert [x.id for x in c] == [a.id]
    s1, _ = mk(store, 10 * H, 11 * H, exclusive=False)
    _, c2 = mk(store, 10 * H, 11 * H, exclusive=False)
    assert c2 == []  # two shared windows may overlap
    _, c3 = mk(store, 2 * H, 3 * H, scope="infra")
    assert c3 == []  # other scope


def test_approve_refuses_overlap_with_approved(store):
    a, _ = mk(store, 0, 2 * H)
    store.decide(a.id, approve=True, by="owner:1", now=1)
    b, _ = mk(store, H, 3 * H)
    with pytest.raises(StoreError):
        store.decide(b.id, approve=True, by="owner:1", now=2)
    assert store.decide(b.id, approve=False, by="owner:1", now=3).state == "declined"


def test_presence_needs_check_in(store):
    w, _ = mk(store, 0, H, presence=True)
    w = store.decide(w.id, approve=True, by="owner:1", now=0)
    assert not w.is_open(10)
    assert store.check_in(w.id, by="owner:1", now=11).is_open(12)


def test_close_removes_permission(store):
    w, _ = mk(store, 0, H)
    store.decide(w.id, approve=True, by="owner:1", now=0)
    w = store.close(w.id, by="agent:test", reason="done", now=100)
    assert w.state == "closed" and not w.is_open(200)
    with pytest.raises(StoreError):
        store.close(w.id, by="agent:test", reason="again", now=101)


def test_effects_claimed_exactly_once_across_store_instances(store, db_path):
    import sqlite3

    w, _ = mk(store, 1000, 2000)
    store.ensure_effects(w, reminder_s=600)
    store.ensure_effects(w, reminder_s=600)  # idempotent
    other = Store(lambda: sqlite3.connect(db_path, timeout=5))
    first = store.claim_due_effects(now=1000)
    second = other.claim_due_effects(now=1000)
    assert sorted(k for _, k, _ in first) == ["remind", "start"]
    assert second == []
    assert [k for _, k, _ in other.claim_due_effects(now=2000)] == ["end"]


def test_audit_preview_is_capped(store):
    store.record_audit(now=1, verdict="blocked", tool="terminal", rule_ids=["k8s-write"], scopes=["k8s-prod"],
                       preview="x" * 1000)
    row = store.audit_tail(1)[0]
    assert row["verdict"] == "blocked" and len(row["preview"]) == 400
