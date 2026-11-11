"""The Desktop half (desktop/plugin.js), run in Node against stand-ins for the app's SDK and React.

The page is rendered from what the backend really serves for the demo scenario (``views.owner_view``
on the seeded fake board), so a change of a served field shows up here.
"""
import json
import re
import shutil
import subprocess

import pytest

from chronomancer import clashes, views
from conftest import NOW, ROOT
from test_seed_demo import FakeStage, seed_demo

PLUGIN = ROOT / "desktop" / "plugin.js"
SOURCE = PLUGIN.read_text()

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    import sqlite3

    from chronomancer.board import Board
    from chronomancer.planner import Planner
    from chronomancer.store import Store
    from conftest import ZONE, Clock
    from fake_kanban import FakeKanban

    tmp = tmp_path_factory.mktemp("desktop")
    kanban, clock = FakeKanban(), Clock(NOW + 7 * 60)
    planner = Planner(Store(lambda: sqlite3.connect(tmp / "plan.db")), Board(kanban, list_ttl_s=0, card_ttl_s=0, closed_ttl_s=0),
                      setting=lambda key, default=None: default, tz=lambda: ZONE, clock=clock)
    ids = seed_demo.scenario(FakeStage(kanban), planner, clock, clock.now)
    view = views.owner_view(planner, horizon_key="2d")
    (tmp / "view.json").write_text(json.dumps(view))
    done = subprocess.run(["node", str(ROOT / "tests" / "desktop_harness.mjs"), str(PLUGIN), str(tmp / "view.json")],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout), view, ids


# ---- what it is allowed to be ----

def test_it_imports_only_the_sdk_and_react_and_has_no_colour_of_its_own():
    assert sorted(set(re.findall(r"from '([^']+)'", SOURCE))) == ["@hermes/plugin-sdk", "react", "react/jsx-runtime"]
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", SOURCE), "theme variables only"
    for banned in (r"\beval\(", r"new Function", r"\bimport\(", r"\bdocument\.", r"innerHTML", r"hermesDesktop", r"localStorage",
                   r"(?<![A-Za-z.])fetch\(", r"XMLHttpRequest", r"WebSocket", r"data-slot", r"querySelector"):
        assert not re.search(banned, SOURCE), banned
    assert "prefers-reduced-motion: reduce" in SOURCE


def test_every_sentence_about_the_plan_comes_from_the_backend():
    # The page lays out what views.py and clashes.py wrote. A copy of their wording here would drift.
    for phrase in ("Clashes with", "is also planned on", "allows one card at a time", "They overlap for", "Starts in",
                   "Running for", "over its estimate.", "Released ", "The timer", "Missed its slot", "Planned by",
                   "Next:", "more slot", "is parked", "needs a look", "The Kanban starts it"):
        assert phrase not in SOURCE, phrase
    assert clashes.describe.__doc__ and "over its estimate" in SOURCE     # only as the short mark on a bar's label


def test_it_registers_a_page_a_sidebar_row_a_chip_and_a_palette_command(run):
    out, _view, _ids = run
    assert (out["id"], out["name"]) == ("chronomancer", "Chronomancer")
    assert out["contributions"] == [
        {"id": "page", "area": "routes", "data": {"path": "/chronomancer"}, "render": True},
        {"id": "nav", "area": "sidebar.nav", "data": {"path": "/chronomancer", "label": "Chronomancer", "codicon": "wand"}, "render": False},
        {"id": "chip", "area": "statusBar.right", "data": None, "render": True},
        {"id": "open", "area": "palette", "data": {"id": "chronomancer.open", "label": "Open Chronomancer",
                                                   "keywords": ["plan", "slot", "kanban", "timeline", "schedule", "clash"]}, "render": False}]
    assert out["prefetch"] == ["/plan?horizon=2d"] and out["missingKeys"] == []
    assert out["navigations"] == ["/chronomancer", "/chronomancer"]            # the chip and the palette command


# ---- the page ----

def test_the_header_says_what_the_backend_says(run):
    out, view, _ids = run
    wide = out["wide"]
    assert wide["headline"] == view["summary"]["headline"] == 'Next: "Rotate the database credentials" (keeper) in 8 min, at 10:15.'
    assert wide["facts"] == [[f["kind"], f["text"]] for f in view["summary"]["facts"]]
    assert wide["horizon"] == [["Day", False], ["2 days", True], ["Week", False]]
    assert wide["style"] == 1 and view["later"]["text"] in wide["later"]


