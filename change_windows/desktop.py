"""Hermes Desktop owner surface: the logic behind ``dashboard/plugin_api.py``.

No web framework here, so it is tested without Hermes or FastAPI. Every method returns
``(HTTP status, JSON body)``.

Trust: the Mini App verifies a Telegram signature on every request. This surface does not. Hermes
authenticates the request (the Desktop session, or a signed-in dashboard session) before the route
runs, and this code trusts that. Code running as the same OS user can reach the same local API.
``approval.desktop: false`` turns approve and decline off here; closing stays, because closing only
removes permission.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

from . import overlaps
from .store import OverlapError, StoreError
from .views import ACTIONS, IDEMPOTENCY_KEY, WINDOW_ID, audit_view, decisions_view, list_view, snapshot_view

logger = logging.getLogger(__name__)

CHANNEL = "desktop"
ACTOR = "desktop"   # stored as decided_by: there is no per-user identity on this surface
KEY_OWNER = 0       # idempotency keys of this surface; no Telegram user has id 0
APPROVAL_SETTING = "approval.desktop"

Result = tuple[int, dict[str, Any]]


def approval_enabled(setting: Callable[..., Any]) -> bool:
    """True only when the setting is absent or the boolean ``true``. Anything else (``false``,
    ``"false"``, ``0``, a typo, an unreadable config) turns Desktop approval off: the switch that
    narrows who can approve must not fail open."""
    try:
        return setting(APPROVAL_SETTING, True) is True
    except Exception:
        logger.warning("change-windows: could not read %s; Desktop approval is off", APPROVAL_SETTING)
        return False


def _error(status: int, message: str) -> Result:
    return status, {"detail": message}  # the key FastAPI and the Desktop client use for errors


class DesktopApi:
    def __init__(self, actions: Any, *, wake: Callable[[], None] = lambda: None):
        self.actions = actions
        self.wake = wake

    def windows(self) -> Result:
        data = list_view(self.actions)
        data["approval"] = approval_enabled(self.actions.setting)
        return 200, data

    def audit(self) -> Result:
        store = self.actions.store
        return 200, {"audit": [{**row, "when": self._stamp(row["ts"])} for row in audit_view(store)],
                     "decisions": [{**d, "when": self._stamp(d["decided_at"])} for d in decisions_view(store)]}

    def _stamp(self, ts: Any) -> str:
        """A moment in the plugin's time zone, written like the window spans ("Tue 10 Nov 13:57 UTC").
        The page would otherwise show these in the app's own zone, next to spans in the plugin's."""
        if not isinstance(ts, int) or isinstance(ts, bool):
            return ""
        zone = self.actions.tz() or timezone.utc
        return datetime.fromtimestamp(ts, zone).strftime("%a %d %b %H:%M %Z").rstrip()

    def act(self, window_id: Any, action: Any, key: Any) -> Result:
        if action not in ACTIONS:
            return _error(404, "Unknown action.")
        if not isinstance(window_id, str) or not WINDOW_ID.fullmatch(window_id):
            return _error(400, "Window ids look like W1A2B3C.")
        if not isinstance(key, str) or not IDEMPOTENCY_KEY.fullmatch(key):
            return _error(400, "key must be 16-128 letters, digits, underscores or hyphens.")
        # Checked before a replay too: turning the setting off also stops retries of old requests.
        if action != "close" and not approval_enabled(self.actions.setting):
            return _error(403, "Desktop approval is off (approval.desktop: false). Decide in Telegram.")
        store = self.actions.store
        if store.get(window_id) is None:
            return _error(404, "Window not found.")
        try:
            result = store.api_action(KEY_OWNER, key, window_id, action, int(self.actions.clock()),
                                      channel=CHANNEL, by=ACTOR)
        except OverlapError as exc:
            return _error(409, overlaps.refusal(self.actions, exc, int(self.actions.clock())))
        except StoreError as exc:
            return _error(409, str(exc))
        try:
            self.wake()  # the gateway scheduler, when it runs in this process; it also polls
        except Exception:
            logger.debug("change-windows: wake after a Desktop action failed", exc_info=True)
        return 200, {"window": snapshot_view(result)}
