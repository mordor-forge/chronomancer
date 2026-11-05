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

# Masking for the OPT-IN command preview (setting audit_command_preview). Best effort only: a
# stored preview can still hold a secret. The default audit row stores no argument values.
_REDACT_INPUT_CAP = 2000
_SENSITIVE = r"(?:token|password|passwd|pwd|secret|api[-_]?key|apikey|key|auth|authorization|credential|private[-_]?key)"
_QVAL = r"""(?:"(?:[^"\\]|\\.)*"|'[^']*'|\S+)"""
_MASKS = [
    # Authorization: Bearer x / Basic x
    re.compile(r"(?i)\b((?:bearer|basic)\s+)\S+"),
    # "password": "x y" / token=x / --set db.password='x' (JSON, assignments; escaped quotes handled)
    re.compile(rf"""(?i)((?<![\w-])[\\"']*[\w.-]{{0,40}}{_SENSITIVE}[\w.-]{{0,40}}[\\"']*\s*[:=]\s*){_QVAL}"""),
    # --password "x y" / --token x / -p x
    re.compile(rf"""(?i)((?<![\w-])(?:--?[\w-]{{0,40}}{_SENSITIVE}[\w-]{{0,40}}|-p)(?:=|\s+)){_QVAL}"""),
]
_TOKEN_SHAPES = re.compile(
    r"(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[abprs]-[A-Za-z0-9-]+|sk-[A-Za-z0-9_-]{16,}|"
    r"AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|\d{6,}:[A-Za-z0-9_-]{30,})")


def redact(text: str) -> str:
    """Mask common secret shapes. Input is capped first so a long argument cannot stall the guard."""
    if len(text) > _REDACT_INPUT_CAP:
        text = text[:_REDACT_INPUT_CAP] + " [truncated]"
    for pat in _MASKS:
        text = pat.sub(lambda m: m.group(1) + "[masked]", text)
    return _TOKEN_SHAPES.sub("[masked]", text)


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


def preview(tool_name: str, args: dict[str, Any], *, include_command: bool = False) -> str:
    """Audit preview. Default: metadata only (no argument values). Opt-in: the masked shell line."""
    if tool_name == "terminal":
        command = extract_text(args, "command")
        if include_command:
            return redact(command)[:400]
        return f"terminal: {len(command)} chars (command not stored)"
    if tool_name == "execute_code":
        return f"execute_code: {len(extract_text(args, 'code'))} chars of code (not stored)"
    return f"{tool_name}: args {sorted(args)[:12]} (values not stored)"


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
    if SELF_SCOPE in scopes and mode != "off":  # self-protection blocks in warn mode too
        return Decision("block", rule_ids, scopes, None, block_message([SELF_SCOPE], rule_ids, store, now, tz))
    for scope in scopes:
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
        return ("change-windows: blocked. This call touches the change-windows store, settings or plugin "
                "state, which no window can open. Only the owner approves windows, from Telegram.")
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
    command_preview: Callable[[], bool] = lambda: False,
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
                    window_id=decision.window_id, preview=preview(tool_name, args, include_command=bool(command_preview())),
                    session_id=session_id or "", task_id=task_id or "")
            except Exception:
                pass  # the audit row is best effort; the verdict is not
        return decision.hook_result()

    return pre_tool_call
