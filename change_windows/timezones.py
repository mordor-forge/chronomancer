"""The plugin's time zone: an IANA zone, so times after a daylight-saving change are right.

``timezone`` setting first; when it is empty, the system zone by name (the ``TZ`` variable, then
the ``/etc/localtime`` link). Only when no name can be found does it fall back to today's fixed
UTC offset, which is wrong for dates on the other side of a daylight-saving change.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_MARK = "zoneinfo/"
_warned: set[str] = set()


def _warn_once(key: str, message: str, *args: Any) -> None:
    if key not in _warned:
        _warned.add(key)
        logger.warning(message, *args)


def _zone(name: str) -> Optional[ZoneInfo]:
    try:
        return ZoneInfo(name)
    except Exception:  # unknown key, bad characters, no tz database
        return None


def system_zone_name(*, environ: Optional[Mapping[str, str]] = None,
                     localtime: Path = Path("/etc/localtime")) -> Optional[str]:
    """The system's IANA zone name, or None when it cannot be found."""
    environ = os.environ if environ is None else environ
    candidates = [str(environ.get("TZ") or "").lstrip(":")]
    try:
        candidates.append(os.path.realpath(localtime))
    except OSError:
        pass
    for value in candidates:
        if _MARK in value:  # a path into the tz database: /usr/share/zoneinfo/Europe/Prague
            value = value.rsplit(_MARK, 1)[1]
        if value and not value.startswith("/") and _zone(value) is not None:
            return value
    return None


def local_tz(name: Optional[str], *, environ: Optional[Mapping[str, str]] = None,
             localtime: Path = Path("/etc/localtime")) -> Any:
    """``tzinfo`` for parsing and showing times. See the module docstring for the order."""
    if name:
        zone = _zone(str(name).strip())
        if zone is not None:
            return zone
        _warn_once(f"unknown:{name}", "change-windows: unknown timezone %r; using the system zone", name)
    system = system_zone_name(environ=environ, localtime=localtime)
    if system:
        return ZoneInfo(system)
    _warn_once("fixed", "change-windows: could not find the system's IANA time zone; using today's fixed offset "
                        "(wrong across a daylight-saving change). Set the timezone setting.")
    return datetime.now().astimezone().tzinfo
