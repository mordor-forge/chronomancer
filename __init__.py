"""change-windows: approved time windows for production writes by Hermes agents.

Wiring:
- ``pre_tool_call`` guard: matched production-write calls need an open window for their scope.
- Agent tools: window_propose, window_list, window_next, window_close.
- Telegram (``register_platform_handler``, in-process only): proposal messages with Approve /
  Decline, Close now, ``/windows``; one scheduler task per profile (``spawn_task``) that sends the
  proposal messages, releases linked Kanban cards at start (``dispatch_tool``: kanban_show,
  kanban_unblock, kanban_comment) and sends the open and end notices.

- Optional Mini App: one loopback HTTP server started from the Telegram factory, with
  Telegram-signed initData, an approver allow-list and durable idempotency keys.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from .change_windows import cards, guard, rules, tg, tools
from .change_windows.store import Store
from .change_windows.timezones import local_tz

logger = logging.getLogger(__name__)

PLUGIN_NAME = "change-windows"
TOOLSET = "change_windows"


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
    from plugins.plugin_storage import plugin_data_dir, plugin_db

    store = Store(lambda: plugin_db(PLUGIN_NAME))
    rule_cache = _RuleCache(ctx)

    def setting(key: str, default: Any = None) -> Any:
        return ctx.get_config(key, default=default)

    def mode() -> str:
        raw = setting("mode", "enforce")
        if raw is False:  # YAML 1.1 reads an unquoted ``mode: off`` as the boolean False
            return "off"
        value = str(raw).strip().lower()
        return value if value in guard.MODES else "enforce"  # an unknown value fails closed

    def tz() -> Any:
        return local_tz(setting("timezone", None))

    agent_tools = tools.Tools(store, settings=setting, tz=tz, actor=lambda: f"agent:{getattr(ctx, 'profile_name', None) or 'default'}",
                              on_proposed=lambda _window, _conflicts: tg.wake_all())
    for schema, handler in (
        (tools.PROPOSE, agent_tools.propose),
        (tools.LIST, agent_tools.list),
        (tools.NEXT, agent_tools.next),
        (tools.CLOSE, agent_tools.close),
    ):
        ctx.register_tool(name=schema["name"], toolset=TOOLSET, schema=schema, handler=handler, emoji="🕒")

    ctx.register_hook("pre_tool_call", guard.make_hook(
        get_rules=rule_cache.get, get_mode=mode, store=store, tz=tz,
        command_preview=lambda: setting("audit_command_preview", False) is True))

    # Linked cards get their titles on the owner surfaces through the kanban tools.
    cards.configure(lambda tool, args: ctx.dispatch_tool(tool, args))

    # Telegram: needs the native Application, so in-process plugins only. Under
    # plugins.isolation: host (or on a core without the seam) the guard and the tools still work.
    factory = tg.make_factory(
        key=lambda: str(plugin_data_dir(PLUGIN_NAME)),
        actions=tg.Actions(store, setting=setting, tz=tz),
        spawn=lambda coro, name: ctx.spawn_task(coro, name=name),
        dispatch=lambda tool, args: ctx.dispatch_tool(tool, args))
    try:
        ctx.register_platform_handler("telegram", factory)
    except Exception as exc:
        logger.warning("change-windows: Telegram approvals and the scheduler are off (%s). The guard and the "
                       "agent tools still work; the Telegram parts need plugins.isolation: in_process.", exc)
