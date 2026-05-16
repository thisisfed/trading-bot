"""
signals.py
----------
Internal Convergence Scanner — Tier 1 / Tier 2 buy signals.

v2 fixes:
- Reinstates broad-market filter (SPY > HMA, sloping up). User's version
  silently dropped this, so signals fired in any market regime.
- Reinstates the 6-condition convergence scoring (was simplified to 4).
- Cleaner targets (measured-move when available, else 3R fallback).
- Stops use max(flag_low - small buf, entry - 2*ATR, swing_low) so we never
  set a stop that's higher than entry.
- scan_universe takes EITHER tickers list (canonical) or price_data dict
  (legacy live.py compat).

Conditions (each is binary):
  c1: Close > HMA(long) AND HMA(long) sloping up
  c2: Volume > vol_confirm_mult * 20d-avg volume
  c3: rsi_min < RSI(14) < rsi_max
  c4: relative-strength vs SPY > 0
  c5: bull-flag active OR breakout confirmed
  c6: Close > HMA(short) AND HMA(short) sloping up

Tier 1 = score >= tier1_min_conditions  AND (breakout or flag)
Tier 2 = score >= tier2_min_conditions
Broad-market filter (if enabled): SPY > HMA(long) AND HMA(long) sloping up.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from . import config, data
from .indicators import hma, rsi, atr, sma, relative_strength, detect_bull_flag, weekly_hma_aligned
from .logging_utils import get_logger

log = get_logger("ics.signals")


@dataclass
class Signal:
    ticker: str
    timestamp: pd.Timestamp
    tier: int
    score: int
    entry_price: float        # USD
    stop_loss: float          # USD
    target_price: float       # USD
    atr_value: float
    pole_height_pct: float
    flag_high: float
    flag_low: float
    rsi: float
    rs_score: float
    above_hma_long: bool
    above_hma_short: bool
    volume_confirmed: bool
    breakout: bool
    flag_active: bool
    reasons: str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["timestamp"] = pd.Timestamp(self.timestamp).isoformat()
        return d


# ---------------------------------------------------------------------------
# Indicator pre-computation
# ---------------------------------------------------------------------------
def _add_indicators(df: pd.DataFrame, ref_close: pd.Series,
                    p: config.SignalParams) -> pd.DataFrame:
    """Attach all indicator columns the scanner needs."""
    out = df.copy()
    out["hma_long"] = hma(out["Close"], p.hma_period_long)
    out["hma_short"] = hma(out["Close"], p.hma_period_short)
    out["hma_long_slope"] = out["hma_long"].diff()
    out["hma_short_slope"] = out["hma_short"].diff()
    out["rsi"] = rsi(out["Close"], p.rsi_period)
    out["atr"] = atr(out, 14)
    out["vol_avg_20"] = out["Volume"].rolling(p.vol_confirm_window,
                                              min_periods=p.vol_confirm_window).mean()
    out["vol_confirmed"] = out["Volume"] > out["vol_avg_20"] * p.vol_confirm_mult
    out["rs"] = relative_strength(out["Close"], ref_close, p.rs_lookback_days)

    # Weekly HMA bullish-cross filter (computed only when enabled to save work)
    if p.require_weekly_hma_bullish:
        out["weekly_hma_short"] = weekly_hma_aligned(out["Close"], p.weekly_hma_short_period)
        out["weekly_hma_long"]  = weekly_hma_aligned(out["Close"], p.weekly_hma_long_period)
        # Three-state: True (bullish), False (bearish), NaN (insufficient history).
        # Use object dtype so NaN survives — bool dtype would coerce NaN to False.
        bullish = out["weekly_hma_short"] > out["weekly_hma_long"]
        unknown = out["weekly_hma_short"].isna() | out["weekly_hma_long"].isna()
        bullish = bullish.where(~unknown, other=pd.NA).astype("object")
        out["weekly_hma_bullish"] = bullish

    flags = detect_bull_flag(
        out,
        pole_lookback=p.pole_lookback_bars,
        flag_lookback=p.flag_lookback_bars,
        flag_max_range_pct_of_pole=p.flag_max_range_pct_of_pole,
        pole_min_gain_pct=p.pole_min_gain_pct,
        breakout_buffer_pct=p.breakout_buffer_pct,
    )
    out = pd.concat([out, flags], axis=1)

    # Mean-reversion indicators (computed only when enabled).  Cheap, but no
    # need to add columns we never read.
    if p.mean_reversion_enabled:
        out["mr_rsi_short"] = rsi(out["Close"], p.mr_rsi_period)
        out["mr_sma_filter"] = sma(out["Close"], p.mr_sma_filter_period)

    return out


def _mr_entry_at(row: pd.Series, prev_row: pd.Series,
                 p: config.SignalParams) -> bool:
    """
    Return True iff the current bar triggers a mean-reversion entry.

    All conditions must hold:
      - Close > 200-SMA (still in uptrend)
      - RSI(2) < 10 (oversold)
      - Low < previous Low (real selling pressure today)
      - Close > Open (intraday reversal sign)

    Any NaN in any of the inputs => return False (fail-closed).
    """
    if not p.mean_reversion_enabled:
        return False
    needed = ("Close", "Low", "Open", "mr_rsi_short", "mr_sma_filter")
    if any(k not in row or pd.isna(row[k]) for k in needed):
        return False
    if "Low" not in prev_row or pd.isna(prev_row["Low"]):
        return False
    return bool(
        row["Close"] > row["mr_sma_filter"]
        and row["mr_rsi_short"] < p.mr_rsi_threshold
        and row["Low"] < prev_row["Low"]
        and row["Close"] > row["Open"]
    )


def _broad_market_ok(spy_df: pd.DataFrame, p: config.SignalParams,
                     at: pd.Timestamp) -> bool:
    """SPY > HMA(long) AND HMA sloping up?"""
    if spy_df is None or spy_df.empty:
        return True  # no data, fail open
    spy = spy_df.copy()
    spy["hma"] = hma(spy["Close"], p.hma_period_long)
    spy["slope"] = spy["hma"].diff()
    if at not in spy.index:
        idx = spy.index.searchsorted(at, side="right") - 1
        if idx < 0:
            return False
        row = spy.iloc[idx]
    else:
        row = spy.loc[at]
    if pd.isna(row["hma"]):
        return False
    return bool((row["Close"] > row["hma"]) and (row["slope"] > 0))


def _evaluate_bar(row: pd.Series, breakout_today: bool,
                  flag_active_today: bool,
                  p: config.SignalParams) -> tuple[int, list[str], dict]:
    """Score the convergence on one bar."""
    conds = {
        "c1_above_hma_long": (row["Close"] > row["hma_long"]) and (row["hma_long_slope"] > 0),
        "c2_volume_confirmed": bool(row["vol_confirmed"]),
        "c3_rsi_in_band": p.rsi_min < row["rsi"] < p.rsi_max,
        "c4_positive_rs": row["rs"] > 0,
        "c5_breakout_or_flag": breakout_today or flag_active_today,
        "c6_above_hma_short": (row["Close"] > row["hma_short"]) and (row["hma_short_slope"] > 0),
    }
    reasons = []
    if conds["c1_above_hma_long"]:
        reasons.append(f"Px>HMA{p.hma_period_long}")
    if conds["c2_volume_confirmed"]:
        reasons.append(f"Vol>{p.vol_confirm_mult:.1f}x20d")
    if conds["c3_rsi_in_band"]:
        reasons.append(f"RSI {row['rsi']:.0f}")
    if conds["c4_positive_rs"]:
        reasons.append(f"RS+ ({row['rs']*100:.1f}%)")
    if conds["c5_breakout_or_flag"]:
        reasons.append("Breakout" if breakout_today else "Flag")
    if conds["c6_above_hma_short"]:
        reasons.append(f"Px>HMA{p.hma_period_short}")
    return sum(bool(v) for v in conds.values()), reasons, conds


# ---------------------------------------------------------------------------
# Public scan
# ---------------------------------------------------------------------------
def scan_ticker(
    ticker: str,
    df: pd.DataFrame,
    ref_df: pd.DataFrame,
    spy_df: pd.DataFrame,
    p: Optional[config.SignalParams] = None,
    only_last_bar: bool = True,
) -> List[Signal]:
    """Scan a single ticker. Returns list of Signal objects (≤1 if only_last_bar)."""
    p = p or config.SIGNAL_PARAMS
    if df is None or df.empty or len(df) < max(p.hma_period_long * 2, 60):
        return []

    if ref_df is None or ref_df.empty:
        ref_df = spy_df  # fallback

    out = _add_indicators(df, ref_df["Close"], p)
    signals: List[Signal] = []

    if only_last_bar:
        bars = [(out.index[-1], out.iloc[-1])]
    else:
        bars = list(zip(out.index, [out.iloc[i] for i in range(len(out))]))

    for ts, row in bars:
        if pd.isna(row.get("hma_long")) or pd.isna(row.get("hma_short")):
            continue

        # Regime filter: gate new entries on broad-market conditions.
        # When config.REGIME_FILTERS.enabled, use the new multi-check filter.
        # Otherwise fall back to the legacy HMA-only filter for compatibility.
        from .regime import regime_ok
        if config.REGIME_FILTERS.enabled:
            r = regime_ok(spy_df, ts)
            if not r.ok:
                continue
        elif p.require_spy_above_hma and not _broad_market_ok(spy_df, p, ts):
            continue

        # Weekly HMA bullish-cross filter (per-stock).  Independent of regime.
        # Three-state value:
        #   True   = weekly HMA(short) > weekly HMA(long), allow entry
        #   False  = weekly HMA(short) <= weekly HMA(long), BLOCK entry
        #   pd.NA  = insufficient history, fail OPEN (allow entry)
        #   None   = column not present (param off), fail OPEN
        if p.require_weekly_hma_bullish:
            wh = row.get("weekly_hma_bullish")
            if wh is False:
                continue

        breakout_today = bool(row.get("breakout", False))
        flag_active_today = bool(row.get("flag_active", False))
        score, reasons, conds = _evaluate_bar(row, breakout_today, flag_active_today, p)

        tier = 0
        if score >= p.tier1_min_conditions and (breakout_today or flag_active_today):
            tier = 1
        elif score >= p.tier2_min_conditions:
            tier = 2
        if tier == 0:
            continue

        entry = float(row["Close"])
        atr_val = float(row["atr"]) if not pd.isna(row["atr"]) else entry * 0.02
        flag_low = float(row["flag_low"]) if not pd.isna(row["flag_low"]) else np.nan
        flag_high = float(row["flag_high"]) if not pd.isna(row["flag_high"]) else np.nan

        # Stop: max of (flag_low - small buf), (entry - 2*ATR), recent_swing_low,
        # but always strictly below entry.
        recent_low = float(out["Low"].tail(min(20, len(out))).min())
        stop_candidates = [entry - 2 * atr_val, recent_low]
        if not np.isnan(flag_low):
            stop_candidates.append(flag_low * 0.995)
        # Filter candidates to those strictly below entry; otherwise fall back.
        valid = [s for s in stop_candidates if 0 < s < entry]
        stop_loss = max(valid) if valid else entry - 2 * atr_val
        # Final defensive guard
        if stop_loss <= 0 or stop_loss >= entry:
            continue

        # Target: measured-move if available, else entry + RR*risk
        mm = (float(row.get("measured_move_target"))
              if not pd.isna(row.get("measured_move_target", np.nan))
              else np.nan)
        risk_per_share = max(entry - stop_loss, atr_val * 0.5)
        if not np.isnan(mm) and mm > entry:
            target = mm
        else:
            target = entry + risk_per_share * config.RISK_PARAMS.target_rr_multiple

        # Final defensive guard: target must be above entry
        if target <= entry:
            target = entry + risk_per_share * config.RISK_PARAMS.target_rr_multiple

        # Reject degenerate signals.  These typically come from bull-flag
        # measured-move calculations where the pole height collapsed near
        # zero, producing a "target" that's only a cent or two above entry.
        # Such trades can never clear fees + slippage and just clutter the
        # alert + paper book.
        target_gain_pct = (target - entry) / entry if entry > 0 else 0.0
        if target_gain_pct < p.min_target_gain_pct:
            log.debug(
                "Rejecting %s: target gain %.2f%% < min %.2f%%",
                ticker, target_gain_pct * 100, p.min_target_gain_pct * 100,
            )
            continue
        rr_ratio = (target - entry) / risk_per_share if risk_per_share > 0 else 0.0
        if rr_ratio < p.min_reward_risk_ratio:
            log.debug(
                "Rejecting %s: R/R %.2f < min %.2f",
                ticker, rr_ratio, p.min_reward_risk_ratio,
            )
            continue

        signals.append(
            Signal(
                ticker=ticker,
                timestamp=pd.Timestamp(ts),
                tier=tier,
                score=score,
                entry_price=round(entry, 4),
                stop_loss=round(stop_loss, 4),
                target_price=round(target, 4),
                atr_value=round(atr_val, 4),
                pole_height_pct=(float(row.get("pole_height_pct"))
                                 if not pd.isna(row.get("pole_height_pct", np.nan)) else 0.0),
                flag_high=flag_high if not np.isnan(flag_high) else 0.0,
                flag_low=flag_low if not np.isnan(flag_low) else 0.0,
                rsi=round(float(row["rsi"]), 2),
                rs_score=round(float(row["rs"]), 4),
                above_hma_long=bool(conds["c1_above_hma_long"]),
                above_hma_short=bool(conds["c6_above_hma_short"]),
                volume_confirmed=bool(conds["c2_volume_confirmed"]),
                breakout=breakout_today,
                flag_active=flag_active_today,
                reasons=" | ".join(reasons),
            )
        )
    return signals


def scan_universe(
    tickers,
    only_last_bar: bool = True,
    p: Optional[config.SignalParams] = None,
) -> List[Signal]:
    """
    Scan a list of tickers (or a dict of ticker -> df for legacy callers).
    Returns a list of Signal objects.
    """
    p = p or config.SIGNAL_PARAMS

    if isinstance(tickers, dict):
        # Legacy: caller passed price_data dict directly
        price_data = tickers
        ticker_list = list(price_data.keys())
        spy_df = price_data.get(config.SPY_TICKER) or pd.DataFrame()
        if spy_df.empty:
            spy_df = data.get_history(config.SPY_TICKER)
    else:
        ticker_list = list(tickers)
        end = pd.Timestamp.utcnow().normalize()
        start = end - pd.Timedelta(days=400)
        spy_df = data.get_history(config.SPY_TICKER,
                                  start=str(start.date()), end=str(end.date()))
        price_data = {}
        for t in ticker_list:
            df = data.get_history(t, start=str(start.date()), end=str(end.date()))
            if not df.empty:
                price_data[t] = df

    ref_df = spy_df  # SPY used as RS reference

    all_signals: List[Signal] = []
    for t in ticker_list:
        df = price_data.get(t)
        if df is None or df.empty:
            continue
        sigs = scan_ticker(t, df, ref_df, spy_df, p=p, only_last_bar=only_last_bar)
        all_signals.extend(sigs)

    log.info("Scanned %d tickers; produced %d signals.", len(ticker_list), len(all_signals))
    return all_signals


def current_regime_status() -> dict:
    """
    Check the regime filter against the latest available SPY bar and return
    a structured status that the live engine can format for Telegram.

    Returns
    -------
    dict with:
      "ok"        : bool — True = regime allowing new entries
      "reason"    : str  — explanation, e.g. "SPY 595 below 200-SMA 612"
      "as_of"     : str  — ISO date of the bar checked, or None if unavailable
      "enabled"   : bool — whether regime filter is active in config

    Falls back gracefully on data errors (returns ok=True, reason="data error")
    so a transient network failure doesn't surface as a panic message.
    """
    if not config.REGIME_FILTERS.enabled:
        return {"ok": True, "reason": "regime filter disabled in config",
                "as_of": None, "enabled": False}
    try:
        from .regime import regime_ok
        end = pd.Timestamp.utcnow().normalize()
        start = end - pd.Timedelta(days=400)
        spy_df = data.get_history(config.SPY_TICKER,
                                  start=str(start.date()), end=str(end.date()))
        if spy_df is None or spy_df.empty:
            return {"ok": True, "reason": "SPY data unavailable — fail open",
                    "as_of": None, "enabled": True}
        last_ts = spy_df.index[-1]
        result = regime_ok(spy_df, last_ts)
        return {
            "ok": bool(result.ok),
            "reason": result.reason,
            "as_of": last_ts.strftime("%Y-%m-%d"),
            "enabled": True,
        }
    except Exception as e:
        log.warning("Regime status check failed: %s", e)
        return {"ok": True, "reason": f"check failed: {e}",
                "as_of": None, "enabled": True}
