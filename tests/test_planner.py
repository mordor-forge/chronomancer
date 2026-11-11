"""The plan: giving cards slots, moving them, running them now, and releasing them at their start."""
import pytest

from chronomancer.clashes import BUSY, CLASH
from chronomancer.planner import (DONE, DUE, FAILED_, GONE, MAX_ATTEMPTS, MISSED_, OWNER, PLANNED_, RUNNING,
                                  UNPLANNED, WAITING, PlanError)
from chronomancer.store import FAILED, MISSED, PLANNED, RELEASED
from conftest import HOUR, NOW, at

AGENT = "agent:assistant"


def refusal(call):
    with pytest.raises(PlanError) as caught:
        call()
    return str(caught.value)


# ---- giving a card a slot ----

@pytest.mark.parametrize("status", ["ready", "todo"])
def test_a_slot_parks_a_card_that_would_start_by_itself(planner, kanban, store, status):
    kanban.add("t_1", "Upgrade ingress", status=status)
    change = planner.set_slot("t_1", at(14), estimate_s=2 * HOUR, actor=AGENT)
    assert kanban.status("t_1") == "scheduled"
    assert kanban.tasks["t_1"]["reason"] == "SCHEDULED_UNTIL=2026-11-11T14:00+01:00 (chronomancer slot)"
    slot = store.get_slot("", "t_1")
    assert (slot.start_ts, slot.estimate_s, slot.state, slot.parked, slot.set_by) == (at(14), 2 * HOUR, PLANNED, True, AGENT)
    assert (change.action, change.parked, change.clashes) == ("plan", True, [])
    (entry,) = store.log_tail()
    assert (entry.actor, entry.action, entry.detail["start"], entry.detail["title"]) == (AGENT, "plan", at(14), "Upgrade ingress")


@pytest.mark.parametrize("status", ["blocked", "scheduled"])
def test_a_slot_leaves_a_card_that_already_waits(planner, kanban, store, status):
    kanban.add("t_1", status=status)
    change = planner.set_slot("t_1", at(14), actor=OWNER)
    assert kanban.status("t_1") == status and kanban.count("kanban_schedule") == 0
    assert (store.get_slot("", "t_1").parked, change.parked) == (False, False)
    assert store.get_slot("", "t_1").estimate_s == 60 * 60          # the default estimate


def test_the_default_estimate_comes_from_the_settings(planner, kanban, store, config):
    config["default_estimate_minutes"] = 25
    kanban.add("t_1", status="blocked")
    planner.set_slot("t_1", at(14), actor=OWNER)
    assert store.get_slot("", "t_1").estimate_s == 25 * 60


@pytest.mark.parametrize("status,word", [("running", "is running now"), ("review", "is in review"), ("done", "is done"),
                                         ("archived", "is archived"), ("triage", "is in triage")])
def test_cards_that_cannot_start_later_are_refused(planner, kanban, store, status, word):
    kanban.add("t_1", "Upgrade ingress", status=status)
    assert word in refusal(lambda: planner.set_slot("t_1", at(14), actor=OWNER))
    assert word in refusal(lambda: planner.run_now("t_1", actor=OWNER))
    assert store.get_slot("", "t_1") is None and kanban.status("t_1") == status


def test_bad_requests_are_refused_and_change_nothing(planner, kanban, store):
    kanban.add("t_1")
    assert "not on the board" in refusal(lambda: planner.set_slot("t_9", at(14), actor=OWNER))
    assert "in the past" in refusal(lambda: planner.set_slot("t_1", NOW - 600, actor=OWNER))
    assert "between 1 minute and 14 days" in refusal(lambda: planner.set_slot("t_1", at(14), estimate_s=5, actor=OWNER))
    assert "A card id looks like" in refusal(lambda: planner.set_slot("no such id!", at(14), actor=OWNER))
    assert store.slots("") == [] and kanban.status("t_1") == "ready"


