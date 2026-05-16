"""
Tests for the new live-engine integrations:
  - Earnings blackout filter (already in earnings.py, now wired into scan)
  - Pre-market readiness report assembly
"""
from unittest.mock import patch

import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# Earnings blackout — the module itself
# ---------------------------------------------------------------------------
def test_is_in_blackout_unknown_earnings_returns_false():
    """Fail open: no earnings data = no blackout."""
    from ics import earnings
    with patch("ics.earnings.get_next_earnings", return_value=None):
        assert earnings.is_in_earnings_blackout("AAPL", pd.Timestamp("2024-01-01")) is False


def test_is_in_blackout_past_earnings_returns_false():
    """Earnings already happened → no upcoming event → no blackout."""
    from ics import earnings
    past = pd.Timestamp("2023-12-01")
    with patch("ics.earnings.get_next_earnings", return_value=past):
        assert earnings.is_in_earnings_blackout(
            "AAPL", pd.Timestamp("2024-01-15"), blackout_days=7,
        ) is False


def test_is_in_blackout_within_window_returns_true():
    """Earnings in 3 days, blackout=7 days → True."""
    from ics import earnings
    now = pd.Timestamp("2024-01-15")
    upcoming = pd.Timestamp("2024-01-18")  # 3 days
    with patch("ics.earnings.get_next_earnings", return_value=upcoming):
        assert earnings.is_in_earnings_blackout(
            "AAPL", now, blackout_days=7,
        ) is True


def test_is_in_blackout_outside_window_returns_false():
    """Earnings in 14 days, blackout=7 days → False."""
    from ics import earnings
    now = pd.Timestamp("2024-01-15")
    upcoming = pd.Timestamp("2024-01-29")  # 14 days
    with patch("ics.earnings.get_next_earnings", return_value=upcoming):
        assert earnings.is_in_earnings_blackout(
            "AAPL", now, blackout_days=7,
        ) is False


def test_is_in_blackout_zero_days_disables_check():
    from ics import earnings
    with patch("ics.earnings.get_next_earnings", return_value=pd.Timestamp("2024-01-16")):
        assert earnings.is_in_earnings_blackout(
            "AAPL", pd.Timestamp("2024-01-15"), blackout_days=0,
        ) is False


def test_blackout_config_defaults_are_sane():
    from ics import config
    # The flag is enabled by default in live mode...
    assert config.SIGNAL_PARAMS.earnings_blackout_enabled_live is True
    # ...and the window is at least 5 days (one trading week)
    assert config.SIGNAL_PARAMS.earnings_blackout_days >= 5
    # ...but not absurdly long (would suppress too many trades)
    assert config.SIGNAL_PARAMS.earnings_blackout_days <= 14


# ---------------------------------------------------------------------------
# Pre-market report assembly
# ---------------------------------------------------------------------------
def test_premarket_report_includes_header():
    """The pre-market report always starts with the readiness header."""
    from ics import live
    with patch.object(live.signals, "current_regime_status",
                      return_value={"enabled": True, "ok": True, "reason": "OK"}):
        text = live._build_premarket_report()
    assert "PRE-MARKET" in text or "premarket" in text.lower()


def test_premarket_report_shows_regime_off_warning():
    """When regime is OFF, the report says so prominently."""
    from ics import live
    with patch.object(live.signals, "current_regime_status",
                      return_value={"enabled": True, "ok": False,
                                    "reason": "SPY below 200-SMA",
                                    "as_of": "2024-01-15"}):
        text = live._build_premarket_report()
    assert "REGIME OFF" in text
    assert "200-SMA" in text


def test_premarket_report_handles_empty_state():
    """No positions, no watchlist, no regime data → doesn't crash."""
    from ics import live
    with patch.object(live.signals, "current_regime_status",
                      side_effect=Exception("no SPY")):
        # Should not raise
        text = live._build_premarket_report()
    assert isinstance(text, str)
    assert len(text) > 0
