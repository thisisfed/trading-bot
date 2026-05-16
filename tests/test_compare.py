"""
Tests for compare.py — strategy vs benchmark.
Synthetic equity curves; data.get_history is mocked so no network is used.
"""
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest


def _curve(start_val, end_val, days=252, name="equity"):
    """Linear equity curve from start_val to end_val over `days` business days."""
    idx = pd.bdate_range("2020-01-02", periods=days)
    vals = np.linspace(start_val, end_val, days)
    return pd.Series(vals, index=idx, name=name)


def _ohlc_from_curve(eq):
    df = pd.DataFrame({
        "Open": eq.values, "High": eq.values * 1.005, "Low": eq.values * 0.995,
        "Close": eq.values, "Volume": 1_000_000,
    }, index=eq.index)
    return df


# ---------------------------------------------------------------------------
# Tests for the helper that finds the longest underwater run
# ---------------------------------------------------------------------------
def test_longest_run_of_true_basic():
    from ics.compare import _longest_run_of_true
    s = pd.Series([False, True, True, False, True, True, True, False])
    assert _longest_run_of_true(s) == 3


def test_longest_run_of_true_all_false():
    from ics.compare import _longest_run_of_true
    s = pd.Series([False, False, False])
    assert _longest_run_of_true(s) == 0


def test_longest_run_of_true_all_true():
    from ics.compare import _longest_run_of_true
    s = pd.Series([True, True, True, True])
    assert _longest_run_of_true(s) == 4


def test_longest_run_of_true_empty():
    from ics.compare import _longest_run_of_true
    assert _longest_run_of_true(pd.Series([], dtype=bool)) == 0


# ---------------------------------------------------------------------------
# Tests for _curve_metrics
# ---------------------------------------------------------------------------
def test_curve_metrics_uptrend():
    """A pure linear uptrend should produce positive CAGR and ~0 drawdown."""
    from ics.compare import _curve_metrics
    eq = _curve(30_000, 60_000, days=252)
    m = _curve_metrics(eq)
    assert m.final_equity == pytest.approx(60_000)
    assert m.cagr_pct > 0
    assert m.max_dd_pct == pytest.approx(0, abs=0.01)


def test_curve_metrics_with_drawdown():
    """A curve that dips in the middle should report a positive DD."""
    from ics.compare import _curve_metrics
    idx = pd.bdate_range("2020-01-02", periods=200)
    vals = np.concatenate([
        np.linspace(30_000, 35_000, 50),
        np.linspace(35_000, 28_000, 50),  # 20% drawdown
        np.linspace(28_000, 40_000, 100),
    ])
    eq = pd.Series(vals, index=idx)
    m = _curve_metrics(eq)
    assert m.max_dd_pct > 15
    assert m.days_underwater > 0


def test_curve_metrics_empty_returns_zeros():
    from ics.compare import _curve_metrics
    m = _curve_metrics(pd.Series(dtype=float))
    assert m.final_equity == 0
    assert m.cagr_pct == 0


# ---------------------------------------------------------------------------
# End-to-end: compare_to_benchmark with mocked yfinance
# ---------------------------------------------------------------------------
def test_compare_strategy_clearly_beats_benchmark():
    from ics import data, compare
    # Use noisy curves so Sharpe is defined (pure linear has zero volatility)
    rng = np.random.default_rng(42)
    idx = pd.bdate_range("2020-01-02", periods=252)
    strat_rets = rng.normal(0.003, 0.01, 252)   # +0.3% per day, low vol
    strat = pd.Series(30_000 * np.exp(np.cumsum(strat_rets)), index=idx)
    bench_rets = rng.normal(0.0004, 0.012, 252) # +0.04% per day, higher vol
    bench_curve = pd.Series(100 * np.exp(np.cumsum(bench_rets)), index=idx)

    with patch.object(data, "get_history", return_value=_ohlc_from_curve(bench_curve)):
        out = compare.compare_to_benchmark(
            strategy_equity_gbp=strat,
            benchmark_ticker="VWRP.L",
        )

    assert out["verdict"].startswith("✅"), f"got {out['verdict']!r}"
    assert out["edge"]["cagr_pct"] > 0
    assert out["edge"]["sharpe"] > 0


