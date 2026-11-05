import pytest

from change_windows.store import Store, StoreError

H = 3600


TIMED = ("remind", "start", "end")  # the announce effect of a proposal is tested on its own


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


def test_effects_claimed_once_across_store_instances(store, db_path):
    import sqlite3

    w, _ = mk(store, 1000, 2000)
    store.ensure_effects(w, reminder_s=600)
    store.ensure_effects(w, reminder_s=600)  # idempotent
    other = Store(lambda: sqlite3.connect(db_path, timeout=5))
    first = store.claim_due_effects(now=1000, kinds=TIMED)
    second = other.claim_due_effects(now=1000, kinds=TIMED)
    assert sorted(k for _, k, _ in first) == ["remind", "start"]
    assert second == []
    for wid, kind, _ in first:
        store.finish_effect(wid, kind, "ok", now=1001)
    assert [k for _, k, _ in other.claim_due_effects(now=2000, kinds=TIMED)] == ["end"]


def test_audit_preview_is_capped(store):
    store.record_audit(now=1, verdict="blocked", tool="terminal", rule_ids=["k8s-write"], scopes=["k8s-prod"],
                       preview="x" * 1000)
    row = store.audit_tail(1)[0]
    assert row["verdict"] == "blocked" and len(row["preview"]) == 400



def test_concurrent_approvals_of_overlapping_exclusive_windows(db_path):
    import sqlite3
    import threading

    def connect():
        return sqlite3.connect(db_path, timeout=10)

    s0 = Store(connect)
    a, _ = s0.propose(name="a", scope="k8s-prod", start_ts=0, end_ts=2 * H, reason="r", proposed_by="x", now=0)
    b, _ = s0.propose(name="b", scope="k8s-prod", start_ts=H, end_ts=3 * H, reason="r", proposed_by="x", now=0)
    barrier = threading.Barrier(2)
    results = {}

    def approve(wid):
        st = Store(connect)
        barrier.wait()
        try:
            results[wid] = st.decide(wid, approve=True, by="owner:1", now=5).state
        except StoreError as exc:
            results[wid] = f"refused: {exc}"

    threads = [threading.Thread(target=approve, args=(w.id,)) for w in (a, b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(v == "approved" for v in results.values()) == [False, True]


def test_close_after_concurrent_approval_still_removes_permission(store):
    w, _ = mk(store, 0, H)
    store.decide(w.id, approve=True, by="owner:1", now=1)  # approved between the agent's read and close
    closed = store.close(w.id, by="agent:test", reason="done", now=10)
    assert closed.state == "closed" and not closed.is_open(11)


def test_effect_lease_survives_a_crash_and_is_bounded(store):
    w, _ = mk(store, 1000, 2000)
    store.ensure_effects(w, reminder_s=600)
    first = store.claim_due_effects(now=1000, lease_s=300, kinds=TIMED)
    assert sorted(k for _, k, _ in first) == ["remind", "start"]
    assert store.claim_due_effects(now=1100, lease_s=300, kinds=TIMED) == []          # lease still held
    store.finish_effect(w.id, "remind", "sent", now=1100)
    again = store.claim_due_effects(now=1400, lease_s=300, kinds=TIMED)               # crashed "start" is retried
    assert [k for _, k, _ in again] == ["start"]
    store.claim_due_effects(now=1800, lease_s=300, kinds=TIMED)                       # attempt 3
    assert store.claim_due_effects(now=2600, lease_s=300, kinds=TIMED) == [(w.id, "end", 2000)]
    assert store.failed_effects(1800, lease_s=300) == []                  # last lease still live
    assert (w.id, "start", 3) in store.failed_effects(2600, lease_s=300)



def test_v1_database_is_migrated_in_place(db_path):
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE effects (window_id TEXT NOT NULL, kind TEXT NOT NULL, due_ts INTEGER NOT NULL,
                              done_ts INTEGER, result TEXT, PRIMARY KEY (window_id, kind));
        INSERT INTO effects VALUES ('WOLD', 'start', 10, NULL, NULL);
    """)
    conn.commit()
    conn.close()
    s = Store(lambda: sqlite3.connect(db_path, timeout=5))
    assert s.claim_due_effects(now=100) == [("WOLD", "start", 10)]



def test_concurrent_v1_migration_is_serialized(db_path):
    import sqlite3
    import threading

    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE effects (window_id TEXT NOT NULL, kind TEXT NOT NULL, due_ts INTEGER NOT NULL,
                              done_ts INTEGER, result TEXT, PRIMARY KEY (window_id, kind));
    """)
    conn.commit()
    conn.close()
    errors = []
    barrier = threading.Barrier(4)

    def open_store():
        barrier.wait()
        try:
            Store(lambda: sqlite3.connect(db_path, timeout=10)).audit_tail(1)
        except Exception as exc:  # report, do not hide
            errors.append(repr(exc))

    threads = [threading.Thread(target=open_store) for _ in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert errors == []
    cols = {r[1] for r in sqlite3.connect(db_path).execute("PRAGMA table_info(effects)")}
    assert {"claimed_ts", "attempts"} <= cols


def test_purge_previews(store):
    store.record_audit(now=1, verdict="blocked", tool="terminal", rule_ids=["k8s-write"], scopes=["k8s-prod"],
                       preview="old preview text")
    store.record_audit(now=5, verdict="blocked", tool="terminal", rule_ids=["k8s-write"], scopes=["k8s-prod"],
                       preview="newer preview")
    assert store.purge_previews(before_ts=3) == 1
    assert [r["preview"] for r in store.audit_tail(2)] == ["newer preview", "[purged]"]
    assert store.purge_previews() == 1


def test_a_proposal_queues_one_announce_effect(store):
    w, _ = mk(store, 1000, 2000)
    assert store.claim_due_effects(now=0, kinds=("announce",)) == [(w.id, "announce", 0)]
    assert store.claim_due_effects(now=10, kinds=("announce",)) == []  # leased
    store.finish_effect(w.id, "announce", "sent", now=11)
    assert store.claim_due_effects(now=1000, kinds=("announce",)) == []
    # an approved-at-creation window (emergency path) is not announced as a proposal
    store.propose(name="e", scope="infra", start_ts=0, end_ts=600, reason="r", proposed_by="owner", now=0,
                  state="approved", decided_by="telegram:1")
    assert store.claim_due_effects(now=5000, kinds=("announce",)) == []
