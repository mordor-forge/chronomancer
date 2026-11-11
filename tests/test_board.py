"""The board adapter: only the public kanban tools, cleaned text, short caches, honest errors."""
import json

import pytest

from chronomancer.board import Board, BoardError, clean


class Ticks:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_cards_are_listed_by_status_and_cached_briefly(kanban):
    for task_id, status in (("t_1", "ready"), ("t_2", "blocked"), ("t_3", "done"), ("t_4", "archived")):
        kanban.add(task_id, status=status, assignee="builder")
    ticks = Ticks()
    board = Board(kanban, list_ttl_s=8, clock=ticks)
    assert [c.task_id for c in board.cards("")] == ["t_1", "t_2"]              # done and archived are not open
    assert kanban.count("kanban_list") == 7                                    # one call per open status
    board.cards("")
    assert kanban.count("kanban_list") == 7                                    # cached
    ticks.now = 9
    board.cards("")
    assert kanban.count("kanban_list") == 14


def test_the_board_slug_is_passed_only_when_set(kanban):
    kanban.add("t_1")
    Board(kanban).card("", "t_1")
    Board(kanban).card("ops", "t_1")
    assert [args.get("board") for _, args in kanban.calls] == [None, "ops"]


def test_a_write_drops_the_cache_but_keeps_closed_cards(kanban):
    kanban.add("t_1", status="ready")
    kanban.add("t_done", status="done")
    board = Board(kanban, clock=Ticks())
    board.cards("")
    board.card("", "t_done")
    shows = kanban.count("kanban_show")
    board.park("", "t_1", "SCHEDULED_UNTIL=later")
    assert [c.status for c in board.cards("")] == ["scheduled"]                # read again after the write
    board.card("", "t_done")
    assert kanban.count("kanban_show") == shows                                # a done card does not change


def test_an_unknown_card_is_none_and_any_other_error_is_an_error(kanban):
    board = Board(kanban)
    assert board.card("", "t_9") is None
    kanban.refuse.add("kanban_show")
    with pytest.raises(BoardError, match="kanban_show t_9: kanban_show refused"):
        board.card("", "t_9", fresh=True)


def test_a_missing_tool_is_not_read_as_a_missing_card():
    board = Board(lambda tool, args: json.dumps({"error": f"Unknown tool: {tool}"}))
    with pytest.raises(BoardError, match="Unknown tool"):
        board.card("", "t_1")
    with pytest.raises(BoardError, match="Unknown tool"):
        board.cards("")


def test_no_dispatcher_and_raising_tools_are_board_errors(kanban):
    with pytest.raises(BoardError, match="not loaded"):
        Board(None).cards("")
    assert Board(None).available is False
    kanban.down = True
    with pytest.raises(BoardError, match=r"kanban_unblock failed \(RuntimeError\)"):
        Board(kanban).release("", "t_1")


def test_titles_are_one_line_of_plain_text(kanban):
    kanban.add("t_1", title="Deploy\nweb \u202eevil\u200b  now " + "x" * 300)
    card = Board(kanban).card("", "t_1")
    assert card.title.startswith("Deploy web evil now xxx") and len(card.title) == 200 and card.title.endswith("…")
    assert clean(None, 10) == "" and clean("  a \t b ", 10) == "a b"
