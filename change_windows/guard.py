"""The decision behind the ``pre_tool_call`` hook: allow, warn or block one tool call."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .rules import SELF_SCOPE, RuleSet, extract_text
from .store import Store, Window

MODES = ("enforce", "warn", "off")

# Mask obvious secrets before a command preview reaches the audit log.
_SECRET_RE = re.compile(
    r"(?i)(--?(token|password|passwd|secret|api[-_]?key)[= ]\S+|(token|password|secret|key)=\S+|"
    r"bearer\s+\S+|gh[pousr]_[A-Za-z0-9]{20,}|xox[abpr]-\S+)")


@dataclass
class Decision:
    verdict: str  # "allow" | "warn" | "block"
    rule_ids: list[str] = field(default_factory=list)
    scopes: list[str] = field(default_factory=list)
    window_id: Optional[str] = None
    message: str = ""

    def hook_result(self) -> Optional[dict[str, str]]:
        if self.verdict == "block":
            return {"action": "block", "message": self.message}
        return None


def preview(tool_name: str, args: dict[str, Any], field_name: str = "command") -> str:
    text = extract_text(args, field_name) or extract_text(args, "*")
    return _SECRET_RE.sub("[masked]", text)[:400]


def _fmt(ts: int, tz: Any) -> str:
    return datetime.fromtimestamp(ts, tz or timezone.utc).strftime("%a %d %b %H:%M %Z")


def decide(
    tool_name: str,
    args: dict[str, Any],
    *,
    rules: RuleSet,
    store: Store,
    mode: str = "enforce",
    now: Optional[int] = None,
    tz: Any = None,
) -> Decision:
    """Pure policy: no matched rule -> allow; every matched scope needs an open window.

    Store errors propagate to the caller, which turns them into a block (fail closed).
    """
    if mode == "off":
        return Decision("allow")
    matched = rules.match(tool_name, args)
    if not matched:
        return Decision("allow")
    now = int(time.time()) if now is None else now
    rule_ids = [r.id for r in matched]
    scopes = sorted({r.scope for r in matched})
    missing: list[str] = []
    window_id: Optional[str] = None
    for scope in scopes:
        if scope == SELF_SCOPE:
            missing.append(scope)
            continue
        open_now = store.open_windows(scope, now)
        if open_now:
            window_id = open_now[0].id
        else:
            missing.append(scope)
    if not missing:
        return Decision("allow", rule_ids, scopes, window_id)
    if mode == "warn":
        return Decision("warn", rule_ids, scopes, window_id, "")
    return Decision("block", rule_ids, scopes, window_id, block_message(missing, rule_ids, store, now, tz))


def block_message(missing: list[str], rule_ids: list[str], store: Store, now: int, tz: Any) -> str:
    if SELF_SCOPE in missing:
        return ("change-windows: blocked. This call touches the change-windows store or its approval "
                "commands, which no window can open. Only the owner approves windows (Telegram or a TTY).")
    lines = [f"change-windows: blocked. Rule(s) {', '.join(rule_ids)} need an open change window for "
             f"scope(s) {', '.join(missing)}, and none is open now."]
    for scope in missing:
        nxt: Optional[Window] = store.next_window(scope, now)
        if nxt is not None and nxt.start_ts > now:
            lines.append(f"Next approved window for {scope}: {nxt.id} '{nxt.name}' "
                         f"{_fmt(nxt.start_ts, tz)} to {_fmt(nxt.end_ts, tz)}.")
        elif nxt is not None and nxt.presence and nxt.checked_in_at is None:
            lines.append(f"Window {nxt.id} for {scope} is waiting for the owner's check-in.")
        else:
            lines.append(f"No approved window for {scope}.")
    lines.append("Do not retry or work around this. Call window_propose(scope, start, end, reason) and wait "
                 "for the owner's approval, or stop and tell the user.")
    return " ".join(lines)


def make_hook(
    *,
    get_rules: Callable[[], RuleSet],
    get_mode: Callable[[], str],
    store: Store,
    tz: Callable[[], Any] = lambda: None,
    clock: Callable[[], float] = time.time,
) -> Callable[..., Optional[dict[str, str]]]:
    """Build the ``pre_tool_call`` callback. Never raises for a matched call: it blocks instead."""

    def pre_tool_call(tool_name: str = "", args: Optional[dict[str, Any]] = None, task_id: str = "",
                      session_id: str = "", **_: Any) -> Optional[dict[str, str]]:
        args = args if isinstance(args, dict) else {}
        rules = get_rules()
        if not rules.match(tool_name, args):
            return None  # the fast path: most calls never touch the store
        now = int(clock())
        try:
            decision = decide(tool_name, args, rules=rules, store=store, mode=get_mode(), now=now, tz=tz())
        except Exception as exc:  # fail closed on a matched call
            return {"action": "block",
                    "message": f"change-windows: blocked because the window store failed ({type(exc).__name__}). "
                               f"Tell the user; do not retry."}
        if decision.verdict in ("block", "warn") or decision.window_id:
            try:
                store.record_audit(
                    now=now, verdict={"block": "blocked", "warn": "warned"}.get(decision.verdict, "allowed"),
                    tool=tool_name, rule_ids=decision.rule_ids, scopes=decision.scopes,
                    window_id=decision.window_id, preview=preview(tool_name, args),
                    session_id=session_id or "", task_id=task_id or "")
            except Exception:
                pass  # the audit row is best effort; the verdict is not
        return decision.hook_result()

    return pre_tool_call