def test_a_refusal_by_the_kanban_refuses_the_move(planner, kanban, store):
    kanban.add("t_1")
    kanban.refuse.add("kanban_schedule")
    assert "The Kanban did not accept the change" in refusal(lambda: planner.set_slot("t_1", at(14), actor=OWNER))
    assert store.get_slot("", "t_1") is None


def test_an_unreachable_board_refuses_the_move(planner, kanban, store):
    kanban.add("t_1")
    kanban.down = True
    assert "cannot be reached" in refusal(lambda: planner.set_slot("t_1", at(14), actor=OWNER))
    assert store.get_slot("", "t_1") is None


# ---- moving ----

def test_moving_a_slot_keeps_its_estimate_and_records_where_it_was(planner, kanban, store):
    kanban.add("t_1")
    planner.set_slot("t_1", at(14), estimate_s=90 * 60, actor=AGENT)
    change = planner.set_slot("t_1", at(16), actor=OWNER)
    slot = store.get_slot("", "t_1")
    assert (slot.start_ts, slot.estimate_s, slot.set_by, slot.parked) == (at(16), 90 * 60, OWNER, True)
    assert (change.action, change.previous.start_ts) == ("move", at(14))
    assert kanban.count("kanban_schedule") == 1                      # the card was parked once
    entry = store.log_tail()[0]
    assert (entry.actor, entry.action, entry.detail["from"], entry.detail["start"]) == (OWNER, "move", at(14), at(16))


def test_a_released_card_that_gets_a_new_slot_is_parked_again(planner, kanban, store, clock):
    kanban.add("t_1", status="scheduled")
    planner.set_slot("t_1", at(11), actor=OWNER)
    clock.now = at(11)
    planner.run_due()
    assert kanban.status("t_1") == "ready"
    planner.set_slot("t_1", at(15), actor=OWNER)                   # not picked up yet: plan it again
    assert kanban.status("t_1") == "scheduled"
    slot = store.get_slot("", "t_1")
    assert (slot.state, slot.start_ts, slot.released_at) == (PLANNED, at(15), None)


def test_a_dry_run_reports_and_changes_nothing(planner, kanban, store):
    kanban.add("t_1")
    change = planner.set_slot("t_1", at(14), actor=AGENT, dry_run=True)
    assert (change.action, change.slot.start_ts) == ("preview", at(14))
    assert store.slots("") == [] and store.log_tail() == [] and kanban.status("t_1") == "ready"


# ---- pins ----

def test_only_the_owner_pins_and_an_agent_cannot_touch_a_pinned_slot(planner, kanban, store):
    kanban.add("t_1", status="blocked")
    planner.set_slot("t_1", at(14), actor=AGENT)
    assert "Only the owner" in refusal(lambda: planner.pin("t_1", True, actor=AGENT))
    planner.pin("t_1", True, actor=OWNER)
    for move in (lambda: planner.set_slot("t_1", at(16), actor=AGENT), lambda: planner.run_now("t_1", actor=AGENT),
                 lambda: planner.clear("t_1", actor=AGENT)):
        assert "The owner pinned this slot at Wed 11 Nov 14:00" in refusal(move)
    assert store.get_slot("", "t_1").start_ts == at(14) and kanban.status("t_1") == "blocked"
    planner.set_slot("t_1", at(16), actor=OWNER)                   # the owner moves it; the pin stays
    assert (store.get_slot("", "t_1").start_ts, store.get_slot("", "t_1").pinned) == (at(16), True)
    planner.pin("t_1", False, actor=OWNER)
    planner.set_slot("t_1", at(17), actor=AGENT)
    assert [e.action for e in store.log_tail()][:4] == ["move", "unpin", "move", "pin"]


# ---- run now ----

