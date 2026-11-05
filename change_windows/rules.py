"""Guard rules: which tool calls count as production writes, and which window scope they need.

A rule matches a tool name (fnmatch pattern) and, optionally, a regular expression on one
argument of the call (``command`` for ``terminal``, ``code`` for ``execute_code``, ``*`` for all
arguments as JSON). Text rules are best effort: a determined caller can hide a command from a
regular expression. The guard is a seatbelt for honest agents, not a sandbox (see README).
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

# The scope no window can ever open: rules in it always block (self-protection).
SELF_SCOPE = "_self"

# A command segment: stop at a pipe, a list separator or a new line, so
# "kubectl get pods | grep apply" does not read as "kubectl apply".
_SEG = r"[^\n|;&]*?"


_SEGMENTS = re.compile(r"[\n|;&]+")
_QUOTES = re.compile(r"[\\\"'`]")


def normalize(text: str) -> str:
    """Drop shell quotes and backslashes so 'kubectl' "apply" and ['kubectl','apply'] read as words."""
    return _QUOTES.sub("", text)


@dataclass(frozen=True)
class Rule:
    id: str
    scope: str
    tools: tuple[str, ...]
    pattern: re.Pattern[str] | None = None
    field: str = "command"
    description: str = ""
    unless: re.Pattern[str] | None = None

    def applies_to(self, tool_name: str) -> bool:
        return any(fnmatch.fnmatchcase(tool_name, pat) for pat in self.tools)

    def matches(self, tool_name: str, args: dict[str, Any]) -> bool:
        if not self.applies_to(tool_name):
            return False
        if self.pattern is None:
            return True
        text = normalize(extract_text(args, self.field))
        if not text or self.pattern.search(text) is None:
            return False
        if self.unless is None:
            return True
        # An exception applies per command segment: one safe segment never cancels another.
        return any(self.pattern.search(seg) and not self.unless.search(seg) for seg in _SEGMENTS.split(text))


DEFAULT_RULES: list[dict[str, Any]] = [
    {
        "id": "k8s-write",
        "scope": "k8s-prod",
        "tools": ["terminal"],
        "match": rf"\b(kubectl|oc)\b{_SEG}\b(apply|create|delete|patch|replace|scale|edit|label|annotate|"
                 rf"drain|cordon|uncordon|taint|expose|autoscale|rollout\s+(restart|undo|pause|resume)|set\s+\w+)\b",
        "description": "kubectl / oc commands that change cluster state",
    },
    {
        "id": "k8s-exec",
        "scope": "k8s-prod",
        "tools": ["terminal"],
        "match": rf"\b(kubectl|oc)\b{_SEG}\b(exec|cp|debug|attach|run)\b",
        "description": "remote execution in a cluster (can write); disable if you only exec for reads",
    },
    {
        "id": "helm-write",
        "scope": "k8s-prod",
        "tools": ["terminal"],
        "match": rf"\bhelm\b{_SEG}\b(install|upgrade|uninstall|rollback|delete)\b",
        "description": "helm releases",
    },
    {
        "id": "argocd-write",
        "scope": "k8s-prod",
        "tools": ["terminal"],
        "match": rf"\bargocd\b{_SEG}\bapp\s+(sync|delete|rollback|set|patch|terminate-op)\b",
        "description": "Argo CD application changes",
    },
    {
        "id": "iac-apply",
        "scope": "infra",
        "tools": ["terminal"],
        "match": rf"\b(tofu|terraform|terragrunt|pulumi)\b{_SEG}\b(apply|destroy|import|up)\b",
        "description": "infrastructure-as-code apply / destroy",
    },
    {
        "id": "git-push",
        "scope": "gitops-merge",
        "tools": ["terminal"],
        "match": rf"\bgit\b{_SEG}\bpush\b",
        "description": "every git push (v0.1 policy: refspecs, upstreams and wildcards are not parsed); "
                       "disable it and add a narrower rule if your agent pushes feature branches",
    },
    {
        "id": "pr-merge",
        "scope": "gitops-merge",
        "tools": ["terminal"],
        "match": rf"\b(gh|tea|glab)\b{_SEG}\b(pr|mr)\s+merge\b",
        "description": "pull / merge request merges from the CLI",
    },
    {
        # Code tools are opaque: match the binary name anywhere, not a verb.
        "id": "code-k8s",
        "scope": "k8s-prod",
        "tools": ["execute_code"],
        "field": "code",
        "match": r"\b(kubectl|oc|helm|argocd)\b",
        "description": "code that names a cluster CLI (opaque: any use counts)",
    },
    {
        "id": "code-iac",
        "scope": "infra",
        "tools": ["execute_code"],
        "field": "code",
        "match": r"\b(tofu|terraform|terragrunt|pulumi)\b",
        "description": "code that names an IaC CLI (opaque: any use counts)",
    },
    {
        "id": "code-git",
        "scope": "gitops-merge",
        "tools": ["execute_code"],
        "field": "code",
        "match": r"\bgit\b[\s\S]*\bpush\b|\b(gh|tea|glab)\b[\s\S]*\bmerge\b",
        "description": "code that names git and push, or a forge CLI and merge, anywhere (opaque)",
    },
    {
        "id": "self-protect",
        "scope": SELF_SCOPE,
        "tools": ["terminal", "execute_code", "write_file", "patch"],
        "field": "*",
        "match": r"plugin-data[/\\]+change-windows|plugins\.entries\.change-windows|"
                 r"\bhermes\s+(?:-\S+\s+)*change-windows\s+(?!(?:list|show|status|help)\b)\w|"
                 r"\bhermes\s+plugins\s+(?:disable|remove|uninstall|update|install)\s+(?:\S+\s+)*change-windows\b|"
                 r"\.hermes[/\\][^\s,]*config\.ya?ml[\s\S]*\bchange-windows\b|"
                 r"\bchange-windows\b[\s\S]*\.hermes[/\\][^\s,]*config\.ya?ml",
        "description": "the plugin's own store, settings and plugin state",
    },
]


class RuleError(ValueError):
    """A rule entry that cannot be compiled."""


def extract_text(args: dict[str, Any], field_name: str) -> str:
    """The text a rule reads; shell line continuations are joined so a split command still matches."""
    if not isinstance(args, dict):
        return ""
    if field_name == "*":
        try:
            text = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(args)
        # JSON escapes a real newline as "\n": read it as one.
        text = text.replace("\\n", "\n")
    else:
        value = args.get(field_name)
        text = value if isinstance(value, str) else ""
    return text.replace("\\\n", " ")


def compile_rule(raw: dict[str, Any]) -> Rule:
    if not isinstance(raw, dict):
        raise RuleError(f"rule must be a mapping, got {type(raw).__name__}")
    rule_id = str(raw.get("id") or "").strip()
    scope = str(raw.get("scope") or "").strip()
    tools = raw.get("tools") or ([raw["tool"]] if raw.get("tool") else [])
    if isinstance(tools, str):
        tools = [tools]
    if not rule_id or not scope or not tools:
        raise RuleError(f"rule needs id, scope and tools: {raw!r}")
    try:
        pattern = re.compile(raw["match"]) if raw.get("match") else None
        unless = re.compile(raw["unless"]) if raw.get("unless") else None
    except re.error as exc:
        raise RuleError(f"rule {rule_id}: bad regular expression: {exc}") from exc
    return Rule(
        id=rule_id,
        scope=scope,
        tools=tuple(str(t) for t in tools),
        pattern=pattern,
        field=str(raw.get("field") or "command"),
        description=str(raw.get("description") or ""),
        unless=unless,
    )


@dataclass
class RuleSet:
    rules: list[Rule] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def match(self, tool_name: str, args: dict[str, Any]) -> list[Rule]:
        return [r for r in self.rules if r.matches(tool_name, args)]


def build_ruleset(
    extra_rules: Iterable[dict[str, Any]] | None = None,
    disabled: Iterable[str] | None = None,
    *,
    include_defaults: bool = True,
) -> RuleSet:
    """Defaults plus the user's extra rules, minus disabled ids. ``self-protect`` cannot be disabled.

    A bad extra rule is reported in ``errors`` and skipped; the defaults still load, so a typo in
    the settings never switches the guard off.
    """
    disabled_ids = {str(d) for d in (disabled or [])} - {"self-protect"}
    out = RuleSet()
    raw_rules = list(DEFAULT_RULES) if include_defaults else [r for r in DEFAULT_RULES if r["id"] == "self-protect"]
    raw_rules += list(extra_rules or [])
    seen: set[str] = set()
    for raw in raw_rules:
        try:
            rule = compile_rule(raw)
        except RuleError as exc:
            out.errors.append(str(exc))
            continue
        if rule.id in disabled_ids or rule.id in seen:
            continue
        seen.add(rule.id)
        out.rules.append(rule)
    return out
