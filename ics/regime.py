"""
regime.py
---------
Broad-market regime filters that gate new entries.

The old `_broad_market_ok` (HMA-based) flips too easily in choppy markets.
The 2021–2022 OOS losses in our WFO came from exactly that — SPY was usually
above its short-term HMA but the market was clearly in a chop/distribution
regime.

This module replaces (or augments) it with three independent, configurable
checks:

  1. SPY > 200-SMA AND SMA sloping up over N bars.
     Slow enough that it doesn't flip on every weekly chop.

  2. VIX < threshold.
     High VIX = chop regime, breakout strategies underperform.

  3. SPY no more than X% below its 60-day high.
     Early-warning of regime change before the SMA flips.

All three default to ON; turn individual checks off via RegimeFilters fields,
or set `enabled=False` to bypass entirely.

Existing positions are always managed normally — stops, targets, trailing
exits all continue to fire regardless of the regime filter.  The filter only
prevents NEW entries.

Backward compatibility: when the regime filter is enabled, the old
`SignalParams.require_spy_above_hma` is ignored.  When disabled, behaviour
falls back to the legacy HMA filter.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from . import config, data
from .logging_utils import get_logger

log = get_logger("ics.regime")


# Cache the VIX series so we don't refetch on every bar in a backtest
_vix_cache: dict[str, pd.Series] = {}


@dataclass
class RegimeReason:
    """Explains WHY the regime check passed or failed (for logging/debugging)."""
    ok: bool
    reason: str


def regime_ok(
    spy_df: pd.DataFrame,
    at: pd.Timestamp,
    rf: Optional[config.RegimeFilters] = None,
    *,
    vix_df: Optional[pd.DataFrame] = None,
) -> RegimeReason:
    """
    Should we allow new entries on this bar?

    Returns RegimeReason(ok=bool, reason=str).  All checks fail open if their
    underlying data is unavailable (so a missing VIX feed doesn't kill the bot).

    Parameters
    ----------
    spy_df  : pd.DataFrame  — daily SPY OHLCV with a DatetimeIndex
    at      : pd.Timestamp  — bar to check
    rf      : config.RegimeFilters | None  — defaults to config.REGIME_FILTERS
    vix_df  : pd.DataFrame | None  — optional, fetched on demand if needed

    Returns
    -------
    RegimeReason
    """
    rf = rf or config.REGIME_FILTERS
    if not rf.enabled:
        return RegimeReason(True, "regime filter disabled")
    if spy_df is None or spy_df.empty:
        return RegimeReason(True, "no SPY data — fail open")

    # --- Snap to the nearest available bar at or before `at` ---
    if at not in spy_df.index:
        idx = spy_df.index.searchsorted(at, side="right") - 1
        if idx < 0:
            return RegimeReason(False, "no SPY history before bar")
        spy_at = spy_df.iloc[: idx + 1]
    else:
        spy_at = spy_df.loc[:at]

    # ----- Check 1: SPY above SMA + SMA sloping up -----
    if rf.require_spy_above_sma:
        # Need SMA period + slope lookback bars to compute meaningfully
        need = rf.spy_sma_period + rf.sma_slope_lookback
        if len(spy_at) < need:
            return RegimeReason(True, f"SMA history insufficient ({len(spy_at)} < {need}) — fail open")
        sma = spy_at["Close"].rolling(rf.spy_sma_period).mean()
        last_close = float(spy_at["Close"].iloc[-1])
        last_sma = float(sma.iloc[-1])
        if pd.isna(last_sma):
            return RegimeReason(True, "SMA not yet ready")
        if last_close < last_sma:
            return RegimeReason(False, f"SPY {last_close:.2f} below {rf.spy_sma_period}-SMA {last_sma:.2f}")
        prev_sma = float(sma.iloc[-1 - rf.sma_slope_lookback])
        if pd.isna(prev_sma):
            return RegimeReason(True, "SMA slope not yet computable")
        if last_sma <= prev_sma:
            return RegimeReason(False, f"{rf.spy_sma_period}-SMA flat/declining over {rf.sma_slope_lookback}d")

    # ----- Check 2: VIX below threshold -----
    if rf.require_vix_below_threshold and rf.vix_max < 999:
        vix_value = _vix_at(at, vix_df=vix_df, ticker=rf.vix_ticker)
        if vix_value is not None and vix_value > rf.vix_max:
            return RegimeReason(False, f"VIX {vix_value:.1f} > {rf.vix_max}")

    # ----- Check 3: SPY drawdown from recent high -----
    if rf.require_spy_drawdown_ok:
        if len(spy_at) < rf.spy_drawdown_lookback:
            return RegimeReason(True, "drawdown lookback history insufficient — fail open")
        recent_window = spy_at["Close"].iloc[-rf.spy_drawdown_lookback:]
        recent_high = float(recent_window.max())
        last_close = float(recent_window.iloc[-1])
        dd_pct = (recent_high - last_close) / recent_high if recent_high > 0 else 0
        if dd_pct > rf.max_spy_drawdown_pct:
            return RegimeReason(
                False,
                f"SPY {dd_pct*100:.1f}% off {rf.spy_drawdown_lookback}d high "
                f"(max {rf.max_spy_drawdown_pct*100:.1f}%)"
            )

    return RegimeReason(True, "all regime checks pass")


# ---------------------------------------------------------------------------
# VIX data helper
# ---------------------------------------------------------------------------
def _vix_at(
    at: pd.Timestamp,
    *,
    vix_df: Optional[pd.DataFrame] = None,
    ticker: str = "^VIX",
) -> Optional[float]:
    """Return the VIX close at-or-before `at`, or None if unavailable."""
    df = vix_df if vix_df is not None else _get_cached_vix(ticker)
    if df is None or df.empty:
        return None

    if at in df.index:
        v = df.loc[at, "Close"]
    else:
        idx = df.index.searchsorted(at, side="right") - 1
        if idx < 0:
            return None
        v = df.iloc[idx]["Close"]
    try:
        return float(v) if not pd.isna(v) else None
    except (TypeError, ValueError):
        return None


def _get_cached_vix(ticker: str) -> Optional[pd.DataFrame]:
    """Fetch and cache the VIX history. Returns None on failure (fail open)."""
    if ticker in _vix_cache:
        cached = _vix_cache[ticker]
        return cached if not cached.empty else None
    try:
        df = data.get_history(ticker)
        if df is None or df.empty:
            log.warning("VIX history empty for %s — regime VIX check will fail open.", ticker)
            _vix_cache[ticker] = pd.DataFrame()
            return None
        # Normalise to a Close column DataFrame
        if "Close" not in df.columns:
            log.warning("VIX feed missing Close column — disabling VIX check.")
            _vix_cache[ticker] = pd.DataFrame()
            return None
        _vix_cache[ticker] = df[["Close"]]
        return df[["Close"]]
    except Exception as e:
        log.warning("Failed to fetch VIX (%s): %s — regime VIX check disabled.", ticker, e)
        _vix_cache[ticker] = pd.DataFrame()
        return None


def reset_cache() -> None:
    """Clear the VIX cache (used in tests)."""
    _vix_cache.clear()
