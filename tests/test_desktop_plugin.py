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
    """Every colour is a theme variable of the app, named once in the ``C`` table."""
    code = _code()
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", code.replace("url(#cw-hatch", ""))
    assert not re.search(r"\b(rgba?|hsla?|oklch|oklab|color-mix)\s*\(", code)
    table = re.search(r"const C = \{(.*?)\n\}", code, flags=re.S).group(1)
    values = dict(re.findall(r"(\w+): '([^']+)'", table))
    assert values and all(re.fullmatch(r"var\(--ui-[\w-]+\)|url\(#cw-hatch(-warn)?\)|none|transparent", v)
                          for v in values.values()), values
    colour_values = re.findall(r"\b(?:color|background|backgroundColor|borderColor|fill|stroke)\s*:\s*([^,}\n]+)", code)
    assert colour_values and all(v.strip().startswith(("C.", "C[", "'var(--ui-")) for v in colour_values), colour_values
    # borders and shadows take their colour from the table too
    for shorthand in re.findall(r"\b(?:border(?:Top|Bottom|Left|Right)?|boxShadow|outline)\s*:\s*(`[^`]*`|'[^']*')", code):
        assert "${C." in shorthand or shorthand in ("'none'",) or re.fullmatch(r"'?0'?", shorthand), shorthand
    # Text is always the primary tone: the app's secondary and tertiary tones are under 4.5:1 in some
    # themes (Catppuccin Latte). The secondary tone is used for shapes only (3:1).
    text_styles = re.findall(r"\b(?:color)\s*:\s*(C\.\w+)", code)
    assert set(text_styles) <= {"C.primary", "C.secondary", "C.green", "C.yellow", "C.red"}
    assert "C.tertiary" not in code and "svgText(10, 'secondary'" not in code
    assert set(re.findall(r"var\((--[\w-]+)[,)]", code)) <= {
        "--ui-text-primary", "--ui-text-secondary", "--ui-stroke-secondary",
        "--ui-accent", "--ui-green", "--ui-yellow", "--ui-red", "--dt-font-mono"}


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


def test_the_door_is_one_sentence_with_a_glyph_in_every_state(ran):
    """Is the door open now, and until when? Words and a glyph, never colour alone."""
    assert ran["door"] == {"kind": "open", "mode": "enforce", "role": "status", "icon": "unlock", "lines": [
        "Open: name WAAAAA3, 42 min left",
        "Matched writes in scope infra are allowed until 10:42. Other scopes stay blocked. "
        "1 proposal waits for your decision."]}
    assert ran["doorNext"]["icon"] == "lock" and ran["doorNext"]["lines"] == [
        "Closed: production writes are blocked",
        "Next: 11:00 name WAAAAA2 (approved), in 1 h. 1 proposal waits for your decision."]
    assert ran["doorClosed"]["lines"] == ["Closed: production writes are blocked",
                                          "No approved window ahead. 1 proposal waits for your decision."]


def test_the_door_does_not_say_blocked_when_the_guard_only_warns_or_is_off(ran):
    assert ran["doorWarn"]["lines"][0] == "Warn mode: production writes are recorded, not blocked"
    assert ran["doorWarn"]["icon"] == "warning" and ran["chipWarn"]["text"].endswith("warn only")
    assert ran["doorOff"]["lines"] == ["Guard off: production writes are not checked", "No approved window ahead."]


def test_the_runway_draws_today_and_tomorrow_with_bars_bands_night_and_a_needle(ran):
    runway = ran["runway"]
    assert runway["kind"] == "wide" and runway["days"] == 2 and runway["role"] == "group" and runway["label"]
    bars = {b["id"]: b for b in runway["bars"]}
    assert {i: b["kind"] for i, b in bars.items()} == {
        "WAAAAA1": "pending", "WAAAAA2": "approved", "WAAAAA3": "open", "WAAAAA6": "past", "WAAAAA7": "approved"}
    # declined and expired proposals never were windows: no bar
    assert "WAAAAA4" not in bars and "WAAAAA5" not in bars
    assert runway["bands"] == [{"label": "Overlap: 'name WAAAAA1' and 'name WAAAAA2', 11:00-12:00, 1 h", "strong": "no",
                                "text": ["⚠ overlap 1 h"]}]
    assert runway["night"] == 4 and runway["needle"] == ["10:00"]  # the needle is on today only
    assert runway["patterns"] == ["cw-hatch", "cw-hatch-warn"]  # proposals and overlaps are hatched, not only coloured
    for text in ("Today · Tue 10 Nov", "Tomorrow · Wed 11 Nov", "now 10:00", "night", "open now", "proposed", "overlap"):
        assert text in ran["wide"]["texts"], text


def test_runway_bars_are_keyboard_buttons_that_read_out_and_jump_to_the_card(ran):
    for bar in ran["runway"]["bars"]:
        assert bar["role"] == "button" and bar["tabIndex"] == 0 and bar["keys"] and bar["lit"] == "no"
    assert ran["runway"]["bars"][1]["label"] == "name WAAAAA3, open, span WAAAAA3, 42 min left"
    assert ran["runway"]["readout"].startswith("Focus or hover a bar")
    assert ran["focused"]["readout"] == "name WAAAAA2 · approved · span WAAAAA2 · starts in 1 h · scope k8s-prod"
    assert ran["picked"] == {"lit": ["WAAAAA3"], "prevented": 1}  # Enter picks; other keys are left alone
    assert ran["articles"] == {"type": "article", "tabIndex": -1, "label": "name WAAAAA3"}  # focusable from the runway


def test_the_runway_is_a_ladder_when_narrow_and_is_drawn_at_its_measured_width(ran):
    ladder = ran["ladder"]
    assert ladder["kind"] == "ladder" and ladder["days"] == 2 and ladder["needle"] == ["10:00"]
    assert {b["id"] for b in ladder["bars"]} == {b["id"] for b in ran["runway"]["bars"]}
    assert ladder["bands"][0]["text"] == ["⚠ overlap 1 h"]
    assert ran["squeezed"] == "ladder"  # a wide window with a narrow content column
    assert ran["measured"].startswith("0 0 900 ") and ran["runway"]["viewBox"].startswith("0 0 960 ")


def test_windows_are_cards_grouped_by_state_with_the_open_one_first(ran):
    wide = ran["wide"]
    assert "table" not in wide["types"] and ran["groups"] == ["open", "pending", "approved", "finished"]
    order = [wide["texts"].index(h) for h in ("Open now", "Waiting for your decision", "Approved",
                                              "Closed, expired or declined")]
    assert order == sorted(order)
    for text in ("span WAAAAA3 · 42 min left", "span WAAAAA1 · starts in 1 h", "span WAAAAA5 · ended 1 h ago",
                 "scope k8s-prod", "scope infra", "Proposed by ops · exclusive", "reason WAAAAA1"):
        assert text in wide["texts"], text
    assert [b["label"] for b in wide["buttons"]] == ["Close now", "Approve", "Decline", "Close now", "Close now"]
    assert all(b["size"] == "touch" for b in wide["buttons"])
    # the id is secondary and copyable
    assert ran["copies"] == ["WAAAAA3", "WAAAAA1", "WAAAAA2", "WAAAAA7", "WAAAAA4", "WAAAAA5", "WAAAAA6"]


def test_each_card_carries_a_decision_stamp_in_the_plugins_time_zone(ran):
    stamps = ran["stamps"]
    assert stamps["WAAAAA3"].endswith("Approved in Hermes Desktop, 09:45")  # today: the time alone
    assert stamps["WAAAAA2"].endswith("Approved in Telegram, 09:10")
    assert stamps["WAAAAA4"].endswith("Declined in the Mini App, Mon 09 Nov 21:15")  # another day: with the day
    assert stamps["WAAAAA6"].endswith("Approved in Telegram, 07:30 · Closed 09:59")
    assert stamps["WAAAAA7"].endswith("Approved, 09:58")  # decided before the channel was recorded
    assert stamps["WAAAAA1"].endswith("Awaiting your decision") and stamps["WAAAAA5"].endswith("Expired without a decision")


def test_linked_cards_show_title_status_and_assignee_as_plain_text(ran):
    chips = {c["id"]: c for c in ran["cardChips"]}
    assert all(c["type"] == "button" for c in chips.values())
    # the title is untrusted: it is a jsx text child, so markup stays text
    assert chips["t_pending"]["text"].endswith("<b>Upgrade Postgres</b> · blocked · dbat_pending")
    assert chips["t_open"]["text"].endswith("t_open") and "·" not in chips["t_open"]["text"]  # no details: the id alone
    assert "Open the Kanban board (card t_pending)" in ran["wide"]["texts"]


def test_an_overlap_is_a_sentence_on_a_button_that_opens_the_conflict_on_the_other_card(ran):
    assert ran["overlapChips"] == [
        {"to": "WAAAAA2", "type": "button", "text": "warningOverlaps 'name WAAAAA2' 11:00-12:00, 1 h", "expanded": False},
        {"to": "WAAAAA1", "type": "button", "text": "warningOverlaps 'name WAAAAA1' 11:00-12:00, 1 h", "expanded": False}]
    conflict = ran["conflict"]
    [panel] = conflict["panel"]
    assert panel["on"] == "WAAAAA2:WAAAAA1" and panel["role"] == "region"  # on the OTHER window's card
    assert panel["title"] == "This window and 'name WAAAAA1' overlap"
    assert panel["rows"] == [
        ["Other window", "name WAAAAA1 · pending · span WAAAAA1 · scope k8s-prod · proposed by dba WAAAAA1"],
        ["Shared span", "11:00-12:00, 1 h"],
        ["Rule", "same scope k8s-prod; both windows are exclusive"],
        ["What it means", "Approving 'name WAAAAA1' is refused while this window is approved."],
        ["Ways out", "Decline one of them, or ask the agent to propose another time."]]
    assert panel["buttons"] == ["Show 'name WAAAAA1'", "Close explanation"]
    # both cards and both bars are marked, the shared span is strong, everything else steps back
    assert conflict["lit"] == ["WAAAAA1", "WAAAAA2"]
    bars = {b["id"]: b for b in conflict["runway"]["bars"]}
    assert [i for i, b in bars.items() if b["lit"] == "yes"] == ["WAAAAA1", "WAAAAA2"]
    assert all(b["dim"] == (i not in ("WAAAAA1", "WAAAAA2")) for i, b in bars.items())
    assert conflict["runway"]["bands"][0]["strong"] == "yes"
    assert ["WAAAAA1", True] in conflict["expanded"]
    assert ran["conflictBack"] == {"panel": ["WAAAAA1:WAAAAA2"], "lit": ["WAAAAA1", "WAAAAA2"]}  # Show: back to the first card
    assert ran["conflictClosed"] == {"panels": 0, "bands": ["no"], "dim": False}


def test_the_ledger_is_one_log_of_decisions_and_calls_newest_first(ran):
    """Found in the real app: a window approved in Telegram and closed later read "closed in Telegram",
    and the moments were in the app's zone while the spans were in the plugin's."""
    ledger = ran["ledger"]
    assert [row["verdict"] for row in ledger] == ["blocked", "approved", "approved", "declined"]
    assert ledger[0]["icon"] == "circle-slash" and ledger[0]["cells"][2] == "blocked · terminal · k8s-prod · k8s-write"
    assert ledger[1]["cells"][0] == "Tue 10 Nov 13:45 UTC"  # the backend's moment, in its own column
    assert ledger[1]["cells"][2].startswith("approved in Hermes Desktop · name WAAAAA3 · by desktop")
    assert ledger[2]["cells"][2].startswith("approved in Telegram · name WAAAAA6 · by telegram:100200300 · closed later")
    assert not any("closed in Telegram" in cell for row in ledger for cell in row["cells"])
    assert ledger[3]["cells"][2].startswith("declined in the Mini App · name WAAAAA4") and ledger[3]["icon"] == "close"
    assert "UTC" not in ledger[3]["cells"][0]  # no `when` from an older backend: the local clock
    for text in ("Ledger", "Decisions and matched calls, newest first. Last 20 of each · metadata only"):
        assert text in ran["wide"]["texts"], text


def test_narrow_layout_keeps_every_action_with_large_buttons(ran):
    narrow = ran["narrow"]
    assert "table" not in narrow["types"]
    assert [(b["label"], b["size"]) for b in narrow["buttons"]] == [
        ("Close now", "touch"), ("Approve", "touch"), ("Decline", "touch"), ("Close now", "touch"), ("Close now", "touch")]
    assert "Overlaps 'name WAAAAA2' 11:00-12:00, 1 h" in narrow["texts"] and "Ledger" in narrow["texts"]


def test_an_older_backend_still_gets_the_door_the_cards_and_the_actions(ran):
    old = ran["oldBackend"]
    assert old["runway"] is None  # no day boundaries: no timeline, and no crash
    assert old["door"]["lines"][0].startswith("Open: name WAAAAA3, ")
    assert old["chips"] == ["warningWAAAAA2", "warningWAAAAA1"] and old["buttons"] == 5


def test_approval_off_hides_approve_and_decline_but_keeps_close(ran):
    off = ran["approvalOff"]
    assert [b["label"] for b in off["buttons"]] == ["Close now", "Close now", "Close now"]
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
    assert ran["closeDialog"] == {"title": 'Close "name WAAAAA3" now?', "destructive": True}
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


def test_status_bar_chip_says_the_door_in_two_words(ran):
    assert ran["chipOpen"]["text"].endswith("open, 42 min") and ran["chipOpen"]["icon"] == "unlock"
    assert ran["chipOpen"]["tip"].startswith("Change windows. Open: name WAAAAA3, 42 min left. Matched writes in scope infra")
    assert ran["chipNext"]["text"].endswith("next 11:00") and ran["chipNext"]["icon"] == "lock"
    assert "Next: 11:00 name WAAAAA2 (approved)" in ran["chipNext"]["tip"]
    assert ran["chipNone"]["text"].endswith("closed") and ran["chipNone"]["icon"] == "lock"  # closed is a state too
    assert ran["chipLoading"] is None


def test_kanban_badges_use_the_doors_words_within_the_boards_limits(ran):
    badges = ran["badges"]
    assert badges["t_open"] == {"label": "window open, 42 min", "tone": "green",
                                "tooltip": "name WAAAAA3 · span WAAAAA3 · WAAAAA3"}
    assert badges["t_next"]["label"] == "next 11:00" and badges["t_next"]["tone"] == "blue"
    assert badges["t_pending"]["label"] == "pending 11:00" and badges["t_pending"]["tone"] == "yellow"
    assert badges["t_done"]["label"] == "window declined" and badges["t_done"]["tone"] == "neutral"
    assert badges["t_both"]["label"] == badges["t_open"]["label"], "an open window wins over an upcoming one"
    assert badges["t_unknown"] is None and ran["badgesAfterEmpty"] is None
    assert all(len(b["label"]) <= 24 and set(b) == {"label", "tone", "tooltip"} for b in badges.values() if b)
    assert ran["badgeRegistrations"] == 1, "unchanged poll results do not repaint the board"


def test_longest_badge_labels_fit_the_24_character_limit():
    # hours run to 47 h 59 min before the label switches to days; a start on another day carries the day
    for template in ("window open, {}",):
        assert len(template.format("47 h 59 min")) <= 24 and len(template.format("29 d 23 h")) <= 24
    for template in ("next {}", "pending {}"):
        assert len(template.format("Wed 11 Nov 09:00")) <= 24
    labels = re.search(r"badge: \{(.*?)\n  \}", SOURCE, flags=re.S).group(1)
    assert "window open, ${d}" in labels and "next ${at}" in labels and "pending ${at}" in labels


def test_the_conflict_wording_is_the_backends_not_the_pages():
    """One copy of the explanation: change_windows/overlaps.py. The page lays out label, title, rows, pair, note."""
    code = _code()
    for words in ("Overlaps '", "Shared span", "Other window", "What it means", "Ways out", "This window and", "Overlap:"):
        assert words not in code, words
    for field in ("c.label", "c.title", "c.rows", "c.pair", "c.note"):
        assert field in code, field
