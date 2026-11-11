"""A stand-in for the Hermes kanban tools, with the answers and the status rules of the real ones.

``dev/contract_check.py`` runs the same moves against a real Hermes core, so this file cannot
drift from it unnoticed.
"""
import json
from collections import defaultdict

OPEN = ("triage", "todo", "scheduled", "ready", "running", "blocked", "review")


class FakeKanban:
    def __init__(self):
        self.tasks = {}
        self.parents = defaultdict(list)
        self.comments = defaultdict(list)
        self.calls = []
        self.down = False          # True: every call raises, as when the tools are not loaded
        self.refuse = set()        # tool names that answer with an error

    def add(self, task_id, title=None, status="ready", assignee="default", parents=(), **fields):
        self.tasks[task_id] = {"id": task_id, "title": title or f"Card {task_id}", "status": status,
                               "assignee": assignee, "priority": 0, "tenant": None, "project_id": None,
                               "model_override": None, "created_at": 1000, "started_at": None,
                               "completed_at": None, **fields}
        self.parents[task_id] = list(parents)
        return task_id

    def status(self, task_id):
        return self.tasks[task_id]["status"]

    def set(self, task_id, **fields):
        self.tasks[task_id].update(fields)

    def count(self, tool):
        return sum(1 for name, _ in self.calls if name == tool)

    def __call__(self, tool, args):
        self.calls.append((tool, dict(args)))
        if self.down:
            raise RuntimeError("Unknown tool")
        if tool in self.refuse:
            return json.dumps({"error": f"{tool} refused"})
        return json.dumps(getattr(self, "_" + tool)(args))

    def _kanban_list(self, args):
        limit = int(args.get("limit") or 50)
        rows = [dict(task, parents=self.parents[tid]) for tid, task in self.tasks.items()
                if task["status"] != "archived" and (not args.get("status") or task["status"] == args["status"])]
        return {"tasks": rows[:limit], "count": len(rows[:limit]), "limit": limit, "truncated": len(rows) > limit}

    def _kanban_show(self, args):
        task = self.tasks.get(args.get("task_id"))
        if task is None:
            return {"error": f"task {args.get('task_id')} not found"}
        return {"task": dict(task), "parents": self.parents[task["id"]],
                "comments": [{"author": "x", "body": body, "created_at": 0} for body in self.comments[task["id"]]]}

    def _kanban_schedule(self, args):
        task = self.tasks.get(args.get("task_id"))
        if task is None or task["status"] not in ("todo", "ready", "running", "blocked"):
            return {"error": f"could not schedule {args.get('task_id')} (unknown id or not in todo/ready/running/blocked)"}
        task["status"], task["reason"] = "scheduled", args.get("reason")
        return {"ok": True, "task_id": task["id"], "run_id": None, "status": "scheduled", "reason": args.get("reason")}

    def _kanban_unblock(self, args):
        task = self.tasks.get(args.get("task_id"))
        if task is None or task["status"] not in ("blocked", "scheduled"):
            return {"error": f"could not unblock {args.get('task_id')} (not blocked or unknown)"}
        waiting = any(self.tasks.get(p, {}).get("status") != "done" for p in self.parents[task["id"]])
        task["status"] = "todo" if waiting else "ready"
        return {"ok": True, "task_id": task["id"], "status": task["status"]}

    def _kanban_comment(self, args):
        if args.get("task_id") not in self.tasks:
            return {"error": f"task {args.get('task_id')} not found"}
        self.comments[args["task_id"]].append(args.get("body"))
        return {"ok": True, "task_id": args["task_id"]}
