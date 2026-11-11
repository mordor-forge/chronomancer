"""Run the planner against the real Hermes kanban tools, in a scratch environment.

    CW_HERMES_CORE=<hermes-agent checkout> dev/scratch-env.sh python dev/contract_check.py

The unit tests use ``tests/fake_kanban.py``. This script makes the same moves on a real board, so
the fake cannot drift from the core unnoticed. It refuses to run outside ``dev/scratch-env.sh``:
it creates cards.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

kanban_db = os.environ.get("HERMES_KANBAN_DB", "")
if os.environ.get("HERMES_KANBAN_BOARD") != "scratch" or "/kanban/kanban.db" not in kanban_db:
    sys.exit("contract_check: run me through dev/scratch-env.sh (no scratch board in the environment)")

try:
    import hermes_bootstrap  # noqa: F401  (the installed core sets its import paths here)
except ImportError:
    pass
import model_tools  # noqa: F401,E402  (loads the tool registry, as the gateway does)
from tools.registry import registry  # noqa: E402

from chronomancer.board import Board  # noqa: E402
from chronomancer.planner import OWNER, Planner  # noqa: E402
from chronomancer.store import PLANNED, RELEASED, Store  # noqa: E402

results: list[tuple[bool, str]] = []


def check(ok: bool, what: str) -> None:
    results.append((bool(ok), what))
    print(("PASS  " if ok else "FAIL  ") + what)


def dispatch(tool: str, args: dict) -> str:
    return registry.dispatch(tool, args)


def tool(name: str, **args) -> dict:
    out = dispatch(name, args)
    return out if isinstance(out, dict) else json.loads(out)


class Clock:
    def __init__(self) -> None:
        import time
        self.now = int(time.time())

    def __call__(self) -> int:
        return self.now


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="chrono-contract-"))
    clock = Clock()
    store = Store(lambda: sqlite3.connect(work / "plan.db", timeout=5))
    planner = Planner(store, Board(dispatch, list_ttl_s=0, card_ttl_s=0), setting=lambda key, default=None: default,
                      tz=lambda: None, clock=clock)

    def create(title: str, **extra) -> str:
        made = tool("kanban_create", title=title, assignee="builder", **extra)
        if "task_id" not in made:
            sys.exit(f"contract_check: kanban_create failed: {made}")
        return made["task_id"]

    def status(task_id: str) -> str:
        return tool("kanban_show", task_id=task_id)["task"]["status"]

    ready = create("Contract: a ready card")
    blocked = create("Contract: a blocked card", initial_status="blocked")
    parent = create("Contract: an open parent", initial_status="blocked")
    child = create("Contract: a child that waits", parents=[parent])
    check(status(ready) == "ready" and status(blocked) == "blocked" and status(child) == "todo",
          f"new cards land as ready / blocked / todo ({status(ready)}, {status(blocked)}, {status(child)})")

    listed = {c.task_id: c for c in planner.board.cards("")}
    check({ready, blocked, parent, child} <= set(listed) and listed[ready].assignee == "builder",
          f"kanban_list by status returns every open card with its assignee ({len(listed)} cards)")
    check(planner.board.card("", "t_00000000") is None, "kanban_show answers an unknown id as 'not found'")

    start = clock.now + 3600
    planner.set_slot(ready, start, actor=OWNER)
    check(status(ready) == "scheduled", f"a slot parks a ready card (kanban_schedule): {status(ready)}")
    planner.set_slot(child, start, actor=OWNER)
    check(status(child) == "scheduled", f"a slot parks a todo card: {status(child)}")
    planner.set_slot(blocked, start, actor=OWNER)
    check(status(blocked) == "blocked", f"a slot leaves a blocked card: {status(blocked)}")

    clock.now = start
    done: list = []
    thread = threading.Thread(target=lambda: done.extend(planner.run_due()))  # a bare thread, as the timer is
    thread.start()
    thread.join(60)
    check(len(done) == 3 and all(store.get_slot("", t).state == RELEASED for t in (ready, child, blocked)),
          f"the timer pass releases the three due slots from a bare thread ({len(done)} released)")
    check(status(ready) == "ready", f"a released parked card is ready: {status(ready)}")
    check(status(child) == "todo", f"a released child with an open parent is todo: {status(child)}")
    check(status(blocked) in ("ready", "review"), f"a released blocked card resumes: {status(blocked)}")
    comments = [c["body"] for c in tool("kanban_show", task_id=ready).get("comments", [])]
    check(any(c.startswith("chronomancer: released at its slot") for c in comments), "the release comment is on the card")

    planner.set_slot(ready, clock.now + 7200, actor=OWNER)
    check(status(ready) == "scheduled" and store.get_slot("", ready).state == PLANNED, "a released card is planned and parked again")
    change = planner.run_now(ready, actor=OWNER)
    check(status(ready) == "ready" and change.released == "ready", "run now releases it at once")

    snap = planner.snapshot()
    check(not snap.board_error and {i.task_id for i in snap.items} >= {ready, blocked, parent, child},
          f"the snapshot reads the board ({len(snap.items)} cards, error: {snap.board_error or 'none'})")

    failed = [what for ok, what in results if not ok]
    print(f"\n{len(results) - len(failed)} of {len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
