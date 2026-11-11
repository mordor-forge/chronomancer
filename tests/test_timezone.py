"""Time zones: the plugin must use an IANA zone (with DST), also when ``timezone`` is not set."""
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from chronomancer import tools
from chronomancer.timezones import local_tz, system_zone_name

UTC = timezone.utc


def utc(ts):
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M")


def test_naive_times_follow_dst_of_the_configured_zone():
    tz = local_tz("Europe/Prague")
    # 2027-03-28 is the spring change in this zone: +01:00 before, +02:00 after.
    assert utc(tools.parse_time("2027-03-27T09:00", tz, 0)) == "2027-03-27T08:00"
    assert utc(tools.parse_time("2027-03-29T09:00", tz, 0)) == "2027-03-29T07:00"
    assert utc(tools.parse_time("2027-11-01T09:00", tz, 0)) == "2027-11-01T08:00"
    # An explicit offset always wins.
    assert utc(tools.parse_time("2027-03-29T09:00+01:00", tz, 0)) == "2027-03-29T08:00"


def test_slot_planned_before_a_dst_change_for_a_time_after_it(store, kanban):
    import json

    from chronomancer.board import Board
    from chronomancer.planner import Planner
    tz = local_tz("Europe/Prague")
    now = int(datetime(2027, 3, 26, 12, 0, tzinfo=UTC).timestamp())  # winter time
    kanban.add("t_1", status="blocked")
    planner = Planner(store, Board(kanban), setting=lambda k, d=None: d, tz=lambda: tz, clock=lambda: now)
    t = tools.Tools(planner, actor=lambda: "agent:test", clock=lambda: now)
    out = json.loads(t.slot({"card": "t_1", "start": "2027-03-29T09:00", "estimate_minutes": 60}))
    assert out["ok"], out
    assert out["slot"]["start"] == "2027-03-29T09:00+02:00" and out["slot"]["end"] == "2027-03-29T10:00+02:00"
    slot = store.get_slot("", "t_1")
    assert utc(slot.start_ts) == "2027-03-29T07:00" and slot.estimate_s == 3600


@pytest.mark.parametrize("env,expected", [
    ({"TZ": "Europe/Prague"}, "Europe/Prague"),
    ({"TZ": ":America/New_York"}, "America/New_York"),
    ({"TZ": "/usr/share/zoneinfo/Asia/Tokyo"}, "Asia/Tokyo"),
])
def test_system_zone_from_the_tz_variable(env, expected, tmp_path):
    assert system_zone_name(environ=env, localtime=tmp_path / "missing") == expected


def test_system_zone_from_the_localtime_link(tmp_path):
    target = tmp_path / "usr" / "share" / "zoneinfo" / "Europe" / "Prague"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"")
    link = tmp_path / "localtime"
    link.symlink_to(target)
    assert system_zone_name(environ={}, localtime=link) == "Europe/Prague"
    assert system_zone_name(environ={"TZ": "CET-1CEST"}, localtime=link) == "Europe/Prague"  # POSIX form: skip
    assert system_zone_name(environ={}, localtime=tmp_path / "missing") is None


def test_unset_timezone_uses_the_system_iana_zone(tmp_path):
    tz = local_tz("", environ={"TZ": "Europe/Prague"}, localtime=tmp_path / "missing")
    assert isinstance(tz, ZoneInfo) and tz.key == "Europe/Prague"
    assert local_tz(None, environ={"TZ": "Europe/Prague"}, localtime=tmp_path / "missing").key == "Europe/Prague"


def test_unknown_or_unresolvable_zone_falls_back_and_warns_once(tmp_path, caplog):
    import logging
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            a = local_tz("Mars/Olympus", environ={"TZ": "Europe/Prague"}, localtime=tmp_path / "missing")
            b = local_tz("", environ={}, localtime=tmp_path / "missing-too")
    assert a.key == "Europe/Prague"       # a bad setting falls back to the system zone
    assert b.utcoffset(datetime.now()) is not None  # last resort: today's fixed offset
    assert caplog.text.count("Mars/Olympus") == 1 and caplog.text.count("fixed offset") == 1


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="needs time.tzset")
def test_plugin_entry_with_no_timezone_setting_is_dst_aware(plugin_entry, kanban, monkeypatch):
    import json
    monkeypatch.setenv("TZ", "Europe/Prague")
    time.tzset()
    try:
        kanban.add("t_1", status="blocked")
        slot = plugin_entry(dispatch=kanban).tools["chrono_slot"]
        starts = {}
        for month in ("01", "07"):
            out = json.loads(slot({"card": "t_1", "start": f"2999-{month}-15T09:00", "estimate_minutes": 30}))
            assert out["ok"], out
            starts[month] = out["slot"]["start"]
        assert starts == {"01": "2999-01-15T09:00+01:00", "07": "2999-07-15T09:00+02:00"}
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()