def test_the_loom_has_a_lane_per_profile_and_a_bar_per_slot(run):
    out, view, _ids = run
    wide = out["wide"]
    assert wide["lanes"] == ["builder", "keeper", "scribe"]
    # lanes take the theme's hues in order; red is never a lane, it is the clash colour
    assert wide["laneColors"] == [f"var(--ui-{hue}, var(--ui-accent))" for hue in ("purple", "cyan", "orange")]
    assert "--ui-red" not in SOURCE.split("const HUES")[1].split("]")[0]
    assert len(wide["bars"]) == len(view["items"]) == 10 and {b["type"] for b in wide["bars"]} == {"button"}
    by_title = {b["label"].split(",")[0]: b for b in wide["bars"]}
    assert by_title["Rebuild the search index"]["label"] == (
        "Rebuild the search index, running, Wed 11 Nov 08:50 to 10:07, busy, over its estimate")
    assert by_title["Roll out web v2 to production"]["label"].endswith("pinned by you, clash")
    assert by_title["Rebuild the search index"]["title"].splitlines()[2] == "Running for 1 h 17 min, 17 min over its estimate."
    assert (wide["needle"], wide["nowLabel"], wide["days"]) == (1, ["now 10:07"], ["Wed 11 Nov", "Thu 12 Nov", "Fri 13 Nov"])
    assert wide["nights"] == len(view["horizon"]["nights"]) == 4


def test_state_is_never_colour_alone(run):
    out, view, _ids = run
    bars = {b["label"].split(",")[0]: b for b in out["wide"]["bars"]}
    for item in view["items"]:
        bar = bars[item["title"]]
        assert bar["phase"] == item["phase"] and bar["clash"] == ("1" if item["clash"] else "0")
        assert bar["hatch"] == sum(1 for c in item["clashes"] if c["kind"] == "clash")      # a pattern on the shared time
        assert bar["over"] == (1 if item["overrun_s"] else 0)
        assert bar["move"] == ("1" if item["can"]["move"] else "0")
    # no two bars of a lane share pixels in one row
    rows = {}
    for bar in out["wide"]["bars"]:
        rows.setdefault(bar["top"], []).append((bar["left"], bar["left"] + bar["width"]))
    for spans in rows.values():
        spans.sort()
        assert all(a[1] <= b[0] for a, b in zip(spans, spans[1:]))


def test_the_tray_and_the_chronicle(run):
    out, view, _ids = run
    wide = out["wide"]
    assert wide["tray"] == [{"title": "Tune the autoscaler limits", "status": "blocked"},
                            {"title": "Clean up old preview deployments", "status": "ready"},
                            {"title": "Archive last quarter's logs", "status": "scheduled"},
                            {"title": "Sketch the status page redesign", "status": "triage"}]
    moves = [e["text"] for e in view["log"] if not e["quiet"]]
    assert [e["text"] for e in wide["chronicle"]] == moves[:12] and len(moves) > 12       # the newest moves first
    assert wide["chronicle"][0] == {"text": 'The owner pinned "Roll out web v2 to production" at Wed 11 Nov 11:30.', "owner": "1", "icon": "account"}
    assert {e["icon"] for e in wide["chronicle"]} == {"account", "watch", "sparkle"}       # you, the timer, an agent
    assert wide["chronicleMore"] == [f"Show all {len(view['log'])} changes, labels included"]
    assert out["chronicleAll"] == {"entries": len(view["log"]), "toggle": ["Show fewer"]}
    assert wide["scroll"] == 0                                                              # nothing is selected yet


def test_a_selected_slot_opens_its_scroll_with_the_backend_sentences(run):
    out, view, ids = run
    chosen = out["selected"]
    item = next(i for i in view["items"] if i["task_id"] == chosen["id"])
    assert chosen["id"] == ids["rotate"] and chosen["title"] == "Rotate the database credentials"
    assert chosen["clashes"] == [{"kind": c["kind"], "text": c["text"]} for c in item["clashes"]]
    for text in (item["line"], item["set_by_text"], f"{item['when']} · {item['estimate']}"):
        assert text in chosen["texts"]
    assert chosen["buttons"] == ["Next free start: Wed 11 Nov 10:45", "Move", "Run now", "15 min earlier", "15 min later", "Pin", "Unplan", "Add"]
    assert chosen["inputs"] == [["datetime-local", "2026-11-11T10:15"], ["number", "45"], ["text", ""]]   # plain inputs: this SDK has no Input
    assert chosen["partners"] == ["Upgrade the ingress controller to 1.12"] and chosen["pressed"] == 1


