"""Clashes: two slots that overlap in time and share a one-at-a-time label."""
from chronomancer import clashes
from chronomancer.clashes import BUSY, CLASH, Span

H = 3600
PROD = frozenset({"cluster:prod"})


def span(task_id, start, end, assignee="builder", locks=frozenset()):
    return Span(task_id, f"Card {task_id}", assignee, start * H, end * H, locks)


def test_overlap_with_a_shared_one_at_a_time_label_is_a_clash():
    found = clashes.find([span("a", 9, 11, "builder", PROD), span("b", 10, 12, "reviewer", PROD)])
    assert [(c.kind, c.labels, c.start, c.end) for c in found] == [(CLASH, ("cluster:prod",), 10 * H, 11 * H)]


def test_touching_slots_do_not_clash():
    assert clashes.find([span("a", 9, 10, locks=PROD), span("b", 10, 11, locks=PROD)]) == []


def test_overlap_of_one_profile_without_a_shared_label_is_only_busy():
    found = clashes.find([span("a", 9, 11), span("b", 10, 12)])
    assert [c.kind for c in found] == [BUSY]
    assert clashes.find([span("a", 9, 11, "builder"), span("b", 10, 12, "reviewer")]) == []


def test_a_shared_label_wins_over_busy_and_only_shared_labels_are_named():
    a = span("a", 9, 11, "builder", frozenset({"cluster:prod", "db:main"}))
    b = span("b", 10, 12, "builder", frozenset({"cluster:prod", "repo:web"}))
    (found,) = clashes.find([a, b])
    assert found.kind == CLASH and found.labels == ("cluster:prod",)


def test_every_pair_is_found_once_even_with_a_long_slot_first():
    spans = [span("long", 8, 20, locks=PROD), span("b", 9, 10, locks=PROD), span("c", 15, 16, locks=PROD),
             span("d", 21, 22, locks=PROD)]
    pairs = {(c.a.task_id, c.b.task_id) for c in clashes.find(spans)}
    assert pairs == {("long", "b"), ("long", "c")}


def test_against_reports_only_the_candidate_and_ignores_its_own_old_span():
    others = [span("a", 9, 11, locks=PROD), span("b", 10, 12, locks=PROD), span("me", 9, 10, locks=PROD)]
    found = clashes.against(span("me", 11, 13, locks=PROD), others)
    assert [c.other("me").task_id for c in found] == ["b"]


def test_next_free_skips_to_the_end_of_what_blocks_and_lands_on_the_grid():
    others = [span("a", 9, 11, locks=PROD), span("b", 11, 12, locks=PROD)]
    candidate = Span("me", "Me", "builder", 9 * H + 7 * 60, 10 * H + 7 * 60, PROD)   # 09:07, one hour
    assert clashes.next_free(candidate, others) == 12 * H
    assert clashes.next_free(candidate, []) == candidate.start                         # free where it is


def test_next_free_ignores_slots_without_a_shared_label():
    others = [span("a", 9, 11, locks=frozenset({"db:main"})), span("b", 9, 11, "builder")]
    assert clashes.next_free(span("me", 9, 10, locks=PROD), others) == 9 * H


def test_next_free_gives_up_after_the_search_span():
    wall = [span("a", 0, 24 * 30, locks=PROD)]
    assert clashes.next_free(span("me", 1, 2, locks=PROD), wall) is None


def test_the_sentence_names_the_other_card_the_label_and_the_overlap():
    a, b = span("a", 9, 11, "builder", PROD), span("b", 10, 12, "reviewer", PROD)
    (found,) = clashes.find([a, b])
    text = clashes.describe(found, "a", lambda s, e: f"{s // H}-{e // H}")
    assert text == ('Clashes with "Card b" (reviewer, 10-12): both carry cluster:prod, which allows one card '
                    "at a time. They overlap for 1 h.")
    busy = clashes.find([span("a", 9, 11), span("b", 10, 10.5)])[0]
    assert clashes.describe(busy, "b", lambda s, e: "x") == 'builder is also planned on "Card a" (builder, x). They overlap for 30 min.'


def test_short_titles_and_durations():
    assert clashes.short("a" * 80) == "a" * 59 + "…"
    assert [clashes.minutes_text(s) for s in (20, 60, 45 * 60, 3600, 5400, 7200)] == [
        "1 min", "1 min", "45 min", "1 h", "1 h 30 min", "2 h"]