def test_run_now_releases_a_waiting_card_and_moves_its_slot_to_now(planner, kanban, store):
    kanban.add("t_1", "Upgrade ingress", status="blocked")
    planner.set_slot("t_1", at(16), estimate_s=30 * 60, actor=AGENT)
    change = planner.run_now("t_1", actor=OWNER)
    assert kanban.status("t_1") == "ready" and change.released == "ready"
    slot = store.get_slot("", "t_1")
    assert (slot.start_ts, slot.estimate_s, slot.state, slot.released_at) == (NOW, 30 * 60, RELEASED, NOW)
    assert kanban.comments["t_1"] == ["chronomancer: released now by the owner."]
    assert store.log_tail()[0].detail["from"] == at(16)


def test_run_now_on_a_card_without_a_slot_gives_it_one(planner, kanban, store):
    kanban.add("t_1", status="scheduled")
    planner.run_now("t_1", actor=AGENT)
    slot = store.get_slot("", "t_1")
    assert (slot.start_ts, slot.state, slot.estimate_s) == (NOW, RELEASED, 3600)
    assert kanban.comments["t_1"] == ["chronomancer: released now by profile assistant."]


def test_run_now_says_when_the_card_still_waits_for_its_parents(planner, kanban):
    kanban.add("t_parent", status="running")
    kanban.add("t_1", status="scheduled", parents=["t_parent"])
    change = planner.run_now("t_1", actor=OWNER)
    assert kanban.status("t_1") == "todo"
    assert change.note == "released now: todo, it waits for its parent cards"


def test_run_now_on_a_ready_card_has_nothing_to_release(planner, kanban, store):
    kanban.add("t_1", status="ready")
    change = planner.run_now("t_1", actor=OWNER)
    assert (change.released, change.note) == ("", "the card was already ready")
    assert kanban.count("kanban_unblock") == 0 and kanban.comments["t_1"] == []
    assert store.get_slot("", "t_1").state == RELEASED


def test_a_slot_that_starts_now_runs_now(planner, kanban, store):
    kanban.add("t_1", status="blocked")
    change = planner.set_slot("t_1", NOW - 30, actor=OWNER)
    assert change.action == "now" and kanban.status("t_1") == "ready"
    assert store.get_slot("", "t_1").state == RELEASED


def test_no_comment_when_the_setting_is_off(planner, kanban, config):
    config["comment_on_release"] = False
    kanban.add("t_1", status="blocked")
    planner.run_now("t_1", actor=OWNER)
    assert kanban.status("t_1") == "ready" and kanban.comments["t_1"] == []


# ---- clear ----

def test_clear_takes_the_card_out_of_the_plan_and_leaves_the_card(planner, kanban, store):
    kanban.add("t_1")
    planner.set_slot("t_1", at(14), actor=AGENT)
    change = planner.clear("t_1", actor=OWNER)
    assert store.get_slot("", "t_1") is None and kanban.status("t_1") == "scheduled"
    assert change.note == "The card stays scheduled. It runs when it gets a new slot or when it is run now."
    assert "has no slot" in refusal(lambda: planner.clear("t_1", actor=OWNER))


# ---- the timer ----

def test_the_timer_releases_a_card_when_its_slot_starts(planner, kanban, store, clock):
    kanban.add("t_1", "Upgrade ingress")
    planner.set_slot("t_1", at(14), actor=AGENT)
    clock.now = at(14) - 1
    assert planner.run_due() == [] and kanban.status("t_1") == "scheduled"
    clock.now = at(14)
    (change,) = planner.run_due()
    assert kanban.status("t_1") == "ready" and change.released == "ready"
    slot = store.get_slot("", "t_1")
    assert (slot.state, slot.released_at, slot.note) == (RELEASED, at(14), "released at its slot: ready")
    assert kanban.comments["t_1"] == ["chronomancer: released at its slot (planned start 2026-11-11T14:00+01:00)."]
    entry = store.log_tail()[0]
    assert (entry.actor, entry.action, entry.detail["title"]) == ("timer", "release", "Upgrade ingress")
    assert planner.run_due() == [] and kanban.count("kanban_unblock") == 1          # once


