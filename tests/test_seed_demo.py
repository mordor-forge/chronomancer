"""The demo scenario: the plan it leaves behind is the one the README and the video show."""
import importlib.util
import json

import pytest

from chronomancer import views
from conftest import NOW, ROOT, at

spec = importlib.util.spec_from_file_location("seed_demo", ROOT / "dev" / "seed_demo.py")
seed_demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed_demo)


class FakeStage(seed_demo.Stage):
    def __init__(self, kanban):
        self.kanban = kanban

    def create(self, title, assignee, **extra):
        return json.loads(self.kanban("kanban_create", {"title": title, "assignee": assignee, **extra}))["task_id"]

    def park(self, task_id):
        self.kanban("kanban_schedule", {"task_id": task_id, "reason": "waits"})

    def running(self, task_id, started_at):
        self.kanban.set(task_id, status="running", started_at=started_at)

    def done(self, task_id, started_at, completed_at):
        self.kanban.set(task_id, status="done", started_at=started_at, completed_at=completed_at)


@pytest.fixture
def seeded(planner, kanban, clock):
    clock.now = NOW + 7 * 60                                         # 10:07: the scenario rounds to 10:00
    ids = seed_demo.scenario(FakeStage(kanban), planner, clock, clock.now)
    return ids, planner


def test_the_scenario_leaves_every_kind_of_card(seeded, kanban, clock):
    ids, planner = seeded
    assert clock.now == NOW + 7 * 60
    snap = planner.snapshot()
    phases = {key: snap.item(task_id).phase for key, task_id in ids.items()}
    assert phases == {
        "backup": "done", "nightly": "missed", "index": "running", "ingress": "running", "rotate": "planned",
        "rollout": "planned", "firewall": "planned", "notes": "planned", "orders": "planned", "access": "planned",
        "review": "planned", "lockfiles": "planned", "certs": "planned",
        "autoscaler": "unplanned", "previews": "unplanned", "logs": "unplanned", "status-page": "unplanned"}
    statuses = {key: kanban.status(task_id) for key, task_id in ids.items()}
    assert (statuses["autoscaler"], statuses["previews"], statuses["logs"], statuses["status-page"]) == (
        "blocked", "ready", "scheduled", "triage")
    assert statuses["rotate"] == "scheduled"                         # planned cards are parked
    assert snap.item(ids["index"]).overrun_s == 17 * 60 - 15         # started 08:50:15, one hour planned
    assert snap.item(ids["rollout"]).slot.pinned and snap.item(ids["rollout"]).slot.start_ts == at(11, 30)


def test_the_scenario_has_three_clashes_and_one_busy_pair(seeded):
    ids, planner = seeded
    name = {task_id: key for key, task_id in ids.items()}
    snap = planner.snapshot()
    pairs = sorted((c.kind, *sorted((name[c.a.task_id], name[c.b.task_id])), c.labels) for c in snap.clashes)
    assert pairs == [("busy", "index", "ingress", ()),
                     ("clash", "access", "orders", ("db:main",)),
                     ("clash", "firewall", "rollout", ("cluster:prod",)),
                     ("clash", "ingress", "rotate", ("cluster:prod",))]


def test_the_scenario_log_tells_the_story_in_order(seeded):
    ids, planner = seeded
    texts = [views.log_text(planner, e) for e in reversed(planner.store.log_tail(200))]
    story = [t for t in texts if "label" not in t]
    assert story[0] == 'Profile orchestrator planned "Rebuild the nightly base images" for Wed 11 Nov 04:00 (20 min).'
    assert story[4] == 'Profile orchestrator planned "Rotate the database credentials" for Wed 11 Nov 10:15 (45 min), with 1 clash.'
    assert story[-7:] == [
        '"Rebuild the nightly base images" missed its slot at Wed 11 Nov 04:00: the timer was not running.',
        'The timer released "Back up the shop database before the migration" at its slot.',
        'The timer released "Rebuild the search index" at its slot.',
        'The timer released "Upgrade the ingress controller to 1.12" at its slot.',
        'The owner moved "Write the release notes for web v2" from Wed 11 Nov 12:30 to Wed 11 Nov 13:00.',
        'The owner moved "Roll out web v2 to production" from Wed 11 Nov 11:45 to Wed 11 Nov 11:30, with 1 clash.',
        'The owner pinned "Roll out web v2 to production" at Wed 11 Nov 11:30.']


def test_the_page_for_the_scenario(seeded):
    ids, planner = seeded
    view = views.owner_view(planner, horizon_key="2d")
    assert [lane["label"] for lane in view["lanes"]] == ["builder", "keeper", "scribe"]
    assert view["counts"] == {"clashes": 3, "busy": 1, "planned": 7, "running": 2, "attention": 1, "tray": 4}
    assert view["later"]["count"] == 2 and view["later"]["next_title"] == "Refresh the dependency lockfiles"
    assert len(view["items"]) == 10 and len(view["tray"]) == 4       # the backup ran before this horizon starts
    week = views.owner_view(planner, horizon_key="week")
    assert len(week["items"]) == 13 and week["later"]["count"] == 0