# ---- the moves ----

def test_each_move_is_one_request_and_the_backend_words_are_shown(run):
    out, view, ids = run
    card = ids["rotate"]
    start = next(i for i in view["items"] if i["task_id"] == card)["planned_start"]
    free = next(i for i in view["items"] if i["task_id"] == card)["next_free"]
    assert out["moves"] == [
        {"path": f"/cards/{card}/slot", "method": "POST", "body": {"start": start + 900}},
        {"path": f"/cards/{card}/slot", "method": "POST", "body": {"start": free}},
        {"path": f"/cards/{card}/pin", "method": "POST", "body": {"pinned": True}},
        {"path": f"/cards/{card}/slot", "method": "DELETE", "body": None},
        {"path": f"/cards/{card}/slot", "method": "POST", "body": {"start": "2030-01-02T09:30", "estimate_minutes": 75}},
        {"path": f"/cards/{card}/labels", "method": "POST", "body": {"add": ["op:upgrade"], "remove": []}}]
    assert out["notices"][0] == {"kind": "success", "message": f"done /cards/{card}/slot"} and len(out["notices"]) == 6
    assert out["invalidated"] == 6                                              # the plan is read again after every move


def test_cast_now_asks_first(run):
    out, _view, ids = run
    assert out["dialog"] == {"title": "Run this card now?", "confirmLabel": "Run now", "open": True,
                             "description": '"Rotate the database credentials" is released at once and its slot moves to now.'}
    assert out["dialogClosed"] is True
    assert out["cast"] == [{"path": f"/cards/{ids['rotate']}/now", "method": "POST", "body": None}]


def test_a_refused_move_shows_the_reason(run):
    out, _view, _ids = run
    assert out["refused"] == [{"kind": "error", "message": "The owner pinned this slot."},
                              {"kind": "error", "message": "The move failed."}]


def test_arrow_keys_move_the_focused_slot(run):
    out, view, ids = run
    start = next(i for i in view["items"] if i["task_id"] == ids["rotate"])["planned_start"]
    assert out["keyboard"] == {"prevented": 1, "calls": [
        {"path": f"/cards/{ids['rotate']}/slot", "method": "POST", "body": {"start": start + 900}}]}


def test_dragging_a_bar_moves_its_slot_by_quarter_hours(run):
    out, _view, _ids = run
    assert out["dragTiny"] == {"calls": 0, "listeners": 0}                      # a few pixels are a click
    assert out["dragDuring"] == {"guide": ["Move to Thu 15:00"], "dragging": 1}
    drag = out["drag"]
    assert drag["calls"] == [{"path": f"/cards/{drag['id']}/slot", "method": "POST", "body": {"start": drag["expected"]}}]
    assert drag["listeners"] == 0 and drag["expected"] % 900 == 0
    assert out["dragRunning"] == 0                                              # a running card has no start to move


def test_dragging_a_tray_card_onto_the_loom_plans_it(run):
    out, _view, ids = run
    assert out["dropOutside"] == 0 and out["dropTriage"] == 0
    assert out["dropDuring"] == ["Plan at Fri 04:00"]
    drop = out["drop"]
    assert drop["id"] == ids["autoscaler"]
    assert drop["calls"] == [{"path": f"/cards/{drop['id']}/slot", "method": "POST", "body": {"start": drop["near"]}}]
    assert out["trayScroll"] == {"buttons": ["Plan", "Run now", "Add"], "inputs": ["2026-11-11T11:00", "60", ""]}


# ---- horizon, filters, narrow ----

def test_the_horizon_switch_reads_another_view_and_is_remembered(run):
    out, _view, _ids = run
    assert out["horizon"] == {"keys": ['["chronomancer","plan","2d"]', '["chronomancer","plan","week"]'],
                              "stored": {"horizon": "week"}, "poll": [5000]}


