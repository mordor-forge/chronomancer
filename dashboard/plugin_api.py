"""Backend routes for the Hermes Desktop half, mounted at ``/api/plugins/change-windows/``.

    GET  /windows                         windows from today to seven days ahead, and whether
                                          Desktop approval is on
    GET  /audit                           recent matched calls (metadata only) and recent decisions
    POST /windows/{id}/approve|decline|close    body: {"key": "<idempotency key>"}

Hermes authenticates every request before a route runs (the Desktop session's native grant, or a
signed-in dashboard session) and scopes it to the requested profile. These routes add no identity
of their own: see ``change_windows/desktop.py`` for the trust statement and for the logic. This
file only maps HTTP to it.

Hermes imports this file by path, outside the plugin's package, so the core package is found
through the loaded agent half (same process: the Telegram scheduler is woken after an action) or
loaded from this checkout.
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

PLUGIN_NAME = "change-windows"
_ROOT = Path(__file__).resolve().parents[1]
_CORE_NAME = "hermes_change_windows_core"
_LOCK = threading.Lock()
_APIS: dict[str, Any] = {}

router = APIRouter()


def _core_package() -> str:
    """Import name of this checkout's ``change_windows`` package."""
    entry = _ROOT / "__init__.py"
    for name, module in list(sys.modules.items()):
        if not name.startswith("hermes_plugins.") or name.count(".") != 1:
            continue
        try:
            if Path(getattr(module, "__file__", "") or "").resolve() == entry:
                return f"{name}.change_windows"
        except OSError:
            continue
    if _CORE_NAME not in sys.modules:
        package_dir = _ROOT / "change_windows"
        spec = importlib.util.spec_from_file_location(
            _CORE_NAME, package_dir / "__init__.py", submodule_search_locations=[str(package_dir)])
        if spec is None or spec.loader is None:
            raise ImportError("change-windows core package not found")
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
    """``plugins.entries.change-windows.settings.<key>`` of the request's profile, as
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


def _api() -> Any:
    """One API object per profile data directory (the store prepares its schema once per file)."""
    from plugins.plugin_storage import plugin_data_dir, plugin_db

    key = str(plugin_data_dir(PLUGIN_NAME))
    with _LOCK:
        api = _APIS.get(key)
        if api is None:
            core = _core_package()
            store_mod, tg, desktop, timezones = (importlib.import_module(f"{core}.{name}")
                                                 for name in ("store", "tg", "desktop", "timezones"))
            actions = tg.Actions(store_mod.Store(lambda: plugin_db(PLUGIN_NAME)), setting=_setting,
                                 tz=lambda: timezones.local_tz(_setting("timezone", None)))
            api = _APIS[key] = desktop.DesktopApi(actions, wake=tg.wake_all)
        return api


def _respond(call: Callable[[Any], tuple[int, dict[str, Any]]]) -> JSONResponse:
    try:
        status, body = call(_api())
    except Exception as exc:
        # The type only: a store or config error can carry paths or values.
        logger.warning("change-windows: Desktop request failed (%s)", type(exc).__name__)
        status, body = 503, {"detail": "The window store is unavailable; retry shortly."}
    return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


# Plain ``def`` handlers: FastAPI runs them in a worker thread, so SQLite never blocks the gateway loop.
@router.get("/windows")
def windows() -> JSONResponse:
    return _respond(lambda api: api.windows())


@router.get("/audit")
def audit() -> JSONResponse:
    return _respond(lambda api: api.audit())


@router.post("/windows/{window_id}/{action}")
def act(window_id: str, action: str, body: Any = Body(default=None)) -> JSONResponse:
    key = body.get("key") if isinstance(body, dict) else None
    return _respond(lambda api: api.act(window_id, action, key))
