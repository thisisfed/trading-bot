"""Tests for the mean-reversion sleeve."""
import dataclasses
import numpy as np
import pandas as pd
import pytest

from ics import config
from ics.signals import _mr_entry_at, _add_indicators


# ---------------------------------------------------------------------------
# _mr_entry_at — pure-function tests on synthetic rows
# ---------------------------------------------------------------------------
def _make_p(**overrides):
    """SignalParams with MR ON by default; any field can be overridden."""
    overrides.setdefault("mean_reversion_enabled", True)
    return dataclasses.replace(config.SIGNAL_PARAMS, **overrides)


def _row(close, open_, low, mr_rsi_short, mr_sma_filter):
    return pd.Series({
        "Close": close, "Open": open_, "Low": low,
        "mr_rsi_short": mr_rsi_short, "mr_sma_filter": mr_sma_filter,
    })


def _prev(low):
    return pd.Series({"Low": low})


def test_mr_entry_fires_when_all_conditions_met():
    p = _make_p()
    # Close > 200-SMA (uptrend), RSI < 10 (oversold), low < prev low (selling), close > open (reversal)
    row = _row(close=100, open_=99, low=98, mr_rsi_short=8, mr_sma_filter=95)
    assert _mr_entry_at(row, _prev(low=99), p) is True


def test_mr_entry_blocks_when_below_200_sma():
    p = _make_p()
    row = _row(close=90, open_=89, low=88, mr_rsi_short=8, mr_sma_filter=95)
    assert _mr_entry_at(row, _prev(low=99), p) is False


def test_mr_entry_blocks_when_rsi_above_threshold():
    p = _make_p(mr_rsi_threshold=10.0)
    row = _row(close=100, open_=99, low=98, mr_rsi_short=15, mr_sma_filter=95)
    assert _mr_entry_at(row, _prev(low=99), p) is False


def test_mr_entry_blocks_when_low_not_below_prev_low():
    p = _make_p()
    # Today's low = 99 = yesterday's low — no fresh selling pressure
    row = _row(close=100, open_=99, low=99, mr_rsi_short=8, mr_sma_filter=95)
    assert _mr_entry_at(row, _prev(low=99), p) is False


def test_mr_entry_blocks_when_close_below_open():
    p = _make_p()
    # Down day overall — no reversal sign
    row = _row(close=98, open_=99, low=97, mr_rsi_short=8, mr_sma_filter=95)
    assert _mr_entry_at(row, _prev(low=99), p) is False


def test_mr_entry_returns_false_when_disabled():
    p = _make_p(mean_reversion_enabled=False)
    row = _row(close=100, open_=99, low=98, mr_rsi_short=8, mr_sma_filter=95)
    assert _mr_entry_at(row, _prev(low=99), p) is False


def test_mr_entry_handles_nan_inputs_safely():
    p = _make_p()
    row = _row(close=100, open_=99, low=98, mr_rsi_short=np.nan, mr_sma_filter=95)
    assert _mr_entry_at(row, _prev(low=99), p) is False
    row = _row(close=np.nan, open_=99, low=98, mr_rsi_short=8, mr_sma_filter=95)
    assert _mr_entry_at(row, _prev(low=99), p) is False
    # Missing column entirely
    bad_prev = pd.Series({"Low": np.nan})
    row = _row(close=100, open_=99, low=98, mr_rsi_short=8, mr_sma_filter=95)
    assert _mr_entry_at(row, bad_prev, p) is False


def test_mr_indicator_columns_present_when_enabled():
    """_add_indicators should write mr_rsi_short and mr_sma_filter when MR is on."""
    p = _make_p(mr_rsi_period=2, mr_sma_filter_period=20)
    # Synthetic price frame, ~250 bars to satisfy HMA(55) etc.
    n = 260
    rng = np.random.default_rng(seed=42)
    close = pd.Series(100 + rng.normal(0, 1, n).cumsum(), name="Close")
    df = pd.DataFrame({
        "Open": close * 0.999,
        "High": close * 1.005,
        "Low":  close * 0.995,
        "Close": close,
        "Volume": pd.Series(1_000_000, index=range(n)),
    })
    df.index = pd.date_range("2023-01-02", periods=n, freq="B")
    spy_close = close.copy()  # use ticker as own ref to keep dummy clean
    out = _add_indicators(df, spy_close, p)
    assert "mr_rsi_short" in out.columns
    assert "mr_sma_filter" in out.columns
    # Eventually non-NaN once the windows fill
    assert out["mr_rsi_short"].notna().sum() > 200
    assert out["mr_sma_filter"].notna().sum() > 200


def test_mr_indicator_columns_absent_when_disabled():
    """No indicator columns added when MR is off — keeps memory lean for WFO."""
    p = dataclasses.replace(config.SIGNAL_PARAMS, mean_reversion_enabled=False)
    n = 260
    rng = np.random.default_rng(seed=7)
    close = pd.Series(100 + rng.normal(0, 1, n).cumsum(), name="Close")
    df = pd.DataFrame({
        "Open": close * 0.999, "High": close * 1.005, "Low": close * 0.995,
        "Close": close, "Volume": pd.Series(1_000_000, index=range(n)),
    })
    df.index = pd.date_range("2023-01-02", periods=n, freq="B")
    out = _add_indicators(df, close.copy(), p)
    assert "mr_rsi_short" not in out.columns
    assert "mr_sma_filter" not in out.columns


# ---------------------------------------------------------------------------
# OpenPosition has the new MR-tracking fields with sane defaults
# ---------------------------------------------------------------------------
def test_open_position_defaults_to_momentum_signal_type():
    """Existing call sites without signal_type kwarg still work."""
    from ics.backtest import OpenPosition
    pos = OpenPosition(
        ticker="AAPL", tier=1, entry_ts=pd.Timestamp("2024-01-02"),
        entry_usd=100.0, initial_stop_usd=98.0, stop_usd=98.0,
        target_usd=106.0, shares=10,
    )
    assert pos.signal_type == "momentum"
    assert pos.bars_held == 0
    assert pos.prev_close_usd == 0.0
