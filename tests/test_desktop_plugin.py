"""The Desktop half (``desktop/plugin.js``): what it may import, and what it registers and renders.

The static checks need nothing. The behaviour checks run the file in Node against stand-ins for
the three modules the Desktop app provides (``desktop_harness.mjs``); they are skipped without
``node``. Nothing here starts the Desktop app, so layout and theming are not covered.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "desktop" / "plugin.js"
SOURCE = PLUGIN.read_text(encoding="utf-8")
ALLOWED = {"@hermes/plugin-sdk", "react", "react/jsx-runtime"}
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _code():
    """The source without comments, so prose cannot satisfy or trip a check."""
    return re.sub(r"/\*.*?\*/|(?<![:'\"\w])//[^\n]*", "", SOURCE, flags=re.S)


def test_imports_only_the_three_allowed_specifiers():
    code = _code()
    static = re.findall(r"""^\s*import\s+(?:[^'";]*?\sfrom\s*)?['"]([^'"]+)['"]""", code, flags=re.M)
    assert set(static) == ALLOWED and len(static) == 3
    assert not re.search(r"\bimport\s*\(", code), "no dynamic import"
    assert not re.search(r"\brequire\s*\(", code) and "export * from" not in code


def test_is_plain_esm_without_jsx_or_private_app_surface():
    code = _code()
    assert re.search(r"^export default \{", code, flags=re.M)
    assert not re.search(r"<[A-Za-z][\w.]*(\s[^<>]*)?/?>|</[A-Za-z]", code), "JSX does not parse in an uncompiled plugin"
    for forbidden in ("hermesDesktop", "localStorage", "eval(", "new Function", "innerHTML",
                      "ctx.socket", "XMLHttpRequest", "WebSocket", "@/"):
        assert forbidden not in code, forbidden
    assert not re.search(r"\b(window|document|self|top|parent)\.[A-Za-z_$]", code), "no browser globals"
    assert not re.search(r"(?<![\w.])fetch\s*\(", code), "the backend is reached through ctx.rest only"
    assert not re.search(r"\b(setInterval|setTimeout|addEventListener)\s*\(", code), "no untracked timers or listeners"


def test_has_no_hardcoded_colours():
    code = _code()
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", code)
    assert not re.search(r"\b(rgba?|hsla?|oklch|oklab|color-mix)\s*\(", code)
    colour_values = re.findall(r"\b(?:color|background|backgroundColor|borderColor|fill|stroke)\s*:\s*([^,}\n]+)", code)
    assert colour_values and all(v.strip().startswith(("C.", "'transparent'", "'var(--ui-")) for v in colour_values), colour_values
    assert set(re.findall(r"var\((--[\w-]+)\)", code)) <= {
        "--ui-text-primary", "--ui-text-secondary", "--ui-text-tertiary", "--ui-stroke-secondary"}


def test_polls_every_five_to_ten_seconds_and_strings_go_through_i18n():
    code = _code()
    [poll] = re.findall(r"POLL_MS = (\d+)", code)
    assert 5000 <= int(poll) <= 10000 and code.count("refetchInterval: POLL_MS") == 2
    assert "ctx.i18n.register({ en: EN })" in code
    literal_children = re.findall(r"children:\s*'([^']*[A-Za-z]{2}[^']*)'", code)
    assert literal_children == [], "user-visible text belongs in the EN bundle"
    assert "mobile" not in SOURCE.lower()  # no claim about the Hermes mobile app


@needs_node
def test_node_accepts_the_file_as_a_module():
    result = subprocess.run([NODE, "--check", str(PLUGIN)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


@pytest.fixture(scope="module")
def ran():
    if NODE is None:
        pytest.skip("node is not installed")
    result = subprocess.run([NODE, str(Path(__file__).with_name("desktop_harness.mjs")), str(PLUGIN)],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout)


def test_registers_the_four_surfaces_and_the_badges(ran):
    assert ran["id"] == "change-windows"
    surfaces = {c["id"]: c for c in ran["contributions"]}
    assert surfaces["page"]["area"] == "routes" and surfaces["page"]["data"] == {"path": "/change-windows"}
    assert surfaces["nav"]["area"] == "sidebar.nav" and surfaces["nav"]["data"]["label"] == "Change windows"
    assert surfaces["nav"]["data"]["path"] == "/change-windows" and surfaces["nav"]["data"]["codicon"]
    assert surfaces["chip"]["area"] == "statusBar.right" and surfaces["chip"]["render"]
    assert surfaces["open"]["area"] == "palette" and surfaces["open"]["data"]["label"] == "Open change windows"
    assert ran["badgeArea"] == "kanban.card.badges"
    assert ran["navigations"] == ["/change-windows", "/change-windows"]  # the chip and the palette command
    assert ran["missingKeys"] == [] and ran["disposers"] == 1
    assert ran["pollMs"] == [7000] and ran["prefetch"] == ["/windows"]


def test_wide_layout_is_a_table_grouped_by_state(ran):
    wide = ran["wide"]
    assert "table" in wide["types"]
    order = [wide["texts"].index(h) for h in ("Waiting for your decision", "Approved", "Open now",
                                              "Closed, expired or declined")]
    assert order == sorted(order)
    for text in ("Overlaps WAAAAA2", "Cards: t_pending", "42 min left", "starts in 1 h", "ended 1 h ago",
                 "decided in Hermes Desktop", "decided in Telegram", "decided in the Mini App",
                 "Recent decisions", "Recent matched calls", "Last 20 · metadata only", "blocked · terminal"):
        assert text in wide["texts"], text
    assert any("approved in Hermes Desktop · by desktop" in t for t in wide["texts"])
    assert [b["label"] for b in wide["buttons"]] == ["Approve", "Decline", "Close now", "Close now"]


def test_narrow_layout_is_stacked_cards_with_large_buttons(ran):
    narrow = ran["narrow"]
    assert "table" not in narrow["types"]
    assert [(b["label"], b["size"]) for b in narrow["buttons"]] == [
        ("Approve", "touch"), ("Decline", "touch"), ("Close now", "touch"), ("Close now", "touch")]
    assert "Overlaps WAAAAA2" in narrow["texts"] and "Recent decisions" in narrow["texts"]


def test_approval_off_hides_approve_and_decline_but_keeps_close(ran):
    off = ran["approvalOff"]
    assert [b["label"] for b in off["buttons"]] == ["Close now", "Close now"]
    assert any("approval.desktop: false" in t for t in off["texts"])
    assert not any("trusts this Desktop session" in t for t in off["texts"])


def test_loading_error_and_empty_states(ran):
    assert "Loading change windows…" in ran["loading"]["texts"] and ran["loading"]["buttons"] == []
    assert ran["loadErrorProps"] == [{"title": "Change windows are unavailable", "description": "Plugin not found"}]
    assert [b["label"] for b in ran["loadError"]["buttons"]] == ["Retry"]
    assert ran["empty"] == ["No change windows"]


def test_each_action_asks_first_and_posts_with_an_idempotency_key(ran):
    assert ran["dialog"]["open"] is True and ran["dialog"]["title"] == 'Approve "name WAAAAA1"?'
    assert "scope k8s-prod" in ran["dialog"]["description"] and ran["dialog"]["destructive"] is False
    assert ran["closeDialog"] == {"title": 'Close "name WAAAAA2" now?', "destructive": True}
    assert ran["declineDialog"] == {"title": 'Decline "name WAAAAA1"?', "destructive": True}
    first, retry, reopened = ran["approveCalls"]
    for call in (first, retry, reopened):
        assert call["path"] == "/windows/WAAAAA1/approve" and call["opts"]["method"] == "POST"
        assert re.fullmatch(r"[A-Za-z0-9_-]{16,128}", call["opts"]["body"]["key"])  # the backend's rule
    assert first["opts"]["body"]["key"] == retry["opts"]["body"]["key"], "a retry in the same dialog replays"
    assert reopened["opts"]["body"]["key"] != first["opts"]["body"]["key"], "a new dialog is a new request"
    assert ran["dialogClosed"] is True and ran["invalidated"] >= 3
    assert {"kind": "success", "message": "Window approved."} in ran["notices"]


def test_a_refused_action_shows_the_servers_reason(ran):
    assert ran["refused"] == "window WAAAAA1 overlaps approved WAAAAA2"
    assert ran["unreachable"] == "The action failed. Check the window list and retry."


def test_status_bar_chip_shows_the_open_window_else_the_next_else_nothing(ran):
    assert ran["chipOpen"]["text"].endswith("infra open · 42 min left") and ran["chipOpen"]["icon"] == "unlock"
    assert "WAAAAA3" in ran["chipOpen"]["tip"]
    assert ran["chipNext"]["text"].endswith("k8s-prod window in 1 h") and "WAAAAA2" in ran["chipNext"]["tip"]
    assert ran["chipNone"] is None and ran["chipLoading"] is None


def test_kanban_badges_show_state_and_time_within_the_boards_limits(ran):
    badges = ran["badges"]
    assert badges["t_open"] == {"label": "Open · 42 min left", "tone": "green",
                                "tooltip": "WAAAAA3 · name WAAAAA3 · span WAAAAA3"}
    assert badges["t_next"]["label"] == "Window in 1 h" and badges["t_next"]["tone"] == "blue"
    assert badges["t_pending"]["label"] == "Proposed, in 1 h" and badges["t_pending"]["tone"] == "yellow"
    assert badges["t_done"]["label"] == "Window declined" and badges["t_done"]["tone"] == "neutral"
    assert badges["t_both"]["label"] == badges["t_open"]["label"], "an open window wins over an upcoming one"
    assert badges["t_unknown"] is None and ran["badgesAfterEmpty"] is None
    assert all(len(b["label"]) <= 24 and set(b) == {"label", "tone", "tooltip"} for b in badges.values() if b)
    assert ran["badgeRegistrations"] == 1, "unchanged poll results do not repaint the board"


def test_longest_badge_labels_fit_the_24_character_limit():
    # hours run to 47 h 59 min before the label switches to days
    for template in ("Open · {} left", "Window in {}", "Proposed, in {}"):
        assert len(template.format("47 h 59 min")) <= 24 and len(template.format("29 d 23 h")) <= 24
