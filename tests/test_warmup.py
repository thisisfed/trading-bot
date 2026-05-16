"""Regression tests for the indicator-warmup fix.

The bug:  Before this fix, Backtester fetched data starting at `start`,
so long-lookback indicators (notably the 200-SMA used by the regime
filter and the MR sleeve) had ~200 days of NaN at the start of every
backtest.  In WFO contexts where OOS windows are 252 days, that meant
MR signals could only fire in the last ~50 bars and momentum entries
were also suppressed during the regime-filter warm-up — silently
biasing every variant comparison.

The fix:  Backtester now fetches data from (start - warmup_days) so
indicators are warm before the trading loop begins.  The equity curve
and contribution schedule still start from `start`.
"""
import warnings

import pandas as pd
import pytest

from ics import backtest, config, data


# These tests need real cached SPY + ticker data to run a Backtester.
# Skip the whole module gracefully when running in a clean environment
# (e.g. fresh clone, CI without network) so the suite stays green.
_SPY_AVAILABLE = False
try:
    _spy = data.get_history(config.SPY_TICKER, start="2023-06-01", end="2024-12-31")
    _SPY_AVAILABLE = not _spy.empty
except Exception:
    _SPY_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not _SPY_AVAILABLE,
    reason="SPY price data unavailable — these tests require a populated cache.",
)


def _silence_warnings():
    warnings.filterwarnings("ignore")


def test_equity_curve_starts_at_user_start_date():
    """The equity series must still start at `start`, not at fetch_start."""
    _silence_warnings()
    bt = backtest.Backtester(
        tickers=["AAPL"], start="2024-01-01", end="2024-12-31",
        starting_capital_gbp=30000.0,
    )
    res = bt.run()
    assert not res.equity_gbp.empty
    # First equity date should be at or just after Jan 1 2024 (first business day)
    assert res.equity_gbp.index[0] >= pd.Timestamp("2024-01-01")
    assert res.equity_gbp.index[0] <= pd.Timestamp("2024-01-05")
    # Last date should be the requested end (or last business day before it)
    assert res.equity_gbp.index[-1] <= pd.Timestamp("2024-12-31")


def test_fetch_start_precedes_start_by_warmup_days():
    """Internal fetch_start must be ~warmup_days before start."""
    _silence_warnings()
    bt = backtest.Backtester(
        tickers=["AAPL"], start="2024-06-01", end="2024-12-31",
        starting_capital_gbp=30000.0,
        warmup_days=300,
    )
    fetch_ts = pd.Timestamp(bt._fetch_start)
    start_ts = pd.Timestamp("2024-06-01")
    delta = (start_ts - fetch_ts).days
    assert 295 <= delta <= 305, f"fetch_start delta = {delta} days"


def test_warmup_zero_keeps_old_behaviour():
    """warmup_days=0 means fetch_start == start (escape hatch for legacy)."""
    _silence_warnings()
    bt = backtest.Backtester(
        tickers=["AAPL"], start="2024-01-01", end="2024-12-31",
        starting_capital_gbp=30000.0, warmup_days=0,
    )
    assert bt._fetch_start == "2024-01-01"


def test_warmup_makes_mr_signals_fire_in_short_windows():
    """
    The core regression: in a 252-day window, MR signals must fire when
    warmup is on, AND must fire substantially less (or not at all) when
    warmup is off.  Demonstrates the bug existed and is now fixed.

    Uses a small basket rather than a single ticker, because RSI(2)<10
    oversold events are sparse and any one ticker may go a whole year
    without one in a strong uptrend.  Picks tickers that historically
    have higher volatility.
    """
    _silence_warnings()
    import dataclasses
    import os

    # Use any tickers from the price cache that are likely volatile.
    cache_dir = config.DATA_DIR / "price_cache"
    if not cache_dir.exists():
        pytest.skip("No price cache available")
    available = {
        f.stem.replace("__1d", "") for f in cache_dir.iterdir()
        if f.suffix == ".parquet"
    }
    candidates = ["AMD", "NVDA", "TSLA", "AVGO", "MU", "AMAT", "MRVL", "ON",
                  "ARM", "PLTR", "SMCI", "QCOM", "AAPL", "MSFT", "GOOGL"]
    universe = [t for t in candidates if t in available][:10]
    if len(universe) < 5:
        pytest.skip(f"Not enough volatile tickers in cache (got {len(universe)})")

    sp_on = dataclasses.replace(config.SIGNAL_PARAMS, mean_reversion_enabled=True)

    bt_warm = backtest.Backtester(
        tickers=universe, start="2024-01-01", end="2024-12-31",
        starting_capital_gbp=30000.0, signal_params=sp_on,
    )
    res_warm = bt_warm.run()
    n_mr_warm = (
        (res_warm.trades["signal_type"] == "mr").sum()
        if not res_warm.trades.empty and "signal_type" in res_warm.trades.columns
        else 0
    )

    bt_cold = backtest.Backtester(
        tickers=universe, start="2024-01-01", end="2024-12-31",
        starting_capital_gbp=30000.0, signal_params=sp_on, warmup_days=0,
    )
    res_cold = bt_cold.run()
    n_mr_cold = (
        (res_cold.trades["signal_type"] == "mr").sum()
        if not res_cold.trades.empty and "signal_type" in res_cold.trades.columns
        else 0
    )

    assert n_mr_warm >= 1, (
        f"Expected MR signals to fire with warmup on a {len(universe)}-ticker "
        f"basket, got {n_mr_warm}"
    )
    assert n_mr_warm > n_mr_cold, (
        f"Warmup should produce MORE MR signals than no-warmup. "
        f"Got warm={n_mr_warm}, cold={n_mr_cold}"
    )
