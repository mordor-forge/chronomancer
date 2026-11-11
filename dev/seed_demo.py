"""Fill a throwaway Kanban board and its plan with an invented team's work, for demos and screenshots.

    CW_HERMES_CORE=<hermes-agent checkout> dev/scratch-env.sh python dev/seed_demo.py \\
        --plan-db <plugin data dir>/plan.db

The data is fiction: three profiles (builder, keeper, scribe) and an orchestrator that planned
their week. Every time is relative to now, so the plan always looks alive: one card is done, two
run, two pairs of slots clash, one slot was missed in the night, and a few cards wait unplanned.

The script replays the last eight hours with a moving clock. The plan is written by the plugin's
own planner through the real kanban tools, so the board and the change log are what the plugin
really produces. Two things have no public tool and are written to the board's database directly,
which is why this is a demo script and nothing the plugin does: the past start and end times of
the cards that run or are done.

It refuses a board that already has cards, and a board that is not named by HERMES_KANBAN_DB.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from chronomancer.board import Board  # noqa: E402
from chronomancer.planner import OWNER, Planner  # noqa: E402
from chronomancer.store import Store  # noqa: E402
from chronomancer.timezones import local_tz  # noqa: E402

AGENT = "agent:orchestrator"
M, H, D = 60, 3600, 86400

LABELS = (
    ("cluster:prod", True, "The production cluster"),
    ("db:main", True, "The main database"),
    ("repo:web", False, "The web application"),
    ("repo:infra", False, "The infrastructure repository"),
    ("op:upgrade", False, ""),
    ("op:backup", False, ""),
    ("project:shop", False, "The shop relaunch"),
)

# key, title, profile, labels
CARDS = (
    ("backup", "Back up the shop database before the migration", "keeper", ("db:main", "op:backup", "project:shop")),
    ("nightly", "Rebuild the nightly base images", "builder", ("repo:infra",)),
    ("index", "Rebuild the search index", "builder", ("repo:web",)),
    ("ingress", "Upgrade the ingress controller to 1.12", "builder", ("cluster:prod", "op:upgrade", "repo:infra")),
    ("rotate", "Rotate the database credentials", "keeper", ("cluster:prod", "db:main")),
    ("rollout", "Roll out web v2 to production", "builder", ("cluster:prod", "repo:web", "project:shop")),
    ("firewall", "Review the firewall rules", "keeper", ("cluster:prod",)),
    ("notes", "Write the release notes for web v2", "scribe", ("repo:web", "project:shop")),
    ("orders", "Migrate the orders table", "builder", ("db:main", "project:shop")),
    ("access", "Audit access to the main database", "keeper", ("db:main",)),
    ("review", "Draft the monthly incident review", "scribe", ()),
    ("lockfiles", "Refresh the dependency lockfiles", "builder", ("repo:web", "repo:infra")),
    ("certs", "Renew the TLS certificates", "keeper", ("cluster:prod",)),
    ("failover", "Rehearse the database failover", "builder", ("db:main", "cluster:prod")),
    ("changelog", "Publish the changelog for web v2", "scribe", ("repo:web", "project:shop")),
    ("keys", "Rotate the backup encryption keys", "keeper", ("op:backup",)),
    # no slot: these wait in the tray
    ("autoscaler", "Tune the autoscaler limits", "builder", ("cluster:prod",)),
    ("previews", "Clean up old preview deployments", "builder", ("repo:web",)),
    ("logs", "Archive last quarter's logs", "keeper", ()),
    ("status-page", "Sketch the status page redesign", "scribe", ()),
)


class Stage:
    """What the scenario needs from a board beyond the plugin's own moves."""

    def create(self, title: str, assignee: str, **extra: Any) -> str:
        raise NotImplementedError

    def park(self, task_id: str) -> None:
        raise NotImplementedError

    def running(self, task_id: str, started_at: int) -> None:
        raise NotImplementedError

    def done(self, task_id: str, started_at: int, completed_at: int) -> None:
        raise NotImplementedError


