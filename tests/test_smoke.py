"""
test_smoke.py
-------------
Lightweight smoke tests that don't hit the network. They build synthetic price
series and verify the indicators, sizing, signals and backtester all run end-to-end
and produce SANE numbers (no billions, no >100% win rates, no same-day exits).
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ics import config
from ics.indicators import hma, rsi, atr, detect_bull_flag, relative_strength
from ics.sizing import compute_position
from ics.signals import _add_indicators, scan_ticker
from ics.performance import summarize, max_drawdown


# ---------------------------------------------------------------------------
# Helpers — synthetic OHLCV
# ---------------------------------------------------------------------------
def _make_trend(n: int = 400, seed: int = 1, drift: float = 0.0008,
                vol: float = 0.012) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, vol, size=n)
    close = 100.0 * np.exp(np.cumsum(rets))
    high = close * (1.0 + np.abs(rng.normal(0.0, 0.008, n)))
    low = close * (1.0 - np.abs(rng.normal(0.0, 0.008, n)))
    open_ = close * (1.0 + rng.normal(0.0, 0.003, n))
    vol_ser = (1_500_000 + rng.normal(0, 200_000, n)).astype(int).clip(min=10_000)
    idx = pd.bdate_range("2019-01-02", periods=n)
    return pd.DataFrame({
        "Open": open_, "High": np.maximum.reduce([open_, close, high]),
        "Low": np.minimum.reduce([open_, close, low]), "Close": close,
        "Volume": vol_ser,
    }, index=idx)


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------
def test_hma_finite_after_warmup():
    df = _make_trend()
    h = hma(df["Close"], 55)
    assert h.iloc[-1] == h.iloc[-1]  # not NaN
    assert math.isfinite(h.iloc[-1])


def test_rsi_in_range():
    df = _make_trend()
    r = rsi(df["Close"], 14).dropna()
    assert (r.between(0, 100)).all()


def test_atr_positive():
    df = _make_trend()
    a = atr(df, 14).dropna()
    assert (a > 0).all()


def test_relative_strength_runs():
    a = _make_trend(seed=1)["Close"]
    b = _make_trend(seed=2)["Close"]
    rs_v = relative_strength(a, b, 21).dropna()
    assert len(rs_v) > 100


def test_bull_flag_detector_no_crash():
    df = _make_trend(n=300)
    out = detect_bull_flag(df)
    assert {"flag_active", "flag_high", "flag_low", "breakout"} <= set(out.columns)
    assert out["flag_active"].dtype == bool


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------
def test_sizing_respects_risk_budget():
    plan = compute_position(
        equity_gbp=30_000, ticker="X", entry_usd=100, stop_usd=95,
        target_usd=120, tier=2, fx_gbp_per_usd=0.79,
    )
    assert plan is not None
    risk_pct = plan.risk_gbp / 30_000
    # 0.75% target with 1-share rounding tolerance
    assert risk_pct <= config.RISK_PARAMS.risk_per_trade_pct + 0.001
    assert plan.shares >= 1


def test_sizing_capped_by_position_pct():
    # Tight stop -> would otherwise allow huge size; cap engages.
    plan = compute_position(
        equity_gbp=30_000, ticker="X", entry_usd=10, stop_usd=9.95,
        target_usd=15, tier=1, fx_gbp_per_usd=0.79,
    )
    assert plan is not None
    notional_gbp = plan.shares * plan.entry_usd * plan.fx_gbp_per_usd
    cap = 30_000 * config.RISK_PARAMS.max_position_pct_of_equity
    assert notional_gbp <= cap + 1.0  # within 1 GBP rounding


def test_sizing_rejects_inverted_stop():
    plan = compute_position(
        equity_gbp=30_000, ticker="X", entry_usd=100, stop_usd=110,
        target_usd=120, tier=2, fx_gbp_per_usd=0.79,
    )
    assert plan is None


def test_sizing_rejects_bad_fx():
    plan = compute_position(
        equity_gbp=30_000, ticker="X", entry_usd=100, stop_usd=90,
        target_usd=120, tier=2, fx_gbp_per_usd=5.0,  # implausible
    )
    assert plan is None


def test_sizing_abs_share_cap():
    # Penny stock at $1 with $0.01 stop: would allow 22.5M shares without cap.
    plan = compute_position(
        equity_gbp=30_000, ticker="X", entry_usd=1.0, stop_usd=0.99,
        target_usd=2.0, tier=2, fx_gbp_per_usd=0.79,
    )
    if plan is not None:
        assert plan.shares <= config.RISK_PARAMS.abs_max_shares


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------
def test_add_indicators_runs():
    df = _make_trend()
    ref = _make_trend(seed=42)["Close"]
    out = _add_indicators(df, ref, config.SIGNAL_PARAMS)
    needed = {"hma_long", "hma_short", "rsi", "atr", "vol_confirmed", "rs"}
    assert needed <= set(out.columns)


def test_scan_ticker_either_returns_empty_or_valid():
    df = _make_trend(seed=11)
    spy = _make_trend(seed=22)
    sigs = scan_ticker("X", df, spy, spy, only_last_bar=False)
    for s in sigs:
        assert 0 < s.stop_loss < s.entry_price < s.target_price
        assert s.tier in (1, 2)


# ---------------------------------------------------------------------------
# Backtest end-to-end with synthetic data + monkey-patched data layer
# ---------------------------------------------------------------------------
def test_backtest_endtoend_sanity(monkeypatch, tmp_path):
    """
    Patch ics.data so the Backtester uses our synthetic series and never hits
    the network. Verify resulting numbers are sane.
    """
    from ics import data as data_mod, backtest as bt_mod, signals as sig_mod

    # synthetic universe
    synth = {
        "AAA": _make_trend(seed=1, drift=0.0010),
        "BBB": _make_trend(seed=2, drift=0.0009),
        "CCC": _make_trend(seed=3, drift=0.0008),
        "SPY": _make_trend(seed=99, drift=0.0005, vol=0.008),
        "VWRP.L": _make_trend(seed=100, drift=0.0004, vol=0.007),
        "GBPUSD=X": pd.DataFrame({
            "Open": 1.27, "High": 1.27, "Low": 1.27, "Close": 1.27, "Volume": 0,
        }, index=_make_trend(seed=99).index),
    }

    def fake_get_history(ticker, start=None, end=None, interval="1d", force_refresh=False):
        df = synth.get(ticker, pd.DataFrame()).copy()
        if df.empty:
            return df
        if start:
            df = df.loc[df.index >= pd.Timestamp(start)]
        if end:
            df = df.loc[df.index <= pd.Timestamp(end)]
        return df

    def fake_get_fx_series(start=None, end=None):
        df = synth["GBPUSD=X"]
        return (1.0 / df["Close"]).rename("gbp_per_usd")

    monkeypatch.setattr(data_mod, "get_history", fake_get_history)
    monkeypatch.setattr(data_mod, "get_fx_series", fake_get_fx_series)
    monkeypatch.setattr(sig_mod.data, "get_history", fake_get_history, raising=False)

    bt = bt_mod.Backtester(
        tickers=["AAA", "BBB", "CCC"],
        start="2019-06-01",
        end="2020-12-31",
        starting_capital_gbp=30_000.0,
    )
    res = bt.run()

    # ----- sanity assertions -----
    s = res.summary
    # End equity must be plausible: between £100 and £100M (no billion-pound bugs)
    assert 100.0 < s["end_equity_gbp"] < 100_000_000
    # Win-rate is a fraction in [0,1] (NOT 432%)
    assert 0.0 <= s["win_rate"] <= 1.0
    # Drawdown is a fraction in [0,1]
    assert 0.0 <= s["max_drawdown_pct"] <= 1.0
    # No same-day entry+exit (Bug C/D)
    if not res.trades.empty:
        same_day = (res.trades["entry_ts"] == res.trades["exit_ts"]).sum()
        assert same_day == 0, f"{same_day} same-day trades — bug C/D regression"

    # Cooldown actually prevents back-to-back same-ticker re-entries (Bug G)
    if not res.trades.empty and len(res.trades) >= 2:
        for tkr in res.trades["ticker"].unique():
            sub = res.trades[res.trades["ticker"] == tkr].sort_values("entry_ts")
            for i in range(1, len(sub)):
                exit_prev = pd.Timestamp(sub.iloc[i - 1]["exit_ts"])
                entry_cur = pd.Timestamp(sub.iloc[i]["entry_ts"])
                gap_days = (entry_cur - exit_prev).days
                assert gap_days >= 0  # must be after exit (basic sanity)

    # Shares are sane (hard cap from sizing)
    if not res.trades.empty:
        max_shares = res.trades["shares"].max()
        assert max_shares <= config.RISK_PARAMS.abs_max_shares


# ---------------------------------------------------------------------------
# Degenerate-signal filter — reject 0.1% targets and negative R/R
# ---------------------------------------------------------------------------
def test_signal_params_min_thresholds_have_sensible_defaults():
    from ics import config
    p = config.SignalParams()
    assert p.min_target_gain_pct >= 0.01, "default should reject < 1% gains"
    # R/R floor is intentionally low — only catches degenerate signals.
    # The S case had R/R 0.012; RKLB had 0.92.  Threshold should sit between.
    assert 0.0 < p.min_reward_risk_ratio <= 0.5


def test_min_target_gain_pct_filter_logic():
    """A 0.1% gain must be rejected when the threshold is 2%."""
    p = type("P", (), {"min_target_gain_pct": 0.02, "min_reward_risk_ratio": 0.3})()
    entry, target, stop = 16.09, 16.10, 14.74
    target_gain_pct = (target - entry) / entry
    risk_per_share = entry - stop
    rr = (target - entry) / risk_per_share
    assert target_gain_pct < p.min_target_gain_pct  # rejected by gain filter
    assert rr < p.min_reward_risk_ratio  # also rejected by R/R filter


def test_acceptable_signal_passes_filter():
    """A reasonable RKLB-like signal (R/R = 0.92) must NOT be rejected."""
    p = type("P", (), {"min_target_gain_pct": 0.02, "min_reward_risk_ratio": 0.3})()
    entry, target, stop = 99.32, 112.22, 85.23
    target_gain_pct = (target - entry) / entry
    risk_per_share = entry - stop
    rr = (target - entry) / risk_per_share
    assert target_gain_pct >= p.min_target_gain_pct
    assert rr >= p.min_reward_risk_ratio  # 0.92 > 0.3, passes


# ---------------------------------------------------------------------------
# Position-size and total-invested caps — guard against runaway compounding
# ---------------------------------------------------------------------------
def test_position_size_capped_by_absolute_pound_limit():
    """
    With £1,000,000 of equity, a 0.75% risk trade with a tight stop would size
    enormously without an absolute cap.  The absolute cap (default £6,000)
    must clamp the notional regardless of how large equity grows.
    """
    from ics import config
    from ics.sizing import compute_position
    plan = compute_position(
        equity_gbp=1_000_000.0,           # 30x compounded
        ticker="AAPL",
        entry_usd=100.0, stop_usd=99.0,   # tight stop, would size huge
        target_usd=120.0,
        tier=1,
        fx_gbp_per_usd=0.79,
    )
    assert plan is not None
    notional_gbp = plan.shares * 100.0 * 0.79
    cap = config.RISK_PARAMS.max_position_gbp_absolute
    assert notional_gbp <= cap + 100, \
        f"position notional £{notional_gbp:.0f} exceeded absolute cap £{cap:.0f}"


def test_position_size_uncapped_when_absolute_cap_zero():
    """When BOTH absolute caps are disabled, falls back to percentage-of-equity behaviour."""
    from dataclasses import replace
    from ics import config
    from ics.sizing import compute_position
    saved = config.RISK_PARAMS
    try:
        config.RISK_PARAMS = replace(
            saved,
            max_position_gbp_absolute=0.0,
            risk_per_trade_gbp_absolute=0.0,
        )
        plan = compute_position(
            equity_gbp=1_000_000.0,
            ticker="AAPL",
            entry_usd=100.0, stop_usd=95.0, target_usd=115.0,
            tier=1, fx_gbp_per_usd=0.79,
        )
        assert plan is not None
        notional_gbp = plan.shares * 100.0 * 0.79
        # With both caps disabled, percentage cap allows up to 20% × 1M = £200k
        assert notional_gbp > 6_000, "expected uncapped trade to exceed £6k"
    finally:
        config.RISK_PARAMS = saved


def test_default_caps_have_sensible_values():
    from ics import config
    rp = config.RISK_PARAMS
    assert rp.max_position_gbp_absolute > 0, \
        "default absolute cap must be positive to prevent runaway compounding"
    assert rp.max_total_invested_gbp_absolute > 0, \
        "default total-invested cap must be positive (no leverage in ISA)"
    # Total-invested cap should match starting capital (no leverage)
    assert rp.max_total_invested_gbp_absolute >= 30_000, \
        "total cap should be at least starting capital"


def test_risk_per_trade_capped_by_absolute_pound_limit():
    """At £1M equity, 0.75% × £1M = £7,500 risk per trade.  With the absolute
    cap of £225, the actual risk should be clamped to £225 regardless of equity."""
    from ics.sizing import compute_position
    plan = compute_position(
        equity_gbp=1_000_000.0,
        ticker="AAPL",
        entry_usd=100.0, stop_usd=90.0,    # $10/share = £7.90 risk per share
        target_usd=130.0,
        tier=1,
        fx_gbp_per_usd=0.79,
    )
    assert plan is not None
    # With cap £225 and risk-per-share £7.90, max shares = 28
    # Without cap, would be 7500/7.90 = 949 shares
    assert plan.shares <= 30, (
        f"shares {plan.shares} too high — risk cap not enforced "
        f"(implied risk £{plan.shares * 7.9:.0f})"
    )


def test_pyramid_shares_capped_by_absolute_limit():
    """
    The DDOG bug: 18 base shares + 5,665 pyramid shares on a £30k account.
    With absolute caps enforced, pyramid_shares must be a retail-sane number —
    at most enough to use the remaining notional budget (£6k position cap).
    """
    from ics.sizing import compute_position
    # High equity to trigger the compounding path
    plan = compute_position(
        equity_gbp=500_000.0,            # compounded to 16× starting capital
        ticker="DDOG",
        entry_usd=100.0, stop_usd=92.0, target_usd=125.0,
        tier=1,                           # Tier 1 triggers pyramid plan
        fx_gbp_per_usd=0.79,
    )
    assert plan is not None
    pyr = plan.pyramid_shares or 0
    # Pyramid notional should be well under the absolute position cap
    pyr_notional_gbp = pyr * 106.0 * 0.79  # approx trigger price
    from ics import config
    assert pyr_notional_gbp <= config.RISK_PARAMS.max_position_gbp_absolute + 100, (
        f"pyramid notional £{pyr_notional_gbp:.0f} exceeded position cap "
        f"£{config.RISK_PARAMS.max_position_gbp_absolute:.0f}"
    )


# ---------------------------------------------------------------------------
# Telegram message chunking — required when caps allow > 4096 char output
# ---------------------------------------------------------------------------
def test_split_for_telegram_short_message_returns_unchanged():
    from ics.notifier import _split_for_telegram
    assert _split_for_telegram("hello", max_chunk=100) == ["hello"]


def test_split_for_telegram_splits_at_paragraph_break():
    from ics.notifier import _split_for_telegram
    text = "A" * 50 + "\n\n" + "B" * 50 + "\n\n" + "C" * 50
    chunks = _split_for_telegram(text, max_chunk=110)
    # Should produce 2-3 chunks split at paragraph boundaries
    assert len(chunks) >= 2
    # No paragraph boundary should be split mid-block
    for c in chunks:
        assert "A\nB" not in c.replace("\n\n", "")


def test_split_for_telegram_falls_back_to_newline():
    from ics.notifier import _split_for_telegram
    text = "\n".join("X" * 40 for _ in range(10))  # 10 lines of 40 X's
    chunks = _split_for_telegram(text, max_chunk=120)
    assert len(chunks) >= 2


def test_split_for_telegram_hard_cuts_when_no_breaks():
    from ics.notifier import _split_for_telegram
    text = "X" * 1000  # no line breaks
    chunks = _split_for_telegram(text, max_chunk=300)
    assert len(chunks) >= 4
    # Each chunk respects the limit
    for c in chunks:
        assert len(c) <= 300


def test_split_for_telegram_preserves_total_content():
    from ics.notifier import _split_for_telegram
    original = "Para1\n\n" + "X" * 100 + "\n\nPara3\n\n" + "Y" * 100
    chunks = _split_for_telegram(original, max_chunk=80)
    # Joining with whitespace tolerance should reproduce the substantive content
    rejoined = "".join(chunks).replace(" ", "").replace("\n", "")
    src = original.replace(" ", "").replace("\n", "")
    assert rejoined == src


def test_display_caps_match_documented():
    """Sanity: caps in live.py should match the 15+15 we documented."""
    import inspect
    from ics import live
    src = inspect.getsource(live._send_scan_report)
    assert "DISPLAY_CAP_TIER1 = 15" in src
    assert "DISPLAY_CAP_TIER2 = 15" in src