def test_compare_strategy_clearly_loses_to_benchmark():
    from ics import data, compare
    strat = _curve(30_000, 28_000)         # lost money
    bench_curve = _curve(100, 130)         # benchmark gained 30%

    with patch.object(data, "get_history", return_value=_ohlc_from_curve(bench_curve)):
        out = compare.compare_to_benchmark(
            strategy_equity_gbp=strat,
            benchmark_ticker="VWRP.L",
        )

    # Strategy lost money in absolute terms while benchmark gained → must lose
    assert "❌" in out["verdict"] or "WORSE" in out["verdict"], \
        f"got {out['verdict']!r}"


def test_compare_no_benchmark_data_returns_clean_message():
    from ics import data, compare
    strat = _curve(30_000, 32_000)

    with patch.object(data, "get_history", return_value=pd.DataFrame()):
        out = compare.compare_to_benchmark(
            strategy_equity_gbp=strat, benchmark_ticker="DOESNT_EXIST.L",
        )
    assert "NO BENCHMARK DATA" in out["verdict"]


def test_compare_empty_strategy_returns_no_data():
    from ics import compare
    out = compare.compare_to_benchmark(strategy_equity_gbp=pd.Series(dtype=float))
    assert out["verdict"] == "NO DATA"


def test_compare_render_is_human_readable():
    from ics import data, compare
    strat = _curve(30_000, 36_000)
    bench_curve = _curve(100, 108)

    with patch.object(data, "get_history", return_value=_ohlc_from_curve(bench_curve)):
        out = compare.compare_to_benchmark(strategy_equity_gbp=strat)

    text = out["render"]
    assert "STRATEGY" in text
    assert "VWRP.L" in text
    assert "Sharpe" in text
    assert "Calmar" in text
    assert "VERDICT" in text


# ---------------------------------------------------------------------------
# Edge sign convention — for "lower is better" metrics, edge should still
# flip so that positive = strategy is better
# ---------------------------------------------------------------------------
def test_edge_sign_convention_for_drawdown():
    """If strategy DD < benchmark DD, edge['max_dd_pct'] should be POSITIVE."""
    from ics import data, compare
    # Strategy: smooth uptrend, near-zero DD
    strat = _curve(30_000, 32_000, days=200)
    # Benchmark: includes a meaningful drawdown
    idx = pd.bdate_range("2020-01-02", periods=200)
    vals = np.concatenate([
        np.linspace(100, 105, 50),
        np.linspace(105, 80, 50),    # big drawdown
        np.linspace(80, 108, 100),
    ])
    bench_curve = pd.Series(vals, index=idx)

    with patch.object(data, "get_history", return_value=_ohlc_from_curve(bench_curve)):
        out = compare.compare_to_benchmark(strategy_equity_gbp=strat)

    # Strategy has lower DD → edge for DD should be positive (strategy better)
    assert out["edge"]["max_dd_pct"] > 0, \
        f"expected positive edge (strategy better), got {out['edge']['max_dd_pct']}"


def test_edge_sign_convention_for_worst_month():
    """Strategy worst month -5%, benchmark worst month -10%, edge should be +5pp (strategy better)."""
    from ics.compare import _compute_edge, CurveMetrics
    strat = CurveMetrics(0, 0, 0, 0, 0, 0, 0, 0, -5.0)
    bench = CurveMetrics(0, 0, 0, 0, 0, 0, 0, 0, -10.0)
    edge = _compute_edge(strat, bench)
    assert edge["worst_month_pct"] == pytest.approx(5.0), \
        "strategy's less-bad worst month should give positive edge"
