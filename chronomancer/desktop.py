"""The owner's surface in Hermes Desktop: the logic behind ``dashboard/plugin_api.py``.

No web framework here, so it is tested without Hermes or FastAPI. Every method returns
``(HTTP status, JSON body)``.

Trust: Hermes authenticates the request (the Desktop session, or a signed-in dashboard session)
before a route runs, and this code trusts that: every move made here is recorded as the owner's.
Code that runs as the same OS user can reach the same local API. That is why a pin is a courtesy
between the owner and well-behaved agents, not a lock.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from . import views
from .board import BoardError
from .planner import OWNER, PlanError, Planner
from .tools import parse_time

logger = logging.getLogger(__name__)

Result = tuple[int, dict[str, Any]]


def _error(status: int, message: str) -> Result:
    return status, {"detail": message}  # the key FastAPI and the Desktop client use for errors


class DesktopApi:
    def __init__(self, planner: Planner, *, refresh: Callable[[], None] = lambda: None):
        self.planner, self.refresh = planner, refresh

    def _run(self, call: Callable[[], dict[str, Any]]) -> Result:
        try:
            self.refresh()
            return 200, call()
        except PlanError as exc:
            return _error(409, str(exc))
        except BoardError as exc:
            return _error(503, f"The Kanban board cannot be reached now ({exc}). Nothing was changed.")

    @staticmethod
    def _body(body: Any) -> dict[str, Any]:
        return body if isinstance(body, dict) else {}

    def plan(self, horizon: Any = None, board: Optional[str] = None) -> Result:
        def call() -> dict[str, Any]:
            self.planner.run_due()  # a second trigger next to the timer: cheap when nothing is due
            return views.owner_view(self.planner, horizon_key=horizon or views.DEFAULT_HORIZON, board=board or None)
        return self._run(call)

    def set_slot(self, task_id: str, body: Any, board: Optional[str] = None) -> Result:
        b = self._body(body)

        def call() -> dict[str, Any]:
            minutes = b.get("estimate_minutes")
            if minutes is not None and (isinstance(minutes, bool) or not isinstance(minutes, int)):
                raise PlanError("estimate_minutes is a whole number of minutes.")
            start = parse_time(b.get("start"), self.planner.tz(), int(self.planner.clock()))
            change = self.planner.set_slot(task_id, start, estimate_s=minutes * 60 if minutes is not None else None,
                                           actor=OWNER, board=board or None, dry_run=b.get("dry_run") is True)
            return {"change": views.change_view(self.planner, change)}
        return self._run(call)

    def now(self, task_id: str, board: Optional[str] = None) -> Result:
        return self._run(lambda: {"change": views.change_view(
            self.planner, self.planner.run_now(task_id, actor=OWNER, board=board or None))})

    def clear(self, task_id: str, board: Optional[str] = None) -> Result:
        return self._run(lambda: {"change": views.change_view(
            self.planner, self.planner.clear(task_id, actor=OWNER, board=board or None))})

    def pin(self, task_id: str, body: Any, board: Optional[str] = None) -> Result:
        pinned = self._body(body).get("pinned")
        if not isinstance(pinned, bool):
            return _error(400, "pinned is true or false.")
        return self._run(lambda: {"change": views.change_view(
            self.planner, self.planner.pin(task_id, pinned, actor=OWNER, board=board or None))})

    def label(self, body: Any) -> Result:
        b = self._body(body)
        exclusive = b.get("exclusive")
        if exclusive is not None and not isinstance(exclusive, bool):
            return _error(400, "exclusive is true or false.")

        def call() -> dict[str, Any]:
            label = self.planner.label(b.get("name"), exclusive=exclusive, description=b.get("description"), actor=OWNER)
            return {"label": {"name": label.name, "exclusive": label.exclusive, "description": label.description}}
        return self._run(call)

    def drop_label(self, name: str) -> Result:
        def call() -> dict[str, Any]:
            self.planner.drop_label(name, actor=OWNER)
            return {"deleted": name}
        return self._run(call)

    def tag(self, task_id: str, body: Any, board: Optional[str] = None) -> Result:
        b = self._body(body)
        add, remove = b.get("add") or [], b.get("remove") or []
        if not isinstance(add, list) or not isinstance(remove, list):
            return _error(400, "add and remove are lists of label names.")
        return self._run(lambda: {"labels": self.planner.tag(task_id, add=add, remove=remove, actor=OWNER,
                                                             board=board or None)})
