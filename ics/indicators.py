"""
indicators.py
-------------
Pure-pandas / numpy technical indicators. No TA-Lib, no pandas-ta.

Implements:
- WMA, HMA = WMA(2*WMA(n/2) - WMA(n), sqrt(n))
- RSI (Wilder), ATR (Wilder)
- relative_strength vs reference series
- Vectorised bull-flag / consolidation detector
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Moving averages
# ---------------------------------------------------------------------------
def wma(series: pd.Series, period: int) -> pd.Series:
    """Linearly weighted moving average. Weights 1..period (newest=period)."""
    if period <= 0:
        raise ValueError("period must be > 0")
    weights = np.arange(1, period + 1, dtype=float)
    weight_sum = weights.sum()

    def _calc(window: np.ndarray) -> float:
        return float(np.dot(window, weights) / weight_sum)

    return series.rolling(window=period, min_periods=period).apply(_calc, raw=True)


def hma(series: pd.Series, period: int) -> pd.Series:
    """HMA = WMA( 2*WMA(price, n/2) - WMA(price, n), sqrt(n) )."""
    if period <= 1:
        raise ValueError("period must be > 1")
    half = max(1, int(round(period / 2)))
    sqrt_n = max(1, int(round(math.sqrt(period))))
    wma_half = wma(series, half)
    wma_full = wma(series, period)
    raw = 2.0 * wma_half - wma_full
    return wma(raw, sqrt_n)


def weekly_hma_aligned(daily_close: pd.Series, period: int) -> pd.Series:
    """
    Compute HMA on weekly bars and align it back to the daily index, point-in-time
    correctly.

    Resamples `daily_close` to weekly bars (Friday close), computes HMA on those
    weekly bars, then re-indexes to the original daily index by FORWARD FILLING
    the most recent completed weekly value.

    Why forward-fill matters: it ensures every daily bar sees only the weekly
    HMA value from the LAST CLOSED week.  No look-ahead — Tuesday's signal can
    only see last Friday's HMA, not the value that will be true at the end of
    this week.

    Returns a Series with the same index as `daily_close`.  NaN until enough
    weekly history is available.
    """
    if period <= 1:
        raise ValueError("period must be > 1")
    if daily_close is None or daily_close.empty:
        return pd.Series(dtype=float, index=getattr(daily_close, "index", None))

    # Resample to weekly (Friday close).  W-FRI = week ends on Friday.
    weekly = daily_close.resample("W-FRI").last().dropna()
    if len(weekly) < period:
        return pd.Series(np.nan, index=daily_close.index, dtype=float)

    weekly_hma = hma(weekly, period)

    # Re-align to daily.  reindex+ffill so each daily bar gets the most recent
    # completed weekly HMA value.  Then SHIFT BY ONE DAY so the value for any
    # given Friday's daily bar is last week's HMA, not this week's — guards
    # against any same-day leakage where the resample happens to land exactly
    # on a daily bar.
    aligned = weekly_hma.reindex(daily_close.index, method="ffill")
    return aligned.shift(1)


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=period).mean()


# ---------------------------------------------------------------------------
# Momentum / volatility
# ---------------------------------------------------------------------------
def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """RSI with Wilder's smoothing."""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    return out.fillna(50.0)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range (Wilder)."""
    high = df["High"]
    low = df["Low"]
    close = df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


# ---------------------------------------------------------------------------
# Relative strength vs reference
# ---------------------------------------------------------------------------
def relative_strength(price: pd.Series, ref: pd.Series, lookback: int = 21) -> pd.Series:
    """
    RS score = price_return(lookback) - ref_return(lookback). Positive => outperforming.
    """
    p_ret = price / price.shift(lookback) - 1.0
    r_ret = ref.reindex(price.index).ffill()
    r_ret = r_ret / r_ret.shift(lookback) - 1.0
    return p_ret - r_ret


# ---------------------------------------------------------------------------
# Bull-flag / consolidation detector (vectorised, deterministic)
# ---------------------------------------------------------------------------
def detect_bull_flag(
    df: pd.DataFrame,
    pole_lookback: int = 20,
    flag_lookback: int = 15,
    flag_max_range_pct_of_pole: float = 0.30,
    pole_min_gain_pct: float = 0.15,
    breakout_buffer_pct: float = 0.001,
) -> pd.DataFrame:
    """
    Detect bull-flag setups bar-by-bar. Returns DataFrame indexed like df with:
      flag_active, flag_high, flag_low, pole_height_pct, breakout, measured_move_target
    """
    n = len(df)
    out = pd.DataFrame(
        index=df.index,
        data={
            "flag_active": False,
            "flag_high": np.nan,
            "flag_low": np.nan,
            "pole_height_pct": np.nan,
            "breakout": False,
            "measured_move_target": np.nan,
        },
    )
    if n < pole_lookback + flag_lookback + 1:
        return out

    closes = df["Close"].to_numpy(dtype=float)

    flag_active = np.zeros(n, dtype=bool)
    flag_high = np.full(n, np.nan)
    flag_low = np.full(n, np.nan)
    pole_h = np.full(n, np.nan)
    breakout = np.zeros(n, dtype=bool)
    target = np.full(n, np.nan)

    for t in range(pole_lookback + flag_lookback, n):
        flag_start = t - flag_lookback + 1
        flag_window = closes[flag_start: t + 1]
        prior_flag_window = closes[flag_start:t]
        pole_end = flag_start - 1
        pole_start = pole_end - pole_lookback + 1
        pole_window = closes[pole_start: pole_end + 1]

        if len(pole_window) < pole_lookback or len(flag_window) < flag_lookback:
            continue

        base_idx = int(np.argmin(pole_window))
        if base_idx >= len(pole_window) - 1:
            continue
        top_idx_rel = int(np.argmax(pole_window[base_idx:])) + base_idx
        pole_base = pole_window[base_idx]
        pole_top = pole_window[top_idx_rel]
        if pole_base <= 0:
            continue
        gain = (pole_top - pole_base) / pole_base
        if gain < pole_min_gain_pct:
            continue

        f_max = float(np.max(flag_window))
        f_min = float(np.min(flag_window))
        if pole_top <= 0:
            continue
        range_pct_of_pole = (f_max - f_min) / pole_top
        if range_pct_of_pole > flag_max_range_pct_of_pole:
            continue

        flag_active[t] = True
        flag_high[t] = f_max
        flag_low[t] = f_min
        pole_h[t] = gain

        prior_high = float(np.max(prior_flag_window)) if len(prior_flag_window) else f_max
        if closes[t] > prior_high * (1.0 + breakout_buffer_pct):
            breakout[t] = True
            target[t] = pole_top + (pole_top - pole_base)

    out["flag_active"] = flag_active
    out["flag_high"] = flag_high
    out["flag_low"] = flag_low
    out["pole_height_pct"] = pole_h
    out["breakout"] = breakout
    out["measured_move_target"] = target
    return out