def scenario(stage: Stage, planner: Planner, clock: Any, now: int) -> dict[str, str]:
    """Replay the last eight hours on ``stage``. ``clock.now`` is moved; it ends at ``now``."""
    t = now - now % (15 * M)                       # the last quarter hour: neat times on the page
    day0 = datetime.fromtimestamp(t, planner.tz()).replace(hour=0, minute=0, second=0, microsecond=0)

    def day(offset: int, hour: int, minute: int = 0) -> int:
        return int((day0 + timedelta(days=offset)).replace(hour=hour, minute=minute).timestamp())

    clock.now = t - 8 * H
    ids: dict[str, str] = {}
    for key, title, profile, _labels in CARDS:
        extra: dict[str, Any] = {}
        if key == "autoscaler":
            extra["initial_status"] = "blocked"
        if key == "status-page":
            extra["triage"] = True
        ids[key] = stage.create(title, profile, **extra)
    stage.park(ids["logs"])
    for name, exclusive, description in LABELS:
        planner.label(name, exclusive=exclusive, description=description or None, actor=AGENT)
    for key, _title, _profile, labels in CARDS:
        if labels:
            planner.tag(ids[key], add=labels, actor=AGENT)

    def plan(key: str, start: int, minutes: int, actor: str = AGENT) -> None:
        planner.set_slot(ids[key], start, estimate_s=minutes * M, actor=actor)

    plan("nightly", t - 6 * H, 20)
    plan("backup", t - 3 * H, 30)
    plan("index", t - 70 * M, 60)
    plan("ingress", t - 50 * M, 90)
    plan("rotate", t + 15 * M, 45)                 # clashes with the ingress upgrade: both on cluster:prod
    plan("rollout", t + 105 * M, 90)
    plan("firewall", t + 2 * H, 60)                # clashes with the rollout: both on cluster:prod
    plan("notes", t + 150 * M, 45)
    plan("orders", day(1, 9), 120)
    plan("access", day(1, 10), 60)                 # clashes with the migration: both on db:main
    plan("review", day(1, 14), 90)
    plan("lockfiles", day(2, 10), 45)
    plan("certs", day(3, 9), 30)
    plan("failover", day(4, 10), 180)
    plan("changelog", day(4, 15), 60)
    plan("keys", day(5, 11), 45)

    clock.now = t - 5 * H                          # the gateway was down in the night: the image rebuild is missed
    planner.run_due()
    clock.now = t - 3 * H
    planner.run_due()
    stage.done(ids["backup"], t - 3 * H + 20, t - 3 * H + 25 * M)
    clock.now = t - 70 * M
    planner.run_due()
    stage.running(ids["index"], t - 70 * M + 15)
    clock.now = t - 50 * M
    planner.run_due()
    stage.running(ids["ingress"], t - 50 * M + 10)
    clock.now = t - 25 * M                         # the owner looks at the plan and changes two things
    plan("notes", t + 3 * H, 45, actor=OWNER)
    plan("rollout", t + 90 * M, 90, actor=OWNER)
    planner.pin(ids["rollout"], True, actor=OWNER)
    clock.now = now
    planner.board.forget()
    return ids


# ---- a real board, through the kanban tools of a Hermes environment ----

class RealStage(Stage):
    def __init__(self, dispatch: Callable[[str, dict], Any], kanban_db: Path):
        self.dispatch, self.kanban_db = dispatch, kanban_db

    def _tool(self, name: str, **args: Any) -> dict[str, Any]:
        out = self.dispatch(name, args)
        out = out if isinstance(out, dict) else json.loads(out)
        if "error" in out:
            sys.exit(f"seed_demo: {name} failed: {out['error']}")
        return out

    def create(self, title: str, assignee: str, **extra: Any) -> str:
        return self._tool("kanban_create", title=title, assignee=assignee, **extra)["task_id"]

    def park(self, task_id: str) -> None:
        self._tool("kanban_schedule", task_id=task_id, reason="waits for a quiet week")

    def _write(self, sql: str, params: tuple) -> None:
        with sqlite3.connect(self.kanban_db, timeout=10) as conn:
            if conn.execute(sql, params).rowcount != 1:
                sys.exit("seed_demo: the board did not take a staged state")

    def running(self, task_id: str, started_at: int) -> None:
        self._write("UPDATE tasks SET status = 'running', started_at = ? WHERE id = ?", (started_at, task_id))

    def done(self, task_id: str, started_at: int, completed_at: int) -> None:
        self._write("UPDATE tasks SET status = 'done', started_at = ?, completed_at = ? WHERE id = ?",
                    (started_at, completed_at, task_id))


class Clock:
    def __init__(self, now: int):
        self.now = now

    def __call__(self) -> int:
        return self.now


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--plan-db", required=True, type=Path, help="the plugin's plan.db (in its plugin data directory)")
    parser.add_argument("--timezone", default="", help="IANA zone for the times in the log (default: the system zone)")
    parser.add_argument("--board", default="", help="board slug (default: the active board)")
    args = parser.parse_args()

    kanban_db = os.environ.get("HERMES_KANBAN_DB", "")
    if not kanban_db:
        sys.exit("seed_demo: set HERMES_KANBAN_DB to the demo board's database (dev/scratch-env.sh does). "
                 "This script writes cards and never guesses which board you mean.")
    import tools.kanban_tools  # noqa: F401  (registers kanban_* in the tool registry; run with a Hermes core's Python)
    from tools.registry import registry

    def dispatch(tool: str, tool_args: dict) -> Any:
        return registry.dispatch(tool, {**tool_args, **({"board": args.board} if args.board else {})})

    listed = dispatch("kanban_list", {"limit": 5})
    listed = listed if isinstance(listed, dict) else json.loads(listed)
    if "error" in listed:
        sys.exit(f"seed_demo: the board cannot be read: {listed['error']} (run `hermes kanban init` first)")
    if listed.get("tasks"):
        sys.exit("seed_demo: this board already has cards. Seed an empty demo board only.")

    import time
    now = int(time.time())
    clock = Clock(now)
    zone = local_tz(args.timezone or None)
    args.plan_db.parent.mkdir(parents=True, exist_ok=True)
    store = Store(lambda: sqlite3.connect(args.plan_db, timeout=10))
    settings = {"board": args.board}
    planner = Planner(store, Board(dispatch, list_ttl_s=0, card_ttl_s=0), tz=lambda: zone, clock=clock,
                      setting=lambda key, default=None: settings.get(key, default))
    ids = scenario(RealStage(dispatch, Path(kanban_db)), planner, clock, now)
    snap = planner.snapshot()
    print(f"seeded {len(ids)} cards, {len(store.slots(args.board))} slots, {len(store.labels())} labels, "
          f"{len(store.log_tail(500))} log entries; clashes now: {sum(1 for c in snap.clashes if c.kind == 'clash')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
