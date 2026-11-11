"""The orchestrator's tools: JSON in, JSON out, never an exception."""
import json

import pytest

from chronomancer.planner import OWNER, PlanError
from chronomancer.tools import SCHEMAS, Tools, parse_time
from conftest import HOUR, NOW, ZONE, at

AGENT = "agent:assistant"


@pytest.fixture
def tools(planner, clock):
    return Tools(planner, actor=lambda: AGENT, clock=clock)


def call(handler, **args):
    return json.loads(handler(args))


def test_times_are_iso_or_a_delay():
    assert parse_time("2026-11-11T14:00", ZONE, NOW) == at(14)                  # read in the plugin's zone
    assert parse_time("2026-11-11T14:00+00:00", ZONE, NOW) == at(15)            # an offset wins
    assert parse_time("2026-11-11T13:00Z", ZONE, NOW) == at(14)
    assert [parse_time(v, ZONE, NOW) - NOW for v in ("+90m", "+2h", "+1d", "+ 15 min", "now")] == [5400, 7200, 86400, 900, 0]
    assert parse_time(at(14), ZONE, NOW) == at(14)                              # the Desktop page sends seconds
    for bad in ("", "tomorrow", None, True, "+2w"):
        with pytest.raises(PlanError):
            parse_time(bad, ZONE, NOW)


def test_slot_plans_a_card_and_reports_what_happened(tools, kanban):
    kanban.add("t_1", "Upgrade ingress")
    out = call(tools.slot, card="t_1", start="2026-11-11T14:00", estimate_minutes=90)
    assert out["ok"] and kanban.status("t_1") == "scheduled"
    assert out["slot"] == {"start": "2026-11-11T14:00+01:00", "end": "2026-11-11T15:30+01:00", "start_ts": at(14),
                           "estimate_minutes": 90, "pinned_by_owner": False, "state": "planned"}
    assert out["message"] == ('Planned "Upgrade ingress" for Wed 11 Nov 14:00 to 15:30. '
                              "The card was ready and is parked until then.")
    assert (out["action"], out["card"], out["status_before"]) == ("plan", "t_1", "ready")


def test_slot_reports_a_clash_and_the_next_free_start(tools, kanban, planner):
    kanban.add("t_a", "Upgrade ingress", status="blocked", assignee="builder")
    kanban.add("t_b", "Rotate database keys", status="blocked", assignee="keeper")
    out = call(tools.label, label="cluster:prod", one_at_a_time=True, description="the production cluster",
               cards=["t_a", "t_b"])
    assert out == {"ok": True, "label": "cluster:prod", "one_at_a_time": True, "description": "the production cluster",
                   "cards": {"t_a": ["cluster:prod"], "t_b": ["cluster:prod"]}}
    call(tools.slot, card="t_a", start="+4h", estimate_minutes=120)
    out = call(tools.slot, card="t_b", start="+5h", estimate_minutes=60, dry_run=True)
    assert out["action"] == "preview" and planner.store.get_slot("", "t_b") is None
    assert out["clashes"] == ['Clashes with "Upgrade ingress" (builder, Wed 11 Nov 14:00 to 16:00): both carry '
                              "cluster:prod, which allows one card at a time. They overlap for 1 h."]
    assert out["next_free_start"] == "2026-11-11T16:00+01:00"
    assert out["message"].endswith("It clashes with 1 other slot. The next start without a clash is Wed 11 Nov 16:00.")


def test_refusals_come_back_as_errors_not_exceptions(tools, kanban, planner):
    kanban.add("t_1", status="running")
    assert call(tools.slot, card="t_1", start="+1h") == {
        "ok": False, "error": '"Card t_1" is running now. A running card has no start to plan.'}
    assert "not a time" in call(tools.slot, card="t_1", start="soon")["error"]
    assert "whole number" in call(tools.slot, card="t_1", start="+1h", estimate_minutes="60")["error"]
    assert "not on the board" in call(tools.now, card="t_9")["error"]
    assert "has no slot" in call(tools.clear, card="t_1")["error"]
    assert "A card id looks like" in call(tools.slot, start="+1h")["error"]
    assert "true or false" in call(tools.label, label="x", one_at_a_time="yes")["error"]
    assert json.loads(tools.slot(None))["ok"] is False and json.loads(tools.plan("nonsense"))["ok"] is True


def test_an_agent_cannot_move_what_the_owner_pinned(tools, kanban, planner):
    kanban.add("t_1", status="blocked")
    planner.set_slot("t_1", at(14), actor=OWNER)
    planner.pin("t_1", True, actor=OWNER)
    out = call(tools.slot, card="t_1", start="+6h")
    assert out["ok"] is False and out["error"].startswith("The owner pinned this slot at Wed 11 Nov 14:00.")
    assert call(tools.plan)["slots"][0]["pinned_by_owner"] is True


def test_an_unreachable_board_is_an_error_that_says_nothing_changed(tools, kanban):
    kanban.add("t_1")
    kanban.down = True
    assert "cannot be reached" in call(tools.slot, card="t_1", start="+1h")["error"]
    plan = call(tools.plan)                                          # the plan itself is still readable
    assert plan["ok"] and "kanban_list failed" in plan["board_error"]


