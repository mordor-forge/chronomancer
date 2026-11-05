"""Telegram Mini App ``initData`` verification.

Default: the Ed25519 ``signature`` check for third parties (Telegram Mini Apps docs, "Validating
data for Third-Party Use"). It needs only the public bot id and Telegram's published public key,
so this plugin never holds the bot token. The HMAC check with the bot token is kept for completeness
and tests; the plugin does not use it unless the operator opts in.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any, Optional
from urllib.parse import parse_qsl

TELEGRAM_PUBLIC_KEYS = {
    "production": "e7bf03a2fa4602af4580703d88dda5bb59f32ed8b02a56c187fe7d34caed242d",
    "test": "40055058a4ee38156a06562e52eece92a771bcd8346a8c4615cb7376eddf72ec",
}


class InitDataError(ValueError):
    """initData is missing, malformed, stale, or its signature does not verify."""


def parse_init_data(init_data: str) -> dict[str, str]:
    if not isinstance(init_data, str) or not init_data or len(init_data) > 8192:
        raise InitDataError("initData missing or too long")
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise InitDataError("initData is not a query string") from exc
    fields: dict[str, str] = {}
    for key, value in pairs:
        if key in fields:
            raise InitDataError(f"duplicate field {key}")
        fields[key] = value
    return fields


def _check_lines(fields: dict[str, str], exclude: tuple[str, ...]) -> str:
    return "\n".join(f"{k}={fields[k]}" for k in sorted(fields) if k not in exclude)


def _fresh(fields: dict[str, str], max_age_s: int, now: Optional[int]) -> None:
    try:
        auth_date = int(fields["auth_date"])
    except (KeyError, ValueError) as exc:
        raise InitDataError("auth_date missing") from exc
    now = int(time.time()) if now is None else now
    if auth_date > now + 60:
        raise InitDataError("auth_date is in the future")
    if now - auth_date > max_age_s:
        raise InitDataError("initData is too old; reopen the Mini App")


def _user(fields: dict[str, str]) -> dict[str, Any]:
    try:
        user = json.loads(fields.get("user") or "")
    except ValueError as exc:
        raise InitDataError("user field is not JSON") from exc
    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        raise InitDataError("user.id missing")
    return user


def verify_ed25519(init_data: str, bot_id: int, *, public_key_hex: str = TELEGRAM_PUBLIC_KEYS["production"],
                   max_age_s: int = 3600, now: Optional[int] = None) -> dict[str, Any]:
    """Return the verified ``user`` object, or raise ``InitDataError``."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    fields = parse_init_data(init_data)
    sig_b64 = fields.get("signature")
    if not sig_b64:
        raise InitDataError("signature missing")
    try:
        signature = base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4))
    except ValueError as exc:
        raise InitDataError("signature is not base64url") from exc
    message = f"{int(bot_id)}:WebAppData\n" + _check_lines(fields, ("hash", "signature"))
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex)).verify(signature, message.encode())
    except (InvalidSignature, ValueError) as exc:
        raise InitDataError("signature does not verify") from exc
    _fresh(fields, max_age_s, now)
    return _user(fields)


def verify_hmac(init_data: str, bot_token: str, *, max_age_s: int = 3600,
                now: Optional[int] = None) -> dict[str, Any]:
    """Bot-token HMAC check (Telegram docs, "Validating data received via the Mini App")."""
    fields = parse_init_data(init_data)
    received = fields.get("hash") or ""
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, _check_lines(fields, ("hash",)).encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise InitDataError("hash does not verify")
    _fresh(fields, max_age_s, now)
    return _user(fields)
