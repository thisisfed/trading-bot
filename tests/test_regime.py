"""
Smoke tests for the regime filter.
Synthetic SPY data — no network required.
"""
import numpy as np
import pandas as pd
import pytest

from ics import config
from ics.regime import regime_ok, reset_cache


@pytest.fixture
def spy_with_drawdown():
    """SPY with 300d uptrend then 60d 8% drawdown."""
    n = 360
    idx = pd.bdate_range("2022-01-03", periods=n)
    close_up = np.linspace(400, 500, 300)
    close_dn = np.linspace(500, 460, 60)
    close = np.concatenate([close_up, close_dn])
    return pd.DataFrame({
        "Open": close, "High": close * 1.01, "Low": close * 0.99,
        "Close": close, "Volume": 1e8,
    }, index=idx)


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_cache()


def _vix(idx, level):
    return pd.DataFrame({"Close": [level] * len(idx)}, index=idx)


def test_regime_passes_in_uptrend(spy_with_drawdown):
    spy = spy_with_drawdown
    mid = spy.index[250]
    r = regime_ok(spy, mid, vix_df=_vix(spy.index, 15))
    assert r.ok


def test_regime_fails_after_drawdown(spy_with_drawdown):
    spy = spy_with_drawdown
    late = spy.index[350]
    r = regime_ok(spy, late, vix_df=_vix(spy.index, 15))
    assert not r.ok
    assert "below" in r.reason.lower() or "drawdown" in r.reason.lower() or "off" in r.reason.lower()


def test_regime_fails_on_high_vix(spy_with_drawdown):
    spy = spy_with_drawdown
    mid = spy.index[250]
    r = regime_ok(spy, mid, vix_df=_vix(spy.index, 30))
    assert not r.ok
    assert "VIX" in r.reason


def test_regime_disabled_always_passes(spy_with_drawdown):
    spy = spy_with_drawdown
    late = spy.index[350]
    rf = config.RegimeFilters(enabled=False)
    r = regime_ok(spy, late, rf=rf, vix_df=_vix(spy.index, 50))
    assert r.ok
    assert "disabled" in r.reason


def test_regime_fails_open_with_short_history(spy_with_drawdown):
    spy = spy_with_drawdown.iloc[:50]
    r = regime_ok(spy, spy.index[40], vix_df=_vix(spy.index, 15))
    assert r.ok
    assert "insufficient" in r.reason.lower()


def test_regime_drawdown_check_isolated(spy_with_drawdown):
    """Drawdown-only filter (others off) should fire at day 350."""
    spy = spy_with_drawdown
    rf = config.RegimeFilters(
        require_spy_above_sma=False,
        require_vix_below_threshold=False,
        require_spy_drawdown_ok=True,
        max_spy_drawdown_pct=0.05,
    )
    r = regime_ok(spy, spy.index[350], rf=rf, vix_df=_vix(spy.index, 15))
    assert not r.ok
    assert "off" in r.reason and "high" in r.reason


def test_regime_no_spy_data_fails_open():
    r = regime_ok(pd.DataFrame(), pd.Timestamp("2023-01-01"))
    assert r.ok
    assert "no SPY data" in r.reason


# ---------------------------------------------------------------------------
# current_regime_status — the function the live engine calls every scan
# ---------------------------------------------------------------------------
def test_current_regime_status_returns_disabled_when_off():
    """When config.REGIME_FILTERS.enabled is False, should return ok=True with
    'disabled' in the reason."""
    from dataclasses import replace
    from ics import config, signals
    saved = config.REGIME_FILTERS
    try:
        config.REGIME_FILTERS = replace(saved, enabled=False)
        r = signals.current_regime_status()
        assert r["ok"] is True
        assert r["enabled"] is False
        assert "disabled" in r["reason"].lower()
    finally:
        config.REGIME_FILTERS = saved


def test_current_regime_status_has_required_keys():
    """Whatever happens upstream, the dict must have these four keys."""
    from ics import signals
    r = signals.current_regime_status()
    assert set(r.keys()) >= {"ok", "reason", "as_of", "enabled"}
    assert isinstance(r["ok"], bool)
    assert isinstance(r["reason"], str)
    assert isinstance(r["enabled"], bool)


def test_current_regime_status_handles_data_failure_gracefully():
    """If yfinance fails, status must fail-open (ok=True) so the bot doesn't
    panic-block all trades on a transient network glitch."""
    from unittest.mock import patch
    import pandas as pd
    from ics import data, signals
    with patch.object(data, "get_history", side_effect=Exception("network down")):
        r = signals.current_regime_status()
    assert r["ok"] is True
    assert "failed" in r["reason"].lower() or "error" in r["reason"].lower()
