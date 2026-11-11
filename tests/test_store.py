"""The plan store: slots, the release lease, labels and the change log."""
from chronomancer.store import FAILED, PLANNED, RELEASED


def put(store, task_id="t_1", start=1000, est=600, **kw):
    return store.put_slot("", task_id, start_ts=start, estimate_s=est, state=kw.pop("state", PLANNED),
                          parked=kw.pop("parked", False), set_by=kw.pop("by", "owner"), now=kw.pop("now", 900), **kw)


def test_a_slot_is_created_then_replaced_and_keeps_its_pin(store):
    slot = put(store)
    assert (slot.start_ts, slot.end_ts, slot.state, slot.pinned, slot.set_by) == (1000, 1600, PLANNED, False, "owner")
    store.set_pinned("", "t_1", True)
    moved = put(store, start=5000, est=1200, by="agent:assistant")
    assert (moved.start_ts, moved.estimate_s, moved.pinned, moved.set_by) == (5000, 1200, True, "agent:assistant")
    assert [s.task_id for s in store.slots("")] == ["t_1"] and store.slots("other") == []
    assert store.delete_slot("", "t_1") and store.get_slot("", "t_1") is None


def test_a_due_slot_is_leased_to_one_caller(store):
    put(store, "t_1", start=1000)
    put(store, "t_2", start=2000)
    first = store.claim_due(1500, lease_s=60, max_attempts=3)
    assert [(s.task_id, s.attempts) for s in first] == [("t_1", 1)]
    assert store.claim_due(1510, lease_s=60, max_attempts=3) == []          # still leased
    again = store.claim_due(1561, lease_s=60, max_attempts=3)               # the lease ran out
    assert [(s.task_id, s.attempts) for s in again] == [("t_1", 2)]


def test_a_slot_stops_being_claimed_after_its_last_attempt(store):
    put(store, start=1000)
    for now in (1000, 1100, 1200):
        assert len(store.claim_due(now, lease_s=60, max_attempts=3)) == 1
    assert store.claim_due(1300, lease_s=60, max_attempts=3) == []
    assert [s.task_id for s in store.exhausted(1300, lease_s=60, max_attempts=3)] == ["t_1"]
    assert store.next_due_ts(max_attempts=3) is None


def test_finish_closes_the_claimed_slot_once(store):
    put(store, start=1000)
    (slot,) = store.claim_due(1000, lease_s=60, max_attempts=3)
    assert store.finish(slot, RELEASED, "released at its slot: ready", 1001) is True
    done = store.get_slot("", "t_1")
    assert (done.state, done.released_at, done.note) == (RELEASED, 1001, "released at its slot: ready")
    assert store.finish(slot, FAILED, "late", 1002) is False
    assert store.get_slot("", "t_1").state == RELEASED


def test_finish_drops_its_result_when_the_slot_was_moved_meanwhile(store):
    put(store, start=1000)
    (slot,) = store.claim_due(1000, lease_s=60, max_attempts=3)
    put(store, start=9000)                                       # the owner moved it during the release
    assert store.finish(slot, RELEASED, "released", 1001) is False
    moved = store.get_slot("", "t_1")
    assert (moved.state, moved.start_ts, moved.attempts) == (PLANNED, 9000, 0)


def test_next_due_and_recent_slots(store):
    assert store.next_due_ts(max_attempts=3) is None
    put(store, "t_1", start=5000)
    put(store, "t_2", start=3000)
    put(store, "t_old", start=100, state=RELEASED)
    assert store.next_due_ts(max_attempts=3) == 3000
    assert [s.task_id for s in store.slots("", since_ts=1000)] == ["t_2", "t_1"]   # the old finished slot is left out
    assert store.prune_slots(1000) == 1 and len(store.slots("")) == 2


def test_labels_and_card_labels(store):
    label = store.put_label("cluster:prod", exclusive=True, description="the production cluster", by="agent:assistant", now=10)
    assert (label.exclusive, label.description, label.created_by) == (True, "the production cluster", "agent:assistant")
    kept = store.put_label("cluster:prod", exclusive=None, description=None, by="owner", now=20)
    assert (kept.exclusive, kept.description, kept.created_by) == (True, "the production cluster", "agent:assistant")
    store.put_label("repo:web", exclusive=False, description=None, by="owner", now=20)
    assert store.change_card_labels("", "t_1", add=["repo:web", "cluster:prod"]) == ["cluster:prod", "repo:web"]
    assert store.change_card_labels("", "t_1", add=["repo:web"], remove=["cluster:prod"]) == ["repo:web"]
    store.change_card_labels("", "t_2", add=["repo:web"])
    assert store.card_labels("") == {"t_1": ["repo:web"], "t_2": ["repo:web"]}
    assert store.delete_label("repo:web") and store.card_labels("") == {}
    assert [l.name for l in store.labels()] == ["cluster:prod"]


def test_the_log_keeps_order_and_detail(store):
    first = store.add_log(ts=10, actor="owner", action="plan", task_id="t_1", detail={"title": "A", "start": 100})
    second = store.add_log(ts=20, actor="agent:assistant", action="move", task_id="t_1", detail={"from": 100, "start": 200})
    assert [e.id for e in store.log_tail(10)] == [second, first]
    assert [(e.actor, e.detail["start"]) for e in store.log_after(first)] == [("agent:assistant", 200)]
    assert store.last_log_id() == second
    assert store.prune_log(15) == 1 and [e.id for e in store.log_tail(10)] == [second]


def test_meta(store):
    assert store.get_meta("seen:owner", "0") == "0"
    store.set_meta("seen:owner", "7")
    store.set_meta("seen:owner", "9")
    assert store.get_meta("seen:owner") == "9"
