"""Regression tests for v2.5.1 message-formatting and DST fixes."""
from datetime import datetime, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo


# ---------------------------------------------------------------------------
# DST-aware market open
# ---------------------------------------------------------------------------
def test_market_open_utc_in_summer_returns_1330():
    """May = EDT (UTC-4): NYSE 09:30 NY = 13:30 UTC."""
    from ics.live import _market_open_utc
    now = datetime(2026, 5, 11, 9, 0,
                   tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
    out = _market_open_utc(now)
    assert out.hour == 13 and out.minute == 30


def test_market_open_utc_in_winter_returns_1430():
    """January = EST (UTC-5): NYSE 09:30 NY = 14:30 UTC."""
    from ics.live import _market_open_utc
    now = datetime(2026, 1, 14, 9, 0,
                   tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
    out = _market_open_utc(now)
    assert out.hour == 14 and out.minute == 30


def test_market_open_utc_after_close_returns_next_weekday():
    from ics.live import _market_open_utc
    tue_pm = datetime(2026, 5, 12, 17, 0,
                      tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
    out = _market_open_utc(tue_pm)
    out_ny = out.astimezone(ZoneInfo("America/New_York"))
    assert out_ny.day == 13 and out_ny.weekday() == 2


def test_market_open_utc_friday_after_close_returns_monday():
    from ics.live import _market_open_utc
    fri_pm = datetime(2026, 5, 15, 17, 0,
                      tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
    out = _market_open_utc(fri_pm)
    out_ny = out.astimezone(ZoneInfo("America/New_York"))
    assert out_ny.weekday() == 0 and out_ny.day == 18


def test_market_open_utc_saturday_returns_monday():
    from ics.live import _market_open_utc
    sat = datetime(2026, 5, 16, 12, 0,
                   tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
    out = _market_open_utc(sat)
    out_ny = out.astimezone(ZoneInfo("America/New_York"))
    assert out_ny.weekday() == 0 and out_ny.day == 18


def test_market_open_utc_during_open_returns_today():
    from ics.live import _market_open_utc
    now = datetime(2026, 5, 13, 11, 0,
                   tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
    out = _market_open_utc(now)
    out_ny = out.astimezone(ZoneInfo("America/New_York"))
    assert out_ny.day == 13 and out_ny.hour == 9 and out_ny.minute == 30


def test_market_is_open_at_1030_summer():
    from ics.live import _market_is_open
    ts = datetime(2026, 5, 13, 10, 30,
                  tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
    assert _market_is_open(ts) is True


def test_market_is_open_false_on_weekends():
    from ics.live import _market_is_open
    sat = datetime(2026, 5, 16, 12, 0,
                   tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
    assert _market_is_open(sat) is False


def test_market_is_open_false_before_open():
    from ics.live import _market_is_open
    early = datetime(2026, 5, 13, 8, 0,
                     tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
    assert _market_is_open(early) is False


def test_market_is_open_false_after_close():
    from ics.live import _market_is_open
    late = datetime(2026, 5, 13, 17, 0,
                    tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
    assert _market_is_open(late) is False


# ---------------------------------------------------------------------------
# No HTML tags in Telegram-bound strings
# ---------------------------------------------------------------------------
def test_no_html_b_tags_in_live_module():
    from pathlib import Path
    src = Path("ics/live.py").read_text()
    assert "<b>" not in src and "</b>" not in src


def test_premarket_position_lines_not_indented():
    from ics import live
    with patch.object(live.signals, "current_regime_status",
                      return_value={"enabled": True, "ok": True,
                                    "reason": "OK", "as_of": "2026-05-11"}):
        text = live._build_premarket_report()
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("T1 ") or stripped.startswith("T2 "):
            assert not line.startswith("  "), \
                f"position line still indented: {line!r}"


# ---------------------------------------------------------------------------
# /help shows real descriptions
# ---------------------------------------------------------------------------
def test_help_uses_descriptions_when_registered():
    from ics import notifier
    notifier._actions.clear()
    notifier._action_descriptions.clear()
    try:
        notifier.register_action("foo", lambda: "ok", description="does the foo")
        notifier.register_action("bar", lambda: "ok")
        out = notifier._handle_command("/help")
        assert "does the foo" in out
        assert "registered handler" in out
    finally:
        notifier._actions.clear()
        notifier._action_descriptions.clear()


def test_help_groups_commands_logically():
    from ics import notifier
    notifier._actions.clear()
    notifier._action_descriptions.clear()
    try:
        notifier.register_action("scan", lambda: "ok", description="scan now")
        notifier.register_action("done", lambda a: "ok", description="record fill")
        out = notifier._handle_command("/help")
        assert "Status & info" in out
        assert "Scans & state" in out
        assert "Execution audit" in out
    finally:
        notifier._actions.clear()
        notifier._action_descriptions.clear()
