"""The owner's Desktop surface: every move is the owner's, and errors are HTTP answers."""
import pytest

from chronomancer.desktop import DesktopApi
from chronomancer.planner import OWNER
from conftest import HOUR, at

AGENT = "agent:assistant"


@pytest.fixture
def api(planner):
    return DesktopApi(planner)


def test_the_plan_is_served_and_due_slots_run_on_a_read(api, planner, kanban, clock):
    kanban.add("t_1", status="scheduled")
    planner.set_slot("t_1", at(11), actor=AGENT)
    clock.now = at(11)
    status, body = api.plan("day")
    assert status == 200 and body["horizon"]["key"] == "day"
    assert kanban.status("t_1") == "ready" and body["items"][0]["phase"] == "waiting"
    assert api.plan(None)[1]["horizon"]["key"] == "2d"


def test_the_owner_moves_pins_runs_and_clears(api, planner, kanban, store):
    kanban.add("t_1", "Upgrade ingress", status="blocked")
    status, body = api.set_slot("t_1", {"start": at(14), "estimate_minutes": 45})
    assert status == 200 and body["change"]["action"] == "plan"
    slot = store.get_slot("", "t_1")
    assert (slot.start_ts, slot.estimate_s, slot.set_by) == (at(14), 45 * 60, OWNER)
    assert api.set_slot("t_1", {"start": "2026-11-11T16:00"})[1]["change"]["action"] == "move"
    assert api.pin("t_1", {"pinned": True})[1]["change"]["slot"]["pinned_by_owner"] is True
    assert api.set_slot("t_1", {"start": at(17)})[0] == 200          # the owner moves a pinned slot
    assert api.now("t_1")[1]["change"]["message"] == '"Upgrade ingress" runs now (released now: ready).'
    assert api.clear("t_1")[0] == 200 and store.get_slot("", "t_1") is None
    assert {e.actor for e in store.log_tail()} == {OWNER}


def test_a_preview_changes_nothing(api, kanban, store):
    kanban.add("t_1")
    status, body = api.set_slot("t_1", {"start": at(14), "dry_run": True})
    assert status == 200 and body["change"]["action"] == "preview" and store.slots("") == []


def test_refusals_are_409_bad_input_is_400_or_409_and_a_dead_board_says_so(api, kanban, store):
    kanban.add("t_run", status="running")
    kanban.add("t_1", status="blocked")
    assert api.set_slot("t_run", {"start": at(14)}) == (409, {"detail": '"Card t_run" is running now. A running card has no start to plan.'})
    assert api.set_slot("t_1", {})[0] == 409 and api.set_slot("t_1", None)[0] == 409
    assert api.set_slot("t_1", {"start": at(14), "estimate_minutes": "x"})[0] == 409
    assert api.pin("t_1", {"pinned": "yes"}) == (400, {"detail": "pinned is true or false."})
    assert api.pin("t_1", {"pinned": True})[0] == 409                # no slot to pin
    assert api.clear("t_1")[0] == 409
    assert api.label({"name": "x", "exclusive": "no"})[0] == 400
    assert api.tag("t_1", {"add": "repo:web"})[0] == 400
    kanban.down = True
    status, body = api.set_slot("t_1", {"start": at(14)})
    assert status == 409 and "cannot be reached" in body["detail"]
    assert store.slots("") == []


def test_labels_from_the_page(api, kanban, store):
    kanban.add("t_1")
    assert api.label({"name": "Cluster:Prod", "exclusive": True, "description": "the production cluster"}) == (
        200, {"label": {"name": "cluster:prod", "exclusive": True, "description": "the production cluster"}})
    assert api.tag("t_1", {"add": ["cluster:prod", "repo:web"]}) == (200, {"labels": ["cluster:prod", "repo:web"]})
    assert api.tag("t_1", {"remove": ["repo:web"]}) == (200, {"labels": ["cluster:prod"]})
    assert api.drop_label("cluster:prod") == (200, {"deleted": "cluster:prod"})
    assert api.drop_label("cluster:prod")[0] == 409


def test_settings_are_refreshed_before_each_request(planner, kanban):
    seen = []
    api = DesktopApi(planner, refresh=lambda: seen.append(1))
    api.plan("day")
    api.label({"name": "repo:web"})
    assert len(seen) == 2