def test_a_long_tray_is_a_scroll_box_with_a_filter_field(run):
    out, view, _ids = run
    few = len(view["tray"])
    assert few <= 8 and out["shortTray"] == {"field": 0, "box": 1, "rows": few, "badge": str(few)}   # the demo: no field
    assert out["longTray"] == {"field": 1, "box": 1, "rows": 12, "badge": "12", "name": "Filter the cards"}
    assert out["longTrayWords"] == {"field": 1, "box": 1, "rows": 4, "badge": "4 / 12"}     # any case, parts of words
    assert out["longTrayEvery"] == ["Card 9"]                                                # every word must match
    assert out["longTrayId"] == ["Card 7"] and out["longTrayLabel"] == ["Card 5"]            # by card id, by label
    assert out["longTrayNone"] == {"field": 1, "box": 0, "rows": 0, "badge": "0 / 12", "said": True}
    assert out["longTrayCleared"] == 12
    assert "overflow-y: auto" in SOURCE.split("\n.chm-tray {")[1].split("}")[0]              # its own rule: a scroll box


def test_move_waits_for_a_change_and_run_now_says_what_it_does(run):
    out, _view, _ids = run
    assert out["moveButton"] == {"same": True, "estimate": False, "back": True, "start": False}
    assert "Release this card at once. Its slot moves to now." in SOURCE and "'Cast now'" not in SOURCE


def test_lanes_show_a_working_agent_and_every_listed_profile(run):
    out, view, _ids = run
    assert out["busyLanes"] == [["builder", "builder works on a card now"]]          # two of its cards run
    assert out["rosterOff"] == {"enabled": True, "names": ["plain-name", "by-id"]}   # names from objects or strings
    known = [lane["label"] for lane in view["lanes"]]
    assert out["roster"] == {"lanes": known + ["herald"], "chip": True}              # builder is not doubled
    assert "transition: opacity 0.15s ease, top 0.26s" in SOURCE                      # a bar glides to its new row


def test_filters(run):
    out, _view, _ids = run
    assert out["filterProfile"] == {"lanes": ["builder", "keeper"], "tray": 3}
    assert sorted(out["filterLabel"]) == ["Audit access to the main database", "Back up the shop database before the migration",
                                          "Migrate the orders table", "Rotate the database credentials"]
    assert out["filterClashes"] == 6 and out["clear"] is True


def test_narrow_is_a_list_without_dragging(run):
    out, _view, _ids = run
    assert out["narrow"] == {"loom": 0, "rows": 10, "days": ["Wed 11 Nov", "Thu 12 Nov"], "hint": False}


# ---- the edges ----

def test_loading_errors_and_an_empty_plan(run):
    out, _view, _ids = run
    assert out["loading"] == ["Reading the plan…"]
    assert out["loadError"] == [{"title": "The plan is unavailable", "description": "Plugin not found"}]
    assert out["stale"] == ["Could not refresh. Showing the last plan received."]
    assert out["boardDown"] == ["The Kanban board cannot be read: kanban_list failed (RuntimeError)."]
    empty = out["empty"]
    assert empty["bars"] == 0
    for text in ("Nothing is planned.", "Nothing is planned in this horizon.", "Every open card has a slot.", "No changes yet."):
        assert text in empty["texts"]


def test_kanban_cards_get_the_badge_the_backend_wrote(run):
    out, view, ids = run
    badges = out["badges"]
    assert (badges["area"], badges["registrations"], badges["disposers"], badges["unknown"]) == ("kanban.card.badges", 1, 1, None)
    assert badges["shown"] == view["badges"]
    assert badges["shown"][ids["rotate"]]["label"] == "clash Wed 10:15" and badges["shown"][ids["notes"]]["label"] == "slot Wed 13:00"
    assert out["badgesAfter"] == {"registrations": 2, "gone": None}            # a changed plan is painted again


def test_a_change_announced_by_the_backend_reads_the_plan_again(run):
    out, _view, _ids = run
    assert out["event"] == {"names": ["plugin.chronomancer.plan.changed"], "invalidated": 1}


def test_the_status_bar_chip(run):
    out, view, _ids = run
    assert out["chip"] == {"tip": f"Chronomancer. {view['summary']['tip']}", "text": "next 10:15 · 3 clashes · 1 needs a look", "icon": "zap"}
    assert out["chipLoading"] is None
