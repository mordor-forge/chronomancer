"""Optional loopback Mini App. Authenticated APIs; the static shell contains no profile data.

No bot token, cookies, CORS, access log or query-string authentication. Every API request verifies
Telegram's Ed25519 initData and the current approver allow-list, including idempotent retries.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import logging
import re
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .initdata import InitDataError, verify_ed25519
from .store import StoreError
from .views import IDEMPOTENCY_KEY, audit_view, list_view, snapshot_view, window_view

logger = logging.getLogger(__name__)
READ_AGE_S, WRITE_AGE_S = 3600, 600
READ_LIMIT, WRITE_LIMIT = 120, 30  # per approver per minute, per process


def public_url(setting: Callable[..., Any]) -> str | None:
    raw = setting("miniapp_url", "")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        url = urlsplit(raw)
        if url.scheme == "https" and url.hostname and not url.username and not url.password and not url.fragment:
            return raw
    except ValueError:
        pass
    return None


def create_app(actions: Any, bot_id: Callable[[], int | None], *, wake: Callable[[], None] = lambda: None,
               verify: Callable[..., Any] = verify_ed25519) -> Any:
    from aiohttp import web

    html = Path(__file__).with_name("miniapp.html").read_text()

    def inline_hash(tag: str) -> str:
        [body] = re.findall(f"<{tag}>(.*?)</{tag}>", html, re.S)
        digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
        return f"'sha256-{digest}'"

    csp = (f"default-src 'none'; script-src {inline_hash('script')} https://telegram.org/js/telegram-web-app.js; "
           f"style-src {inline_hash('style')}; connect-src 'self'; img-src data:; "
           "base-uri 'none'; form-action 'none'")
    user_key = getattr(web, "RequestKey", web.AppKey)("user_id", int)
    buckets: dict[tuple[int, bool], deque[float]] = defaultdict(deque)

    @web.middleware
    async def policy(request: Any, handler: Any) -> Any:
        try:
            if request.path.startswith("/api/"):
                write = request.method not in ("GET", "HEAD", "OPTIONS")
                bid = bot_id()
                if not bid:
                    raise web.HTTPServiceUnavailable(text="Telegram is not ready; retry shortly.")
                try:
                    user = verify(request.headers.get("X-Telegram-Init-Data", ""), bid,
                                  max_age_s=WRITE_AGE_S if write else READ_AGE_S, now=int(actions.clock()))
                except InitDataError:
                    # Do not echo malformed fields or headers (including initData) into logs or responses.
                    raise web.HTTPUnauthorized(text="Telegram session invalid or expired; reopen the Mini App.")
                if not actions.is_approver(user.get("id")):
                    raise web.HTTPForbidden(text="Only configured approvers can use this Mini App.")
                request[user_key] = user["id"]
                now = asyncio.get_running_loop().time()
                bucket = buckets[(user["id"], write)]
                while bucket and bucket[0] <= now - 60:
                    bucket.popleft()
                if len(bucket) >= (WRITE_LIMIT if write else READ_LIMIT):
                    raise web.HTTPTooManyRequests(text="Too many requests; retry in a minute.",
                                                  headers={"Retry-After": "60"})
                bucket.append(now)
            response = await handler(request)
        except web.HTTPException as exc:
            response = web.json_response({"error": exc.text}, status=exc.status,
                                         headers={k: v for k, v in exc.headers.items()
                                                  if k.lower() in ("allow", "retry-after")})
        except Exception as exc:
            logger.warning("change-windows: Mini App request failed (%s)", type(exc).__name__)
            response = web.json_response({"error": "Request failed; retry shortly."}, status=503)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "no-referrer"})
        return response

    async def page(request: Any) -> Any:
        return web.Response(text=html, content_type="text/html", headers={"Content-Security-Policy": csp})

    async def windows(request: Any) -> Any:
        return web.json_response(await asyncio.to_thread(list_view, actions))

    async def detail(request: Any) -> Any:
        def read() -> Any:
            w = actions.store.get(request.match_info["window_id"])
            return window_view(actions, w, int(actions.clock())) if w else None
        result = await asyncio.to_thread(read)
        if result is None:
            raise web.HTTPNotFound(text="Window not found.")
        return web.json_response({"window": result})

    async def audit(request: Any) -> Any:
        return web.json_response({"audit": await asyncio.to_thread(audit_view, actions.store)})

    async def action(request: Any) -> Any:
        key = request.headers.get("Idempotency-Key", "")
        if not IDEMPOTENCY_KEY.fullmatch(key):
            raise web.HTTPBadRequest(text="Idempotency-Key must be 16-128 letters, digits, underscores or hyphens.")
        try:
            result = await asyncio.to_thread(actions.store.api_action, request[user_key], key,
                                             request.match_info["window_id"], request.match_info["action"],
                                             int(actions.clock()))
        except StoreError as exc:
            raise web.HTTPConflict(text=str(exc))
        wake()
        return web.json_response({"window": snapshot_view(result)})

    app = web.Application(middlewares=[policy], client_max_size=16384)
    app.router.add_get("/", page)
    app.router.add_get("/api/windows", windows)
    app.router.add_get("/api/audit", audit)
    app.router.add_get(r"/api/windows/{window_id:W[0-9A-F]{6}}", detail)
    app.router.add_post(r"/api/windows/{window_id:W[0-9A-F]{6}}/{action:approve|decline|close}", action)
    return app


async def serve(runtime: Any) -> None:
    """One supervised listener per runtime. Unload/cancellation closes the socket.

    On configuration/bind failure log once and await cancellation; reconnect cannot cause a
    noisy retry loop or interfere with the guard. Restart the gateway after fixing configuration.
    """
    from aiohttp import web

    runner = None
    try:
        host = str(runtime.actions.setting("miniapp_host", "127.0.0.1"))
        if host != "localhost" and not ipaddress.ip_address(host).is_loopback:
            raise ValueError("loopback host required")
        port = int(runtime.actions.setting("miniapp_port", 18782))
        if not 1 <= port <= 65535:
            raise ValueError("invalid port")
        runner = web.AppRunner(create_app(runtime.actions, runtime.bot_id, wake=runtime.wake), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
        logger.info("change-windows: Mini App listening on loopback")
        await asyncio.Event().wait()
    except (OSError, ValueError):
        logger.error("change-windows: Mini App listener unavailable; check loopback host and port. Guard remains active.")
        await asyncio.Event().wait()
    finally:
        if runner is not None:
            await runner.cleanup()