def test_the_timer_does_not_release_twice_after_a_crash_between_release_and_record(planner, kanban, store, clock):
    kanban.add("t_1", status="scheduled")
    planner.set_slot("t_1", at(14), actor=AGENT)
    clock.now = at(14)
    (slot,) = store.claim_due(clock.now, lease_s=60, max_attempts=MAX_ATTEMPTS)     # a pass that released ...
    kanban("kanban_unblock", {"task_id": "t_1"})                                    # ... and died before it recorded
    clock.advance(61)
    (change,) = planner.run_due()
    assert change.note == "the card was already ready" and kanban.count("kanban_unblock") == 1
    assert store.get_slot("", "t_1").state == RELEASED


@pytest.mark.parametrize("status", ["ready", "running", "done"])
def test_a_card_that_already_moved_on_is_recorded_not_released(planner, kanban, store, clock, status):
    kanban.add("t_1", status="blocked")
    planner.set_slot("t_1", at(14), actor=AGENT)
    kanban.set("t_1", status=status)                                 # someone started it outside the plan
    clock.now = at(14)
    planner.run_due()
    slot = store.get_slot("", "t_1")
    assert (slot.state, slot.note) == (RELEASED, f"the card was already {status}")
    assert kanban.count("kanban_unblock") == 0 and kanban.comments["t_1"] == []


def test_a_slot_is_released_late_inside_its_grace_and_missed_after_it(planner, kanban, store, clock):
    kanban.add("t_late", status="scheduled")
    kanban.add("t_missed", status="scheduled")
    planner.set_slot("t_late", at(14), estimate_s=2 * HOUR, actor=AGENT)
    planner.set_slot("t_missed", at(14), estimate_s=10 * 60, actor=AGENT)
    clock.now = at(15)                                               # the gateway was down for an hour
    planner.run_due()
    assert kanban.status("t_late") == "ready" and kanban.status("t_missed") == "scheduled"
    missed = store.get_slot("", "t_missed")
    assert (missed.state, missed.note) == (MISSED, "missed: the timer was not running at Wed 11 Nov 14:00")
    assert store.log_tail()[0].action in ("missed", "release")
    assert {e.action for e in store.log_tail(2)} == {"missed", "release"}


def test_a_short_slot_still_has_fifteen_minutes_of_grace(planner, kanban, store, clock):
    kanban.add("t_1", status="scheduled")
    planner.set_slot("t_1", at(14), estimate_s=60, actor=AGENT)
    clock.now = at(14, 14)
    planner.run_due()
    assert store.get_slot("", "t_1").state == RELEASED


def test_an_unreachable_board_is_retried_and_then_given_up(planner, kanban, store, clock):
    kanban.add("t_1", status="scheduled")
    planner.set_slot("t_1", at(14), estimate_s=4 * HOUR, actor=AGENT)
    kanban.down = True
    clock.now = at(14)
    for _ in range(MAX_ATTEMPTS):
        assert planner.run_due() == []
        assert store.get_slot("", "t_1").state == PLANNED
        clock.advance(61)
    planner.run_due()                                                # no attempt left: the slot fails
    slot = store.get_slot("", "t_1")
    assert slot.state == FAILED and slot.note.startswith(f"not released after {MAX_ATTEMPTS} attempts: ")
    assert store.log_tail()[0].action == "failed"
    kanban.down = False
    assert planner.run_due() == [] and kanban.status("t_1") == "scheduled"


def test_the_board_coming_back_releases_on_the_next_attempt(planner, kanban, store, clock):
    kanban.add("t_1", status="scheduled")
    planner.set_slot("t_1", at(14), estimate_s=4 * HOUR, actor=AGENT)
    kanban.down = True
    clock.now = at(14)
    planner.run_due()
    assert "kanban_show failed" in store.get_slot("", "t_1").note
    kanban.down = False
    clock.advance(61)
    planner.run_due()
    assert kanban.status("t_1") == "ready" and store.get_slot("", "t_1").state == RELEASED


