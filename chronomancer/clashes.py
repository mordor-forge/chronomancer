"""Where planned work clashes, and the one place that says so in words.

Two spans clash when they overlap in time and share a label that allows one card at a time.
Two overlapping spans of one profile that share no such label are only "busy": a softer mark.
Neither refuses a move (docs/adr/0001): the result names the clash and the mover decides.

Every surface (the agent tools, the Desktop page) takes its sentences from ``describe``. A test
fails when a surface keeps its own copy.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Iterable, Optional

CLASH, BUSY = "clash", "busy"
STEP_S = 15 * 60
SEARCH_S = 14 * 86400
_TITLE = 60


@dataclass(frozen=True)
class Span:
    """The time one card takes in the plan."""
    task_id: str
    title: str
    assignee: str
    start: int
    end: int
    locks: frozenset = frozenset()   # the card's labels that allow one card at a time


@dataclass(frozen=True)
class Clash:
    kind: str                # CLASH or BUSY
    a: Span
    b: Span
    start: int               # the shared time
    end: int
    labels: tuple            # the shared one-at-a-time labels; empty for BUSY

    @property
    def seconds(self) -> int:
        return self.end - self.start

    def involves(self, task_id: str) -> bool:
        return task_id in (self.a.task_id, self.b.task_id)

    def other(self, task_id: str) -> Span:
        return self.b if self.a.task_id == task_id else self.a


def find(spans: Iterable[Span]) -> list[Clash]:
    """Every pair of spans that clashes or is busy, earliest first."""
    ordered = sorted(spans, key=lambda s: (s.start, s.task_id))
    out: list[Clash] = []
    for i, a in enumerate(ordered):
        for b in ordered[i + 1:]:
            if b.start >= a.end:
                break  # ordered by start: no later span reaches into ``a``
            start, end = max(a.start, b.start), min(a.end, b.end)
            if end <= start or a.task_id == b.task_id:
                continue
            shared = tuple(sorted(a.locks & b.locks))
            if shared:
                out.append(Clash(CLASH, a, b, start, end, shared))
            elif a.assignee and a.assignee == b.assignee:
                out.append(Clash(BUSY, a, b, start, end, ()))
    return out


def against(candidate: Span, spans: Iterable[Span]) -> list[Clash]:
    """What one candidate span would clash with among the other cards' spans."""
    others = [s for s in spans if s.task_id != candidate.task_id]
    return [c for c in find([candidate, *others]) if c.involves(candidate.task_id)]


def next_free(candidate: Span, spans: Iterable[Span], *, step_s: int = STEP_S, search_s: int = SEARCH_S) -> Optional[int]:
    """The earliest start at or after the candidate's, on a ``step_s`` grid, where it clashes with
    nothing. Busy marks do not count. None when the next two weeks hold no such start."""
    others = [s for s in spans if s.task_id != candidate.task_id and s.locks & candidate.locks]
    length = candidate.end - candidate.start
    start, limit = candidate.start, candidate.start + search_s
    while start <= limit:
        blocker = max((s.end for s in others if s.start < start + length and start < s.end), default=None)
        if blocker is None:
            return start
        start = max(start + step_s, -(-blocker // step_s) * step_s)  # the next grid step at or after the blocker's end
    return None


def short(title: str, limit: int = _TITLE) -> str:
    text = " ".join(str(title or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def minutes_text(seconds: int) -> str:
    minutes = max(1, round(seconds / 60))
    hours, rest = divmod(minutes, 60)
    if not hours:
        return f"{minutes} min"
    if hours >= 48:  # days and hours are enough from two days on
        days, hours = divmod(hours, 24)
        return f"{days} d" if not hours else f"{days} d {hours} h"
    return f"{hours} h" if not rest else f"{hours} h {rest} min"


def describe(clash: Clash, task_id: str, span_text: Callable[[int, int], str]) -> str:
    """One sentence for the card ``task_id`` about the other card in the clash."""
    other = clash.other(task_id)
    who = other.assignee or "no profile"
    what = f'"{short(other.title)}" ({who}, {span_text(other.start, other.end)})'
    if clash.kind == CLASH:
        labels = " and ".join(clash.labels)
        return (f"Clashes with {what}: both carry {labels}, which allows one card at a time. "
                f"They overlap for {minutes_text(clash.seconds)}.")
    return f"{who} is also planned on {what}. They overlap for {minutes_text(clash.seconds)}."


def moved(span: Span, start: int) -> Span:
    return replace(span, start=start, end=start + (span.end - span.start))
