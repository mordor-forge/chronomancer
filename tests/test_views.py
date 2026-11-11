"""What the owner's page gets: the horizon, the cards in time, the tray, the labels and the log."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from chronomancer import views
from chronomancer.board import Board
from chronomancer.planner import OWNER, Planner
from conftest import HOUR, NOW, ZONE, Clock, at

AGENT = "agent:assistant"


def hm(ts):
    return datetime.fromtimestamp(ts, ZONE).strftime("%a %H:%M")


# ---- the horizon ----

def test_day_horizon_starts_two_hours_back_and_covers_the_next_24_hours(planner):
    h = views.horizon(planner, "day", NOW)
    assert (h["key"], hm(h["start"]), hm(h["end"])) == ("day", "Wed 08:00", "Thu 10:00")
    assert h["label"] == "Wed 11 Nov 08:00 to Thu 12 Nov 10:00"
    assert len(h["ticks"]) == 27 and h["ticks"][0]["label"] == "08:00"
    midnight = [t for t in h["ticks"] if t["day"]]
    assert [(t["label"], hm(t["ts"])) for t in midnight] == [("Thu 12", "Thu 00:00")]
    assert [(d["label"], d["today"], hm(d["start"]), hm(d["end"])) for d in h["days"]] == [
        ("Wed 11 Nov", True, "Wed 08:00", "Thu 00:00"), ("Thu 12 Nov", False, "Thu 00:00", "Thu 10:00")]
    assert [(hm(n["start"]), hm(n["end"])) for n in h["nights"]] == [("Wed 22:00", "Thu 00:00"), ("Thu 00:00", "Thu 06:00")]


def test_two_day_and_week_horizons(planner):
    two = views.horizon(planner, "2d", NOW)
    assert (hm(two["start"]), hm(two["end"])) == ("Wed 07:00", "Fri 10:00")
    assert [t["label"] for t in two["ticks"]][:6] == ["09:00", "12:00", "15:00", "18:00", "21:00", "Thu 12"]
    week = views.horizon(planner, "week", NOW)
    assert (hm(week["start"]), week["end"] - week["start"]) == ("Wed 00:00", 7 * 86400)
    assert len(week["days"]) == 7 and len(week["ticks"]) == 15 and week["ticks"][1]["label"] == "12:00"


def test_a_number_of_hours_is_a_horizon_and_nonsense_is_the_default(planner):
    six = views.horizon(planner, "6", NOW)
    assert (six["key"], hm(six["start"]), hm(six["end"])) == ("6", "Wed 08:00", "Wed 16:00")
    assert views.horizon(planner, "9999", NOW)["key"] == "336"
    assert views.horizon(planner, "soon", NOW)["key"] == "2d" and views.horizon(planner, None, NOW)["key"] == "2d"


def test_a_week_across_a_daylight_saving_change_keeps_local_midnights(planner):
    now = int(datetime(2027, 3, 27, 10, 0, tzinfo=ZONE).timestamp())        # the clocks change on Sun 28 Mar
    week = views.horizon(planner, "week", now)
    lengths = [(d["label"], (d["end"] - d["start"]) // 3600) for d in week["days"]]
    assert lengths == [("Sat 27 Mar", 24), ("Sun 28 Mar", 23), ("Mon 29 Mar", 24), ("Tue 30 Mar", 24),
                       ("Wed 31 Mar", 24), ("Thu 01 Apr", 24), ("Fri 02 Apr", 24)]
    assert week["end"] - week["start"] == 167 * 3600 and hm(week["end"]) == "Sat 00:00"
    days = [t for t in week["ticks"] if t["day"]]
    assert all(datetime.fromtimestamp(t["ts"], ZONE).hour == 0 for t in days) and len(days) == 8   # both ends


def test_the_horizon_follows_the_plugin_zone(store, kanban):
    tokyo = Planner(store, Board(kanban), setting=lambda k, d=None: d, tz=lambda: ZoneInfo("Asia/Tokyo"), clock=Clock())
    h = views.horizon(tokyo, "day", NOW)
    assert h["ticks"][0]["label"] == "16:00"                                # 10:00 in Prague is 18:00 in Tokyo


# ---- the page ----

@pytest.fixture
def busy_plan(planner, kanban, clock):
    kanban.add("t_a", "Upgrade ingress", status="blocked", assignee="builder", priority=3)
    kanban.add("t_b", "Rotate database keys", status="blocked", assignee="keeper")
    kanban.add("t_c", "Write the release notes", status="ready", assignee="builder")
    kanban.add("t_d", "Triage me", status="triage", assignee="")
    kanban.add("t_far", "Next week", status="blocked", assignee="keeper")
    planner.label("cluster:prod", exclusive=True, description="the production cluster", actor=AGENT)
    planner.tag("t_a", add=["cluster:prod", "repo:web"], actor=AGENT)
    planner.tag("t_b", add=["cluster:prod"], actor=AGENT)
    planner.set_slot("t_a", at(14), estimate_s=2 * HOUR, actor=AGENT)
    planner.set_slot("t_b", at(15), estimate_s=HOUR, actor=OWNER)
    planner.set_slot("t_far", at(9, day=19), actor=AGENT)
    planner.pin("t_b", True, actor=OWNER)
    return planner


def test_the_page_gets_lanes_items_tray_labels_counts_and_log(busy_plan):
    view = views.owner_view(busy_plan, horizon_key="day")
    assert (view["now"], view["now_text"], view["zone"], view["board"], view["board_error"]) == (NOW, "Wed 11 Nov 10:00", "CET", "", "")
    assert (view["tz"], view["utc_offset_s"], view["suggest_local"], view["default_estimate_min"]) == (
        "Europe/Prague", 3600, "2026-11-11T11:00", 60)
    assert view["horizon"]["key"] == "day"
    assert view["lanes"] == [{"profile": "", "label": "No profile", "slots": 0},
                             {"profile": "builder", "label": "builder", "slots": 1},
                             {"profile": "keeper", "label": "keeper", "slots": 1}]
    assert [i["task_id"] for i in view["items"]] == ["t_a", "t_b"]
    assert [i["task_id"] for i in view["tray"]] == ["t_c", "t_d"]
    assert view["later"] == {"count": 1, "next": "Thu 19 Nov 09:00 to 10:00", "next_title": "Next week",
                             "text": '1 more slot after this horizon. Next: "Next week", Thu 19 Nov 09:00 to 10:00.'}
    assert view["counts"] == {"clashes": 1, "busy": 0, "planned": 2, "running": 0, "attention": 0, "tray": 2}
    assert view["labels"] == [
        {"name": "cluster:prod", "exclusive": True, "description": "the production cluster", "cards": 2, "derived": False},
        {"name": "repo:web", "exclusive": False, "description": "", "cards": 1, "derived": False}]
    assert view["log"][0]["text"] == 'The owner pinned "Rotate database keys" at Wed 11 Nov 15:00.'
    assert view["log"][0]["by_owner"] is True and view["log"][0]["when"] == "Wed 11 Nov 10:00"
    quiet = [e["text"] for e in view["log"] if e["quiet"]]           # label housekeeping: shown only on request
    assert len(quiet) == 4 and all("label" in text for text in quiet) and view["log"][0]["quiet"] is False


def test_one_card_on_the_page(busy_plan):
    view = views.owner_view(busy_plan, horizon_key="day")
    card = view["items"][0]
    assert card == {
        "task_id": "t_a", "title": "Upgrade ingress", "short": "Upgrade ingress", "assignee": "builder",
        "status": "blocked", "phase": "planned", "priority": 3, "model": "", "labels": ["cluster:prod", "repo:web"],
        "start": at(14), "end": at(16), "planned_start": at(14), "planned_end": at(16), "estimate_s": 2 * HOUR,
        "estimate_min": 120, "estimate": "2 h", "start_local": "2026-11-11T14:00",
        "when": "Wed 11 Nov 14:00 to 16:00", "pinned": False, "note": "",
        "set_by": AGENT, "set_by_text": "Planned by profile assistant.", "line": "Starts in 4 h.", "overrun_s": 0,
        "clash": True, "busy": False,
        "clashes": [{"kind": "clash", "with": "t_b", "start": at(15), "end": at(16),
                     "text": 'Clashes with "Rotate database keys" (keeper, Wed 11 Nov 15:00 to 16:00): both carry '
                             "cluster:prod, which allows one card at a time. They overlap for 1 h."}],
        "next_free": at(16), "next_free_text": "Wed 11 Nov 16:00",
        "can": {"move": True, "now": True, "clear": True, "pin": True}}
    assert view["items"][1]["pinned"] is True and view["items"][1]["set_by_text"] == "Planned by the owner."


def test_the_headline_says_what_runs_next_and_what_needs_a_look(busy_plan, kanban, clock):
    view = views.owner_view(busy_plan, horizon_key="day")
    assert view["summary"] == {
        "headline": 'Next: "Upgrade ingress" (builder) in 4 h, at 14:00.',
        "facts": [{"kind": "clash", "text": "1 clash"}, {"kind": "planned", "text": "2 planned"},
                  {"kind": "tray", "text": "2 unplanned"}],
        "chip": "next 14:00 · 1 clash", "tip": 'Next: "Upgrade ingress" (builder) in 4 h, at 14:00.'}
    clock.now = at(15)
    busy_plan.run_due()
    kanban.set("t_a", status="running", started_at=at(14))
    kanban.set("t_b", status="running", started_at=at(15))
    summary = views.owner_view(busy_plan, horizon_key="day")["summary"]
    assert summary["headline"] == 'Next: "Next week" (keeper) in 7 d 18 h, at Thu 09:00.'
    assert summary["facts"][0] == {"kind": "running", "text": "2 running"} and summary["chip"].startswith("next Thu 09:00")


def test_the_headline_without_any_slot(planner, kanban):
    assert views.owner_view(planner)["summary"] == {"headline": "Nothing is planned.", "facts": [], "chip": "no slots",
                                                    "tip": "Nothing is planned."}
    kanban.add("t_1", status="running", started_at=NOW - 60)
    summary = views.owner_view(planner)["summary"]
    assert (summary["headline"], summary["chip"]) == ("1 card runs now. Nothing else is planned.", "1 running")


def test_badges_for_the_kanban_board(busy_plan, kanban, clock):
    view = views.owner_view(busy_plan, horizon_key="day")           # every slot gets one, also beyond the horizon
    assert view["badges"] == {
        "t_a": {"label": "clash Wed 14:00", "tone": "red", "tooltip": "Chronomancer. Wed 11 Nov 14:00 to 16:00. Starts in 4 h."},
        "t_b": {"label": "clash Wed 15:00", "tone": "red", "tooltip": "Chronomancer. Wed 11 Nov 15:00 to 16:00. Starts in 5 h."},
        "t_far": {"label": "slot Thu 09:00", "tone": "blue", "tooltip": "Chronomancer. Thu 19 Nov 09:00 to 10:00. Starts in 7 d 23 h."}}
    clock.now = at(15)
    busy_plan.run_due()
    kanban.set("t_a", status="running", started_at=at(14))
    badges = views.owner_view(busy_plan)["badges"]
    assert "t_a" not in badges                                       # the board itself shows that a card runs
    assert (badges["t_b"]["label"], badges["t_b"]["tone"]) == ("released Wed 15:00", "green")
    busy_plan.store.put_slot("", "t_far", start_ts=at(9), estimate_s=600, state="missed", parked=False, set_by="owner", now=NOW)
    assert views.owner_view(busy_plan)["badges"]["t_far"]["label"] == "missed its slot"


def test_tray_cards_say_what_happens_to_them(busy_plan):
    tray = {i["task_id"]: i for i in views.owner_view(busy_plan)["tray"]}
    assert tray["t_c"]["line"] == "Ready. The Kanban starts it without the plan."
    assert tray["t_c"]["can"] == {"move": True, "now": True, "clear": False, "pin": False}
    assert tray["t_d"]["line"] == "In triage. Specify the card before it can be planned."
    assert tray["t_d"]["can"]["move"] is False and tray["t_d"]["when"] == ""


def test_the_status_line_follows_the_card(busy_plan, kanban, clock):
    def line(task_id="t_a"):
        view = views.owner_view(busy_plan, horizon_key="week")
        return next(i for i in view["items"] if i["task_id"] == task_id)["line"]

    clock.now = at(14)
    busy_plan.board.dispatch("kanban_show", {"task_id": "t_a"})
    busy_plan.store.put_slot("", "t_a", start_ts=at(14), estimate_s=2 * HOUR, state="planned", parked=False, set_by=AGENT, now=NOW)
    assert line() == "Its slot has started. The timer releases it within a minute."
    busy_plan.run_due()
    assert line() == "Released Wed 11 Nov 14:00. The card is ready."
    kanban.set("t_a", status="running", started_at=at(14, 5))
    clock.now = at(14, 30)
    assert line() == "Running for 25 min."
    clock.now = at(16, 35)
    assert line() == "Running for 2 h 30 min, 30 min over its estimate."
    kanban.set("t_a", status="done", completed_at=at(16, 40))
    assert line() == "Done Wed 11 Nov 16:40."


def test_missed_failed_and_gone_cards_need_attention(planner, kanban, clock):
    for task_id in ("t_missed", "t_failed", "t_gone"):
        kanban.add(task_id, status="scheduled")
    planner.run_now("t_gone", actor=AGENT)                           # released, and then deleted from the board
    planner.set_slot("t_missed", at(11), estimate_s=600, actor=AGENT)
    planner.set_slot("t_failed", at(12, 55), estimate_s=600, actor=AGENT)
    kanban.set("t_failed", status="triage")
    del kanban.tasks["t_gone"]
    clock.now = at(13)
    planner.run_due()
    view = views.owner_view(planner, horizon_key="week")
    lines = {i["task_id"]: (i["phase"], i["line"]) for i in view["items"]}
    assert lines == {
        "t_missed": ("missed", "Missed its slot: the timer was not running. Move it or run it now."),
        "t_failed": ("failed", "Not released: the card is in triage and cannot be released."),
        "t_gone": ("gone", "The card is no longer on the board.")}
    assert view["counts"]["attention"] == 3


def test_a_board_label_is_listed_as_derived(planner, kanban):
    kanban.add("t_1", tenant="shop")
    assert views.owner_view(planner)["labels"] == [
        {"name": "tenant:shop", "exclusive": False, "description": "From the board.", "cards": 1, "derived": True}]


def test_the_page_shows_slots_when_the_board_is_down(busy_plan, kanban):
    kanban.down = True
    view = views.owner_view(busy_plan, horizon_key="day")
    assert "kanban_list failed" in view["board_error"]
    assert [(i["task_id"], i["title"], i["can"]["move"]) for i in view["items"]] == [("t_a", "t_a", False), ("t_b", "t_b", False)]


# ---- the log in words ----

def test_every_kind_of_change_reads_as_a_sentence(planner, kanban, clock):
    kanban.add("t_1", "Upgrade ingress", status="blocked")
    kanban.add("t_2", "Rotate database keys", status="scheduled")
    planner.set_slot("t_1", at(14), estimate_s=90 * 60, actor=AGENT)
    planner.set_slot("t_1", at(16), actor=OWNER)
    planner.pin("t_1", True, actor=OWNER)
    planner.pin("t_1", False, actor=OWNER)
    planner.label("cluster:prod", exclusive=True, actor=AGENT)
    planner.tag("t_1", add=["cluster:prod"], actor=AGENT)
    planner.tag("t_1", remove=["cluster:prod"], actor=AGENT)
    planner.drop_label("cluster:prod", actor=OWNER)
    planner.run_now("t_1", actor=OWNER)
    planner.set_slot("t_2", at(11), estimate_s=600, actor=AGENT)
    planner.clear("t_2", actor=AGENT)
    planner.set_slot("t_2", at(11), estimate_s=600, actor=AGENT)
    clock.now = at(11)
    planner.run_due()
    texts = [views.log_text(planner, e) for e in reversed(planner.store.log_tail(50))]
    assert texts == [
        'Profile assistant planned "Upgrade ingress" for Wed 11 Nov 14:00 (1 h 30 min).',
        'The owner moved "Upgrade ingress" from Wed 11 Nov 14:00 to Wed 11 Nov 16:00.',
        'The owner pinned "Upgrade ingress" at Wed 11 Nov 16:00.',
        'The owner unpinned "Upgrade ingress".',
        "Profile assistant created label cluster:prod (one card at a time).",
        'Profile assistant set the labels of "Upgrade ingress" to cluster:prod.',
        'Profile assistant removed every label from "Upgrade ingress".',
        "The owner removed label cluster:prod.",
        'The owner ran "Upgrade ingress" now (it was planned for Wed 11 Nov 16:00).',
        'Profile assistant planned "Rotate database keys" for Wed 11 Nov 11:00 (10 min).',
        'Profile assistant took "Rotate database keys" out of the plan (it was planned for Wed 11 Nov 11:00).',
        'Profile assistant planned "Rotate database keys" for Wed 11 Nov 11:00 (10 min).',
        'The timer released "Rotate database keys" at its slot.',
    ]


def test_missed_and_failed_log_sentences(planner, kanban, clock):
    kanban.add("t_1", "Upgrade ingress", status="scheduled")
    kanban.add("t_2", "Rotate database keys", status="blocked")
    planner.set_slot("t_1", at(11), estimate_s=600, actor=AGENT)
    planner.set_slot("t_2", at(12, 50), estimate_s=600, actor=AGENT)
    kanban.set("t_2", status="triage")
    clock.now = at(13)
    planner.run_due()
    texts = {views.log_text(planner, e) for e in planner.store.log_tail(2)}
    assert texts == {'"Upgrade ingress" missed its slot at Wed 11 Nov 11:00: the timer was not running.',
                     'The timer could not release "Rotate database keys": the card is in triage and cannot be released.'}