def test_a_card_that_left_the_board_or_fell_to_triage_fails_with_a_reason(planner, kanban, store, clock):
    kanban.add("t_gone", status="scheduled")
    kanban.add("t_triage", status="blocked")
    planner.set_slot("t_gone", at(14), actor=AGENT)
    planner.set_slot("t_triage", at(14), actor=AGENT)
    del kanban.tasks["t_gone"]
    kanban.set("t_triage", status="triage")
    clock.now = at(14)
    planner.run_due()
    assert store.get_slot("", "t_gone").note == "the card is not on the board"
    assert store.get_slot("", "t_triage").note == "the card is in triage and cannot be released"
    assert {s.state for s in store.slots("")} == {FAILED}


def test_a_slot_moved_during_its_release_keeps_the_new_plan(planner, kanban, store, clock):
    kanban.add("t_1", status="scheduled")
    planner.set_slot("t_1", at(14), actor=AGENT)
    clock.now = at(14)
    real_card = planner.board.card

    def card_then_move(board, task_id, **kw):                        # the owner moves the slot mid-release
        found = real_card(board, task_id, **kw)
        store.put_slot("", "t_1", start_ts=at(18), estimate_s=3600, state=PLANNED, parked=False, set_by=OWNER, now=clock.now)
        return found

    planner.board.card = card_then_move
    assert planner.run_due() == []
    slot = store.get_slot("", "t_1")
    assert (slot.state, slot.start_ts) == (PLANNED, at(18))
    assert [e.action for e in store.log_tail()] == ["plan"]         # no release was recorded


def test_next_due_is_the_earliest_planned_start(planner, kanban):
    assert planner.next_due() is None
    kanban.add("t_1", status="blocked")
    kanban.add("t_2", status="blocked")
    planner.set_slot("t_1", at(16), actor=AGENT)
    planner.set_slot("t_2", at(13), actor=AGENT)
    assert planner.next_due() == at(13)


# ---- clashes ----

def plan_two_on_prod(planner, kanban):
    kanban.add("t_a", "Upgrade ingress", status="blocked", assignee="builder")
    kanban.add("t_b", "Rotate database keys", status="blocked", assignee="keeper")
    planner.label("cluster:prod", exclusive=True, actor=AGENT)
    planner.tag("t_a", add=["cluster:prod"], actor=AGENT)
    planner.tag("t_b", add=["cluster:prod"], actor=AGENT)
    planner.set_slot("t_a", at(14), estimate_s=2 * HOUR, actor=AGENT)


def test_a_clash_is_reported_with_the_next_free_start_and_does_not_refuse(planner, kanban, store):
    plan_two_on_prod(planner, kanban)
    change = planner.set_slot("t_b", at(15), estimate_s=HOUR, actor=AGENT)
    assert store.get_slot("", "t_b").start_ts == at(15)              # the move went through
    (found,) = change.clashes
    assert found.kind == CLASH and change.next_free == at(16)
    assert planner.describe(found, "t_b") == (
        'Clashes with "Upgrade ingress" (builder, Wed 11 Nov 14:00 to 16:00): both carry cluster:prod, which '
        "allows one card at a time. They overlap for 1 h.")
    assert store.log_tail()[0].detail["clashes"] == 1
    snap = planner.snapshot()
    assert [c.kind for c in snap.clashes] == [CLASH] and len(snap.clashes_of("t_a")) == 1


def test_moving_to_the_next_free_start_ends_the_clash(planner, kanban):
    plan_two_on_prod(planner, kanban)
    first = planner.set_slot("t_b", at(15), estimate_s=HOUR, actor=AGENT)
    again = planner.set_slot("t_b", first.next_free, actor=AGENT)
    assert again.clashes == [] and again.next_free is None and planner.snapshot().clashes == []


def test_two_slots_of_one_profile_are_busy_not_a_clash(planner, kanban):
    kanban.add("t_a", status="blocked", assignee="builder")
    kanban.add("t_b", status="blocked", assignee="builder")
    planner.set_slot("t_a", at(14), actor=AGENT)
    change = planner.set_slot("t_b", at(14, 30), actor=AGENT)
    assert [c.kind for c in change.clashes] == [BUSY] and change.next_free is None


