"""Backend routes for the Hermes Desktop half, mounted at ``/api/plugins/chronomancer/``.

    GET    /plan?horizon=day|2d|week|<hours>     the plan: lanes, slots, clashes, tray, labels, log
    POST   /cards/{id}/slot    {"start": <unix seconds or ISO 8601>, "estimate_minutes"?, "dry_run"?}
    POST   /cards/{id}/now
    DELETE /cards/{id}/slot
    POST   /cards/{id}/pin     {"pinned": true|false}
    POST   /cards/{id}/labels  {"add": [...], "remove": [...]}
    POST   /labels             {"name", "exclusive"?, "description"?}
    DELETE /labels/{name}

Every route takes an optional ``board`` query parameter.

Hermes authenticates every request before a route runs (the Desktop session's native grant, or a
signed-in dashboard session) and scopes it to the requested profile. These routes add no identity
of their own: see ``chronomancer/desktop.py`` for the trust statement and for the logic. This file
only maps HTTP to it.

Hermes imports this file by path, outside the plugin's package, and usually in another process
than the agent half: the dashboard server and the gateway are separate services. So:

- When the agent half is loaded in this process, its planner (and timer) is used.
- Otherwise this file builds a planner of its own on the same plan database, loaded from this
  checkout. It reaches the Kanban through the tool registry of this process, after importing only
  the kanban tools. The timer stays in the gateway.
"""
from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
import threading
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

PLUGIN_NAME = "chronomancer"
_ROOT = Path(__file__).resolve().parents[1]
_CORE_NAME = "hermes_chronomancer_core"
_LOCK = threading.Lock()
_APIS: dict[str, Any] = {}

router = APIRouter()


def _core_package() -> str:
    """Import name of this checkout's ``chronomancer`` package."""
    entry = _ROOT / "__init__.py"
    for name, module in list(sys.modules.items()):
        if not name.startswith("hermes_plugins.") or name.count(".") != 1:
            continue
        try:
            if Path(getattr(module, "__file__", "") or "").resolve() == entry:
                return f"{name}.chronomancer"
        except OSError:
            continue
    if _CORE_NAME not in sys.modules:
        package_dir = _ROOT / "chronomancer"
        spec = importlib.util.spec_from_file_location(
            _CORE_NAME, package_dir / "__init__.py", submodule_search_locations=[str(package_dir)])
        if spec is None or spec.loader is None:
            raise ImportError("chronomancer core package not found")
        module = importlib.util.module_from_spec(spec)
        sys.modules[_CORE_NAME] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(_CORE_NAME, None)
            raise
    return _CORE_NAME


def _lookup(tree: Any, key: str, missing: Any) -> Any:
    """``a.b`` as nested mappings (what Hermes writes), else as one literal key."""
    current = tree
    for segment in key.split("."):
        if not isinstance(current, dict) or segment not in current:
            current = missing
            break
        current = current[segment]
    if current is missing and isinstance(tree, dict) and key in tree:
        current = tree[key]
    return current


def _setting(key: str, default: Any = None) -> Any:
    """``plugins.entries.chronomancer.settings.<key>`` of the request's profile, as
    ``ctx.get_config`` reads it (with the legacy ``config`` subtree as a fallback)."""
    from hermes_cli import config as hermes_config

    load = getattr(hermes_config, "load_config_readonly", None) or hermes_config.load_config
    entry = _lookup(load() or {}, f"plugins.entries.{PLUGIN_NAME}", None)
    if not isinstance(entry, dict):
        return default
    missing = object()
    for subtree in ("settings", "config"):
        value = _lookup(entry.get(subtree), key, missing)
        if value is not missing:
            return value
    return default


def _kanban(tool: str, args: dict) -> Any:
    """The kanban tools of this process. There is no ``ctx.dispatch_tool`` here (see the module
    docstring), so the registry is asked directly. Only the kanban tools are imported: the rest of
    the tool registry (MCP servers, browsers, ...) stays unloaded in the dashboard process."""
    import tools.kanban_tools  # noqa: F401  (registers kanban_* in the registry)
    from tools.registry import registry

    return registry.dispatch(tool, args)


def _api() -> Any:
    """The Desktop API for the request's profile: the agent half's planner when it is loaded in
    this process, else a planner of its own on the same plan database."""
    from plugins.plugin_storage import plugin_data_dir, plugin_db

    data_dir = plugin_data_dir(PLUGIN_NAME)
    key = str(data_dir)
    core = _core_package()
    runtime, desktop = (importlib.import_module(f"{core}.{name}") for name in ("runtime", "desktop"))
    with _LOCK:
        active = runtime.get(key)
        if active is None:
            active = runtime.build(data_dir, dispatch=_kanban, read_setting=_setting, timer=False,
                                   prepare=lambda filename: plugin_db(PLUGIN_NAME, filename))
        api = _APIS.get(key)
        if api is None or api.planner is not active.planner:
            api = _APIS[key] = desktop.DesktopApi(active.planner, refresh=active.settings.refresh)
        return api


def _respond(call: Callable[[Any], tuple[int, dict[str, Any]]]) -> JSONResponse:
    try:
        status, body = call(_api())
    except Exception as exc:
        # The type only: a store or config error can carry paths or values.
        logger.warning("chronomancer: Desktop request failed (%s)", type(exc).__name__)
        status, body = 503, {"detail": "The plan is unavailable; retry shortly."}
    return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


# Plain ``def`` handlers: FastAPI runs them in a worker thread, so SQLite never blocks the gateway loop.
@router.get("/plan")
def plan(horizon: str = "", board: str = "") -> JSONResponse:
    return _respond(lambda api: api.plan(horizon, board))


@router.post("/cards/{task_id}/slot")
def set_slot(task_id: str, body: Any = Body(default=None), board: str = "") -> JSONResponse:
    return _respond(lambda api: api.set_slot(task_id, body, board))


@router.delete("/cards/{task_id}/slot")
def clear_slot(task_id: str, board: str = "") -> JSONResponse:
    return _respond(lambda api: api.clear(task_id, board))


@router.post("/cards/{task_id}/now")
def run_now(task_id: str, board: str = "") -> JSONResponse:
    return _respond(lambda api: api.now(task_id, board))


@router.post("/cards/{task_id}/pin")
def pin(task_id: str, body: Any = Body(default=None), board: str = "") -> JSONResponse:
    return _respond(lambda api: api.pin(task_id, body, board))


@router.post("/cards/{task_id}/labels")
def tag(task_id: str, body: Any = Body(default=None), board: str = "") -> JSONResponse:
    return _respond(lambda api: api.tag(task_id, body, board))


@router.post("/labels")
def label(body: Any = Body(default=None)) -> JSONResponse:
    return _respond(lambda api: api.label(body))


@router.delete("/labels/{name:path}")
def drop_label(name: str) -> JSONResponse:
    return _respond(lambda api: api.drop_label(name))
