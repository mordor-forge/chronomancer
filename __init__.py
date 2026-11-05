"""change-windows: approved time windows for production writes by Hermes agents.

Skeleton wiring (build day 1 state):
- ``pre_tool_call`` guard: matched production-write calls need an open window for their scope.
- Agent tools: window_propose, window_list, window_next, window_close.

Planned (see SPEC "Build plan"; not registered yet, so the manifest stays truthful):
- Telegram: approval buttons, /windows command and the Mini App (register_platform_handler).
- Scheduler: reminders, card release at start, miss report (spawn_task + on_kanban_dispatch_tick).
- CLI: ``hermes change-windows`` (list, approve on a real TTY).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from typing import Any

from .change_windows import guard, rules, tools
from .change_windows.store import Store

logger = logging.getLogger(__name__)

PLUGIN_NAME = "change-windows"
TOOLSET = "change_windows"


def _local_tz(name: str | None) -> Any:
    if name:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name)
        except Exception:
            logger.warning("change-windows: unknown timezone %r; using the system zone", name)
    return datetime.now().astimezone().tzinfo


class _RuleCache:
    """Compile rules once per distinct settings value; re-read settings at most every few seconds."""

    def __init__(self, ctx: Any, ttl_s: float = 5.0):
        self._ctx = ctx
        self._ttl = ttl_s
        self._lock = threading.Lock()
        self._key: str | None = None
        self._rules: rules.RuleSet | None = None
        self._read_at = 0.0

    def get(self) -> rules.RuleSet:
        now = time.monotonic()
        with self._lock:
            if self._rules is not None and now - self._read_at < self._ttl:
                return self._rules
            extra = self._ctx.get_config("rules", default=[]) or []
            disabled = self._ctx.get_config("disabled_rules", default=[]) or []
            key = json.dumps([extra, disabled], sort_keys=True, default=str)
            if key != self._key or self._rules is None:
                self._rules = rules.build_ruleset(extra if isinstance(extra, list) else [],
                                                  disabled if isinstance(disabled, list) else [])
                for err in self._rules.errors:
                    logger.warning("change-windows: rule skipped: %s", err)
                self._key = key
            self._read_at = now
            return self._rules


def register(ctx: Any) -> None:
    from plugins.plugin_storage import plugin_db

    store = Store(lambda: plugin_db(PLUGIN_NAME))
    rule_cache = _RuleCache(ctx)

    def setting(key: str, default: Any = None) -> Any:
        return ctx.get_config(key, default=default)

    def mode() -> str:
        value = str(setting("mode", "warn")).strip().lower()
        return value if value in guard.MODES else "enforce"  # an unknown value fails closed

    def tz() -> Any:
        return _local_tz(setting("timezone", None))

    agent_tools = tools.Tools(store, settings=setting, tz=tz, actor=lambda: f"agent:{ctx.profile_name}")
    for schema, handler in (
        (tools.PROPOSE, agent_tools.propose),
        (tools.LIST, agent_tools.list),
        (tools.NEXT, agent_tools.next),
        (tools.CLOSE, agent_tools.close),
    ):
        ctx.register_tool(name=schema["name"], toolset=TOOLSET, schema=schema, handler=handler, emoji="🕒")

    ctx.register_hook("pre_tool_call", guard.make_hook(get_rules=rule_cache.get, get_mode=mode, store=store, tz=tz))