def test_a_running_card_past_its_estimate_clashes_with_the_slot_it_runs_into(planner, kanban, clock):
    plan_two_on_prod(planner, kanban)                                # t_a: 14:00 to 16:00
    planner.set_slot("t_b", at(16), estimate_s=HOUR, actor=AGENT)    # right after it: no clash
    assert planner.snapshot().clashes == []
    clock.now = at(14)
    planner.run_due()
    kanban.set("t_a", status="running", started_at=at(14))
    clock.now = at(16, 20)                                           # t_a is still running, 20 minutes over
    snap = planner.snapshot()
    item = snap.item("t_a")
    assert (item.phase, item.overrun_s, item.end) == (RUNNING, 20 * 60, at(16, 20))
    assert [c.kind for c in snap.clashes] == [CLASH]


# ---- the snapshot ----

def test_the_snapshot_says_where_every_card_is(planner, kanban, store, clock):
    for task_id, status in (("t_planned", "blocked"), ("t_due", "blocked"), ("t_wait", "scheduled"),
                            ("t_run", "blocked"), ("t_done", "blocked"), ("t_gone", "blocked")):
        kanban.add(task_id, status=status)
    kanban.add("t_free", status="ready")
    kanban.add("t_wild", status="running", started_at=NOW - 600)     # runs without a slot
    planner.set_slot("t_planned", at(15), actor=AGENT)
    for task_id in ("t_due", "t_wait", "t_run", "t_done", "t_gone"):
        planner.set_slot(task_id, at(11), estimate_s=HOUR, actor=AGENT)
    clock.now = at(11)
    store.put_slot("", "t_due", start_ts=at(11), estimate_s=HOUR, state=PLANNED, parked=False, set_by=AGENT, now=NOW)
    for slot in store.claim_due(clock.now, lease_s=60, max_attempts=MAX_ATTEMPTS):
        if slot.task_id != "t_due":
            planner._release_due(slot, clock.now)
    kanban.set("t_run", status="running", started_at=at(11, 5))
    kanban.set("t_done", status="done", started_at=at(11, 2), completed_at=at(11, 40))
    del kanban.tasks["t_gone"]
    clock.now = at(12)
    snap = planner.snapshot()
    phases = {i.task_id: i.phase for i in snap.items}
    assert phases == {"t_planned": PLANNED_, "t_due": DUE, "t_wait": WAITING, "t_run": RUNNING, "t_done": DONE,
                      "t_gone": GONE, "t_free": UNPLANNED, "t_wild": RUNNING}
    done = snap.item("t_done")
    assert (done.start, done.end) == (at(11, 2), at(11, 40))
    run = snap.item("t_run")
    assert (run.start, run.end, run.overrun_s) == (at(11, 5), at(12, 5), 0)
    wild = snap.item("t_wild")
    assert (wild.slot, wild.start) == (None, NOW - 600)
    assert {s.task_id for s in snap.spans} == {"t_planned", "t_due", "t_wait", "t_run", "t_wild"}   # done and gone take no time


def test_an_unreadable_board_still_shows_the_slots(planner, kanban, clock):
    kanban.add("t_1", status="blocked")
    planner.set_slot("t_1", at(14), actor=AGENT)
    kanban.down = True
    snap = planner.snapshot()
    assert "kanban_list failed" in snap.board_error
    (item,) = snap.items
    assert (item.phase, item.card, item.title) == (PLANNED_, None, "t_1")


def test_old_finished_slots_are_not_read(planner, kanban, store, clock):
    kanban.add("t_old", status="done")
    store.put_slot("", "t_old", start_ts=NOW - 9 * 86400, estimate_s=HOUR, state=RELEASED, parked=False, set_by=AGENT, now=NOW)
    assert planner.snapshot().items == [] and kanban.count("kanban_show") == 0


# ---- labels ----

