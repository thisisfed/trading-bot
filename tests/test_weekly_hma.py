"""
Tests for the weekly HMA bullish-cross filter and helpers.
"""
import numpy as np
import pandas as pd
import pytest

from ics.indicators import weekly_hma_aligned, hma
from ics import config
from ics.signals import _add_indicators


@pytest.fixture
def long_uptrend_close():
    """A pure uptrend over ~2 years of daily bars — weekly HMA must be bullish."""
    n = 500
    idx = pd.bdate_range("2022-01-03", periods=n)
    close = pd.Series(np.linspace(100.0, 200.0, n), index=idx, name="Close")
    return close


@pytest.fixture
def long_downtrend_close():
    """A pure downtrend — weekly HMA must be bearish."""
    n = 500
    idx = pd.bdate_range("2022-01-03", periods=n)
    close = pd.Series(np.linspace(200.0, 100.0, n), index=idx, name="Close")
    return close


def test_weekly_hma_aligned_returns_same_index(long_uptrend_close):
    out = weekly_hma_aligned(long_uptrend_close, period=4)
    assert out.index.equals(long_uptrend_close.index)


def test_weekly_hma_uptrend_is_above_long_period(long_uptrend_close):
    """In a steady uptrend, short-period weekly HMA > long-period."""
    short = weekly_hma_aligned(long_uptrend_close, period=4)
    long  = weekly_hma_aligned(long_uptrend_close, period=13)
    # Compare the last bar where both are defined
    valid = short.notna() & long.notna()
    assert valid.any()
    last_idx = valid[valid].index[-1]
    assert short.loc[last_idx] > long.loc[last_idx]


def test_weekly_hma_downtrend_is_below_long_period(long_downtrend_close):
    """In a steady downtrend, short-period weekly HMA < long-period."""
    short = weekly_hma_aligned(long_downtrend_close, period=4)
    long  = weekly_hma_aligned(long_downtrend_close, period=13)
    valid = short.notna() & long.notna()
    last_idx = valid[valid].index[-1]
    assert short.loc[last_idx] < long.loc[last_idx]


def test_weekly_hma_no_lookahead(long_uptrend_close):
    """
    The value on day T must NOT change when we add data after day T.
    Catches accidental look-ahead from the resample/ffill chain.
    """
    short = weekly_hma_aligned(long_uptrend_close, period=4)
    truncated = long_uptrend_close.iloc[:300]
    short_trunc = weekly_hma_aligned(truncated, period=4)

    # Compare overlapping non-NaN values
    common_idx = short.index.intersection(short_trunc.index)
    a = short.loc[common_idx].dropna()
    b = short_trunc.loc[common_idx].dropna()
    overlap = a.index.intersection(b.index)
    assert len(overlap) > 50, "should have meaningful overlap"
    pd.testing.assert_series_equal(
        a.loc[overlap], b.loc[overlap], check_names=False
    )


def test_weekly_hma_insufficient_history():
    """With <period weekly bars, return all-NaN aligned to daily index."""
    n = 10  # only ~2 weeks
    idx = pd.bdate_range("2024-01-01", periods=n)
    close = pd.Series(np.linspace(100, 110, n), index=idx)
    out = weekly_hma_aligned(close, period=13)
    assert out.index.equals(idx)
    assert out.isna().all()


def test_weekly_hma_empty_input():
    out = weekly_hma_aligned(pd.Series(dtype=float), period=4)
    assert out.empty


def test_weekly_hma_period_validation():
    with pytest.raises(ValueError):
        weekly_hma_aligned(pd.Series([1.0, 2.0, 3.0]), period=1)


# ---------------------------------------------------------------------------
# Integration with _add_indicators
# ---------------------------------------------------------------------------
def _ohlcv_uptrend(n=500):
    idx = pd.bdate_range("2022-01-03", periods=n)
    close = np.linspace(100.0, 200.0, n)
    return pd.DataFrame({
        "Open": close, "High": close * 1.01, "Low": close * 0.99,
        "Close": close, "Volume": 1_000_000,
    }, index=idx)


def test_add_indicators_skips_weekly_hma_when_disabled():
    df = _ohlcv_uptrend()
    p = config.SignalParams(require_weekly_hma_bullish=False)
    out = _add_indicators(df, df["Close"], p)
    # Column should NOT be present when the param is off (we only compute it
    # when enabled, to save work).
    assert "weekly_hma_bullish" not in out.columns


def test_add_indicators_adds_weekly_hma_when_enabled():
    df = _ohlcv_uptrend()
    p = config.SignalParams(require_weekly_hma_bullish=True)
    out = _add_indicators(df, df["Close"], p)
    assert "weekly_hma_short" in out.columns
    assert "weekly_hma_long" in out.columns
    assert "weekly_hma_bullish" in out.columns


def test_add_indicators_weekly_bullish_in_uptrend():
    df = _ohlcv_uptrend()
    p = config.SignalParams(require_weekly_hma_bullish=True)
    out = _add_indicators(df, df["Close"], p)
    # Once history is sufficient, a steady uptrend should be bullish
    bullish_col = out["weekly_hma_bullish"].dropna()
    assert len(bullish_col) > 100
    # Allow for a few transition bars; the vast majority must be bullish
    bullish_count = (bullish_col == True).sum()  # noqa: E712
    assert bullish_count / len(bullish_col) > 0.9


def test_add_indicators_weekly_bullish_three_state_unknown():
    """Early bars (insufficient weekly history) must be NA, not False."""
    df = _ohlcv_uptrend(n=60)  # only ~12 weeks — barely enough for short, not enough for long
    p = config.SignalParams(
        require_weekly_hma_bullish=True,
        weekly_hma_short_period=4,
        weekly_hma_long_period=13,
    )
    out = _add_indicators(df, df["Close"], p)
    # First bars must be NA (unknown), not False (bearish)
    first_value = out["weekly_hma_bullish"].iloc[0]
    assert pd.isna(first_value)
