"""chronomancer: a shared plan for the Kanban cards of every profile on a Hermes gateway.

Wiring:
- One planner for this profile's plugin data directory (``chronomancer/runtime.py``). It reads and
  writes the Kanban board only through ``ctx.dispatch_tool`` (kanban_list, kanban_show,
  kanban_schedule, kanban_unblock, kanban_comment).
- Agent tools: chrono_plan, chrono_slot, chrono_now, chrono_clear, chrono_label.
- One daemon thread that releases a card when its slot starts. A Kanban worker process does not
  start it: a worker cannot release other cards.
- The owner's Desktop page talks to ``dashboard/plugin_api.py``, which uses the same planner.
"""

from __future__ import annotations

import os
from typing import Any

from .chronomancer import runtime, tools

PLUGIN_NAME = "chronomancer"
TOOLSET = "chronomancer"


def register(ctx: Any) -> None:
    from plugins.plugin_storage import plugin_data_dir, plugin_db

    data_dir = plugin_data_dir(PLUGIN_NAME)  # resolved now, while this profile is in scope
    active = runtime.build(
        data_dir,
        dispatch=lambda tool, args: ctx.dispatch_tool(tool, args),
        read_setting=lambda key, default: ctx.get_config(key, default=default),
        timer=not os.environ.get("HERMES_KANBAN_TASK"),
        prepare=lambda filename: plugin_db(PLUGIN_NAME, filename))

    agent_tools = tools.Tools(
        active.planner, refresh=active.settings.refresh,
        actor=lambda: f"agent:{getattr(ctx, 'profile_name', None) or 'default'}")
    for schema, handler in agent_tools.handlers():
        ctx.register_tool(name=schema["name"], toolset=TOOLSET, schema=schema, handler=handler, emoji="🔮")

    on_unload = getattr(ctx, "on_unload", None)  # stop the timer when the plugin is unloaded or reloaded
    if callable(on_unload):
        on_unload(lambda: runtime.detach(str(data_dir), active))