def test_labels_are_created_on_the_fly_and_validated(planner, kanban, store):
    kanban.add("t_1")
    assert planner.tag("t_1", add=["Repo:Web", "op:upgrade"], actor=AGENT) == ["op:upgrade", "repo:web"]
    assert [(l.name, l.exclusive) for l in store.labels()] == [("op:upgrade", False), ("repo:web", False)]
    planner.label("repo:web", exclusive=True, description="  the  web\nrepository ", actor=OWNER)
    label = store.get_label("repo:web")
    assert (label.exclusive, label.description) == (True, "the web repository")
    assert planner.tag("t_1", remove=["op:upgrade"], actor=AGENT) == ["repo:web"]
    for bad in ("", "has space", "a:b:c", "-x", "x" * 40):
        assert "A label is lower-case" in refusal(lambda: planner.label(bad, actor=AGENT))
    assert "not on the board" in refusal(lambda: planner.tag("t_9", add=["repo:web"], actor=AGENT))
    planner.drop_label("repo:web", actor=OWNER)
    assert store.card_labels("") == {} and "There is no label" in refusal(lambda: planner.drop_label("repo:web", actor=OWNER))


def test_tenant_and_project_from_the_board_appear_as_labels(planner, kanban):
    kanban.add("t_1", tenant="Shop", project_id="billing")
    planner.tag("t_1", add=["op:upgrade"], actor=AGENT)
    assert planner.snapshot().item("t_1").labels == ("op:upgrade", "tenant:shop", "project:billing")


def test_a_board_label_can_be_made_one_at_a_time(planner, kanban):
    kanban.add("t_a", status="blocked", assignee="builder", tenant="shop")
    kanban.add("t_b", status="blocked", assignee="keeper", tenant="shop")
    planner.label("tenant:shop", exclusive=True, actor=OWNER)
    planner.set_slot("t_a", at(14), actor=OWNER)
    assert [c.kind for c in planner.set_slot("t_b", at(14, 30), actor=OWNER).clashes] == [CLASH]


# ---- who changed what ----

def test_an_agent_sees_what_others_changed_since_its_last_look(planner, kanban, clock):
    kanban.add("t_1", status="blocked")
    kanban.add("t_2", status="blocked")
    planner.set_slot("t_1", at(14), actor=AGENT)
    assert planner.changes_since_last_look(AGENT) == []              # its own change is not news
    planner.set_slot("t_1", at(16), actor=OWNER)
    planner.set_slot("t_2", at(11), actor=AGENT)
    clock.now = at(11)
    planner.run_due()
    news = planner.changes_since_last_look(AGENT)
    assert [(e.actor, e.action) for e in news] == [(OWNER, "move"), ("timer", "release")]
    assert planner.changes_since_last_look(AGENT) == []              # asked and seen
    assert [e.actor for e in planner.changes_since_last_look("agent:other", mark=False)] == [AGENT, OWNER, AGENT, "timer"]


def test_who_and_times_in_words(planner):
    assert [planner.who(a) for a in (OWNER, "timer", AGENT)] == ["the owner", "the timer", "profile assistant"]
    assert planner.fmt(at(14)) == "Wed 11 Nov 14:00"
    assert planner.span_text(at(14), at(15, 30)) == "Wed 11 Nov 14:00 to 15:30"
    assert planner.span_text(at(23), at(1, day=12)) == "Wed 11 Nov 23:00 to Thu 12 Nov 01:00"


def test_missed_and_failed_slots_show_as_such(planner, kanban, store, clock):
    kanban.add("t_1", status="scheduled")
    planner.set_slot("t_1", at(11), estimate_s=600, actor=AGENT)
    clock.now = at(13)
    planner.run_due()
    assert planner.snapshot().item("t_1").phase == MISSED_
    store.put_slot("", "t_1", start_ts=at(11), estimate_s=600, state=FAILED, parked=False, set_by=AGENT, now=NOW)
    assert planner.snapshot().item("t_1").phase == FAILED_
