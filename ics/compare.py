"""
compare.py
----------
Side-by-side comparison of the ICS strategy versus a buy-and-hold benchmark
ETF (VWRP.L by default — the Vanguard FTSE All-World UCITS in GBP, which
is what you'd actually buy in a UK ISA as the "do nothing" alternative).

The point of this module is to answer one question:

  "Is running this bot worth it compared to just buying VWRP and forgetting?"

If the strategy doesn't beat VWRP on a risk-adjusted basis (Sharpe, Calmar)
over the same period, the answer is no — even if it beats raw CAGR, you've
taken on more risk and complexity than the lazy alternative.

What it produces
----------------
A side-by-side table:

    Metric                   Strategy    VWRP B&H    Edge
    ----------------------------------------------------
    Final equity (£)         85,489      71,234      +14,255
    CAGR (%)                 21.0        16.5        +4.5
    Max drawdown (%)         14.9        25.4        +10.5  (lower is better)
    Sharpe                   0.94        0.81        +0.13
    Sortino                  1.82        1.32        +0.50
    Calmar                   1.41        0.65        +0.76
    Time underwater (days)   76          184         +108   (lower is better)
    Best month (%)           12.4        14.2        -1.8
    Worst month (%)          -8.7        -12.5       +3.8

…plus a verdict block:

    ✅  STRATEGY WINS
    Beats VWRP on 7 of 9 metrics including Sharpe (the one that matters).

    Or:

    ❌  STRATEGY LOSES
    Loses to VWRP on the headline metrics; just buy and hold.

Usage
-----
    from ics.compare import compare_to_benchmark
    report = compare_to_benchmark(strategy_equity_gbp, start, end)
    print(report["render"])

CLI:
    python -m ics.cli compare --start 2019-01-01 --name strategy_vs_vwrp
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from . import config, data
from .performance import cagr, sharpe, sortino, max_drawdown
from .logging_utils import get_logger

log = get_logger("ics.compare")


# ---------------------------------------------------------------------------
# Metric extraction
# ---------------------------------------------------------------------------
@dataclass
class CurveMetrics:
    final_equity: float
    cagr_pct: float
    max_dd_pct: float
    sharpe: float
    sortino: float
    calmar: float
    days_underwater: int
    best_month_pct: float
    worst_month_pct: float

    def as_dict(self) -> dict:
        return {
            "final_equity":  self.final_equity,
            "cagr_pct":      self.cagr_pct,
            "max_dd_pct":    self.max_dd_pct,
            "sharpe":        self.sharpe,
            "sortino":       self.sortino,
            "calmar":        self.calmar,
            "days_underwater": self.days_underwater,
            "best_month_pct":  self.best_month_pct,
            "worst_month_pct": self.worst_month_pct,
        }


def _curve_metrics(equity: pd.Series) -> CurveMetrics:
    """Compute the comparison metrics for one equity curve."""
    if equity.empty or len(equity) < 2:
        return CurveMetrics(0, 0, 0, 0, 0, 0, 0, 0, 0)

    final = float(equity.iloc[-1])
    c = cagr(equity)
    dd = max_drawdown(equity)
    sh = sharpe(equity)
    so = sortino(equity)
    cal = c / dd if dd > 0 else 0.0

    # Time underwater: longest run where equity is below its prior peak
    running_max = equity.cummax()
    underwater_mask = equity < running_max
    days_uw = _longest_run_of_true(underwater_mask)

    # Monthly returns
    monthly = equity.resample("ME").last().pct_change().dropna()
    best_m = float(monthly.max() * 100) if not monthly.empty else 0.0
    worst_m = float(monthly.min() * 100) if not monthly.empty else 0.0

    return CurveMetrics(
        final_equity=final,
        cagr_pct=c * 100,
        max_dd_pct=dd * 100,
        sharpe=sh,
        sortino=so,
        calmar=cal,
        days_underwater=days_uw,
        best_month_pct=best_m,
        worst_month_pct=worst_m,
    )


def _longest_run_of_true(mask: pd.Series) -> int:
    """Length of the longest consecutive True run in `mask`."""
    if mask.empty:
        return 0
    # Group consecutive runs.  Each time the value changes, increment a group id.
    arr = mask.astype(int).values
    if not arr.any():
        return 0
    # Identify run starts
    diff = np.diff(np.concatenate([[0], arr, [0]]))
    starts = np.where(diff == 1)[0]
    ends = np.where(diff == -1)[0]
    if len(starts) == 0:
        return 0
    return int((ends - starts).max())


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def compare_to_benchmark(
    strategy_equity_gbp: pd.Series,
    start: Optional[str] = None,
    end: Optional[str] = None,
    benchmark_ticker: Optional[str] = None,
    starting_capital_gbp: Optional[float] = None,
) -> dict:
    """
    Build a side-by-side comparison of the strategy curve and a buy-and-hold
    benchmark ETF over the same period.

    Parameters
    ----------
    strategy_equity_gbp : pd.Series  — daily equity in GBP, indexed by date
    start, end          : optional bounds (default: full range of strategy)
    benchmark_ticker    : default config.BENCHMARK_TICKER (VWRP.L)
    starting_capital_gbp: default config.STARTING_CAPITAL_GBP

    Returns
    -------
    dict with:
      "strategy":  CurveMetrics-as-dict
      "benchmark": CurveMetrics-as-dict
      "edge":      strategy minus benchmark per metric
      "verdict":   string ("STRATEGY WINS" / "STRATEGY LOSES" / "TIE")
      "render":    full human-readable report (multi-line string)
      "benchmark_ticker": which ETF was used
    """
    if strategy_equity_gbp is None or strategy_equity_gbp.empty:
        return {"render": "No strategy equity to compare.", "verdict": "NO DATA"}

    benchmark_ticker = benchmark_ticker or config.BENCHMARK_TICKER
    capital = starting_capital_gbp or float(config.STARTING_CAPITAL_GBP)

    # tz-naive so reindex aligns cleanly
    se = strategy_equity_gbp.copy()
    if se.index.tz is not None:
        se.index = se.index.tz_localize(None)

    start_ts = pd.Timestamp(start) if start else se.index[0]
    end_ts   = pd.Timestamp(end)   if end   else se.index[-1]

    # Fetch benchmark and convert to equity curve
    try:
        bench_df = data.get_history(
            benchmark_ticker,
            start=start_ts.strftime("%Y-%m-%d"),
            end=end_ts.strftime("%Y-%m-%d"),
        )
    except Exception as e:
        log.error("Benchmark fetch failed (%s): %s", benchmark_ticker, e)
        bench_df = pd.DataFrame()

    if bench_df is None or bench_df.empty:
        return {
            "render": f"Could not fetch benchmark {benchmark_ticker}.",
            "verdict": "NO BENCHMARK DATA",
            "benchmark_ticker": benchmark_ticker,
        }

    bench_close = bench_df["Close"].copy()
    if bench_close.index.tz is not None:
        bench_close.index = bench_close.index.tz_localize(None)

    # Restrict both series to the overlap, then build aligned equity curves
    overlap = se.index.intersection(bench_close.index)
    if len(overlap) < 30:
        return {
            "render": f"Strategy and benchmark have <30 overlapping days; "
                      f"comparison meaningless.",
            "verdict": "INSUFFICIENT OVERLAP",
            "benchmark_ticker": benchmark_ticker,
        }

    se_aligned = se.reindex(overlap).ffill()
    bench_aligned = bench_close.reindex(overlap).ffill()
    bench_eq = bench_aligned / bench_aligned.iloc[0] * capital

    # Compute metrics for both
    strat_m = _curve_metrics(se_aligned)
    bench_m = _curve_metrics(bench_eq)

    edge = _compute_edge(strat_m, bench_m)
    verdict, summary = _verdict(strat_m, bench_m, edge)

    render = _render_report(
        strategy=strat_m, benchmark=bench_m, edge=edge,
        verdict=verdict, summary=summary,
        benchmark_ticker=benchmark_ticker,
        period=(overlap[0].date(), overlap[-1].date()),
    )

    return {
        "strategy":         strat_m.as_dict(),
        "benchmark":        bench_m.as_dict(),
        "edge":             edge,
        "verdict":          verdict,
        "render":           render,
        "benchmark_ticker": benchmark_ticker,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _compute_edge(strat: CurveMetrics, bench: CurveMetrics) -> dict:
    """
    Edge = strategy - benchmark per metric.  Positive = strategy is better.

    For metrics where lower-is-better (max_dd_pct, days_underwater), we flip
    the sign so positive still means "strategy better".

    For worst_month_pct: this is already a negative number for both sides.
    A LESS negative worst month is better, so we want strategy - benchmark
    directly (no flip).  Example: strat -8.55 vs bench -11.40 →
    raw diff = +2.85, which already correctly means "strategy better".
    """
    sd = strat.as_dict()
    bd = bench.as_dict()
    edge = {}
    for k, v in sd.items():
        diff = v - bd[k]
        if k in ("max_dd_pct", "days_underwater"):
            diff = -diff   # lower is better → flip so positive = strategy better
        edge[k] = diff
    return edge


def _verdict(strat: CurveMetrics, bench: CurveMetrics, edge: dict) -> tuple[str, str]:
    """
    Decide a clear verdict.

    The metric that matters MOST is Sharpe (risk-adjusted return) followed by
    Calmar (return relative to max drawdown).  If the strategy can't beat the
    benchmark on at least one of these two, it's not worth running.
    """
    wins = sum(1 for v in edge.values() if v > 0)
    total = len(edge)

    sharpe_wins = strat.sharpe > bench.sharpe
    calmar_wins = strat.calmar > bench.calmar
    cagr_wins = strat.cagr_pct > bench.cagr_pct

    if sharpe_wins and calmar_wins:
        return ("✅ STRATEGY WINS",
                f"Beats VWRP on Sharpe AND Calmar — both risk-adjusted return measures. "
                f"Won {wins} of {total} metrics overall.")
    elif sharpe_wins or calmar_wins:
        return ("⚠️  MIXED",
                f"Beats VWRP on {'Sharpe' if sharpe_wins else 'Calmar'} but not both. "
                f"Higher return often comes with higher risk — verify the trade-off "
                f"is worth the bot's complexity.")
    elif cagr_wins:
        return ("⚠️  STRATEGY WORSE RISK-ADJUSTED",
                "Beats VWRP on raw CAGR but loses on both Sharpe and Calmar — meaning "
                "you took on more risk to earn the same money.  In a £30k ISA, this is "
                "almost never the right trade-off.")
    else:
        return ("❌ STRATEGY LOSES",
                f"Loses to VWRP on CAGR, Sharpe, AND Calmar.  Just buy and hold; "
                f"the bot isn't earning its keep.")


def _fmt_edge(value: float, fmt: str = ".2f", suffix: str = "") -> str:
    """+1.23 / -0.45 style formatting with explicit sign."""
    if value > 0:
        return f"+{value:{fmt}}{suffix}"
    elif value < 0:
        return f"{value:{fmt}}{suffix}"
    return f"0{suffix}"


def _render_report(strategy: CurveMetrics, benchmark: CurveMetrics, edge: dict,
                   verdict: str, summary: str,
                   benchmark_ticker: str, period: tuple) -> str:
    s, b = strategy, benchmark
    lines = [
        "=" * 78,
        f"  STRATEGY vs {benchmark_ticker.upper()} BUY & HOLD",
        f"  Period: {period[0]} → {period[1]}",
        "=" * 78,
        "",
        f"  {'Metric':<30} {'Strategy':>12} {'Benchmark':>12} {'Edge':>14}",
        "  " + "-" * 70,
        f"  {'Final equity (£)':<30} {s.final_equity:>12,.0f} {b.final_equity:>12,.0f} "
        f"{_fmt_edge(edge['final_equity'], ',.0f'):>14}",
        f"  {'CAGR (%)':<30} {s.cagr_pct:>12.2f} {b.cagr_pct:>12.2f} "
        f"{_fmt_edge(edge['cagr_pct'], '.2f', ' pp'):>14}",
        f"  {'Max drawdown (%)':<30} {s.max_dd_pct:>12.2f} {b.max_dd_pct:>12.2f} "
        f"{_fmt_edge(edge['max_dd_pct'], '.2f', ' pp'):>14}  (lower=better)",
        f"  {'Sharpe ratio':<30} {s.sharpe:>12.3f} {b.sharpe:>12.3f} "
        f"{_fmt_edge(edge['sharpe'], '.3f'):>14}",
        f"  {'Sortino ratio':<30} {s.sortino:>12.3f} {b.sortino:>12.3f} "
        f"{_fmt_edge(edge['sortino'], '.3f'):>14}",
        f"  {'Calmar ratio':<30} {s.calmar:>12.3f} {b.calmar:>12.3f} "
        f"{_fmt_edge(edge['calmar'], '.3f'):>14}",
        f"  {'Days underwater (max)':<30} {s.days_underwater:>12d} {b.days_underwater:>12d} "
        f"{_fmt_edge(edge['days_underwater'], 'd'):>14}  (lower=better)",
        f"  {'Best month (%)':<30} {s.best_month_pct:>12.2f} {b.best_month_pct:>12.2f} "
        f"{_fmt_edge(edge['best_month_pct'], '.2f', ' pp'):>14}",
        f"  {'Worst month (%)':<30} {s.worst_month_pct:>12.2f} {b.worst_month_pct:>12.2f} "
        f"{_fmt_edge(edge['worst_month_pct'], '.2f', ' pp'):>14}",
        "",
        "=" * 78,
        f"  VERDICT: {verdict}",
        "=" * 78,
        "",
        f"  {summary}",
        "",
        "  KEY: Sharpe ratio measures return per unit of total volatility.",
        "       Calmar ratio measures return per unit of MAX DRAWDOWN — i.e.",
        "       'is the worst-case ride worth the average return?'  These two",
        "       are the metrics that matter for ISA wealth-building.  Pure CAGR",
        "       is misleading because a strategy that earns 30% with 50% DD is",
        "       worse than one earning 15% with 10% DD for the same end equity",
        "       once you factor in the psychological cost of drawdown.",
        "",
    ]
    return "\n".join(lines)