def test_an_unexpected_failure_is_reported_without_details(tools, planner):
    planner.snapshot = lambda *a, **k: (_ for _ in ()).throw(ValueError("secret path /x"))
    out = call(tools.plan)
    assert out == {"ok": False, "error": "chrono_plan failed (ValueError). Nothing may have changed; read the plan again."}


def test_now_and_clear(tools, kanban, planner):
    kanban.add("t_1", "Upgrade ingress", status="scheduled")
    call(tools.slot, card="t_1", start="+3h")
    out = call(tools.now, card="t_1")
    assert out["message"] == '"Upgrade ingress" runs now (released now: ready).' and kanban.status("t_1") == "ready"
    out = call(tools.clear, card="t_1")
    assert out["message"] == 'Took "Upgrade ingress" out of the plan. The card is ready: the Kanban dispatcher starts it without the plan.'
    assert planner.store.get_slot("", "t_1") is None


def test_plan_is_compact_and_filters_by_profile_and_label(tools, kanban, planner, clock):
    kanban.add("t_a", "Upgrade ingress", status="blocked", assignee="builder")
    kanban.add("t_b", "Rotate database keys", status="blocked", assignee="keeper")
    kanban.add("t_c", "Write the release notes", status="ready", assignee="builder")
    kanban.add("t_far", "Next month", status="blocked", assignee="builder")
    call(tools.label, label="repo:web", cards=["t_a", "t_c"])
    call(tools.slot, card="t_a", start="2026-11-11T14:00", estimate_minutes=120)
    call(tools.slot, card="t_b", start="2026-11-12T09:00")
    call(tools.slot, card="t_far", start="2026-11-20T09:00")
    plan = call(tools.plan, horizon="2d")
    assert plan["now"] == "2026-11-11T10:00+01:00"
    assert plan["horizon"] == {"from": "2026-11-11T07:00+01:00", "to": "2026-11-13T10:00+01:00"}
    assert [s["card"] for s in plan["slots"]] == ["t_a", "t_b"]      # t_far is beyond the horizon
    assert plan["slots"][0] == {"card": "t_a", "title": "Upgrade ingress", "profile": "builder", "status": "blocked",
                                "phase": "planned", "start": "2026-11-11T14:00+01:00", "end": "2026-11-11T16:00+01:00",
                                "labels": ["repo:web"], "estimate_minutes": 120}
    assert plan["unplanned"] == [{"card": "t_c", "title": "Write the release notes", "profile": "builder",
                                  "status": "ready", "labels": ["repo:web"]}]
    assert plan["labels"] == [{"name": "repo:web", "one_at_a_time": False}]
    assert [s["card"] for s in call(tools.plan, horizon="week", profile="keeper")["slots"]] == ["t_b"]
    assert [s["card"] for s in call(tools.plan, horizon="day", label="repo:web")["slots"]] == ["t_a"]
    assert [s["card"] for s in call(tools.plan, horizon="week")["slots"]] == ["t_a", "t_b"]   # a week is 7 days


def test_plan_tells_the_agent_what_the_owner_and_the_timer_did(tools, kanban, planner, clock):
    kanban.add("t_1", "Upgrade ingress", status="blocked")
    kanban.add("t_2", "Rotate database keys", status="blocked")
    call(tools.slot, card="t_1", start="2026-11-11T14:00")
    call(tools.slot, card="t_2", start="2026-11-11T11:00")
    assert call(tools.plan)["since_your_last_look"] == []
    planner.set_slot("t_1", at(16), actor=OWNER)
    clock.now = at(11)
    news = call(tools.plan)["since_your_last_look"]                  # reading the plan also runs what is due
    assert news == [
        {"when": "2026-11-11T10:00+01:00", "what": 'The owner moved "Upgrade ingress" from Wed 11 Nov 14:00 to Wed 11 Nov 16:00.'},
        {"when": "2026-11-11T11:00+01:00", "what": 'The timer released "Rotate database keys" at its slot.'}]
    assert call(tools.plan)["since_your_last_look"] == []


def test_a_long_tray_is_cut_and_counted(tools, kanban):
    for n in range(45):
        kanban.add(f"t_{n:02d}", status="blocked")
    plan = call(tools.plan)
    assert len(plan["unplanned"]) == 40 and plan["unplanned_more"] == 5


def test_a_label_can_be_deleted_and_taken_off_cards(tools, kanban, planner):
    kanban.add("t_1")
    call(tools.label, label="repo:web", cards=["t_1"])
    assert call(tools.label, label="repo:web", remove_cards=["t_1"])["cards"] == {"t_1": []}
    assert call(tools.label, label="Repo:Web", delete=True) == {"ok": True, "label": "repo:web", "deleted": True}
    assert planner.store.labels() == []


def test_schemas_are_complete_and_named_alike():
    assert [s["name"] for s in SCHEMAS] == ["chrono_plan", "chrono_slot", "chrono_now", "chrono_clear", "chrono_label"]
    for schema in SCHEMAS:
        params = schema["parameters"]
        assert params["type"] == "object" and set(params["required"]) <= set(params["properties"])
        assert len(schema["description"]) > 80
