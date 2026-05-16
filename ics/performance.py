"""
performance.py
--------------
Performance metrics on a GBP equity curve and a trade list.
v2: defensive checks for obviously-broken inputs (empty, NaN, runaway equity).
"""
from __future__ import annotations

from typing import Dict

import numpy as np
import pandas as pd

from . import config


def _to_returns(equity: pd.Series) -> pd.Series:
    if equity.empty:
        return pd.Series(dtype=float)
    return equity.pct_change().replace([np.inf, -np.inf], np.nan).dropna()


def cagr(equity: pd.Series, periods_per_year: int = 252) -> float:
    if len(equity) < 2 or equity.iloc[0] <= 0:
        return 0.0
    n_years = len(equity) / periods_per_year
    if n_years <= 0:
        return 0.0
    ratio = equity.iloc[-1] / equity.iloc[0]
    if ratio <= 0:
        return -1.0
    return float(ratio ** (1 / n_years) - 1.0)


def sharpe(equity: pd.Series, rf: float = 0.0, periods_per_year: int = 252) -> float:
    rets = _to_returns(equity)
    if len(rets) < 2 or rets.std() == 0:
        return 0.0
    excess = rets - rf / periods_per_year
    return float(excess.mean() / rets.std() * np.sqrt(periods_per_year))


def sortino(equity: pd.Series, rf: float = 0.0, periods_per_year: int = 252) -> float:
    rets = _to_returns(equity)
    if len(rets) < 2:
        return 0.0
    downside = rets[rets < 0]
    if len(downside) == 0 or downside.std() == 0:
        return 0.0
    excess = rets - rf / periods_per_year
    return float(excess.mean() / downside.std() * np.sqrt(periods_per_year))


def max_drawdown(equity: pd.Series) -> float:
    """Returns max drawdown as positive fraction (0..1). 0 if input invalid."""
    if equity.empty:
        return 0.0
    if (equity <= 0).any():
        return 1.0  # blew up
    running_max = equity.cummax()
    dd = (equity - running_max) / running_max
    return float(-dd.min())


def calmar(equity: pd.Series, periods_per_year: int = 252) -> float:
    mdd = max_drawdown(equity)
    if mdd == 0:
        return 0.0
    return cagr(equity, periods_per_year) / mdd


def trade_stats(trades: pd.DataFrame) -> Dict[str, float]:
    if trades.empty or "pnl_gbp" not in trades.columns:
        return dict(n_trades=0, win_rate=0.0, avg_win=0.0, avg_loss=0.0,
                    profit_factor=0.0, expectancy_gbp=0.0)
    pnl = trades["pnl_gbp"].dropna()
    if pnl.empty:
        return dict(n_trades=0, win_rate=0.0, avg_win=0.0, avg_loss=0.0,
                    profit_factor=0.0, expectancy_gbp=0.0)
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    win_rate = float(len(wins) / len(pnl))
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(losses.mean()) if len(losses) else 0.0
    if losses.sum() < 0:
        pf = float(wins.sum() / -losses.sum())
    elif wins.sum() > 0:
        pf = float("inf")
    else:
        pf = 0.0
    expectancy = float(pnl.mean())
    return dict(
        n_trades=int(len(pnl)),
        win_rate=win_rate,
        avg_win=avg_win,
        avg_loss=avg_loss,
        profit_factor=pf,
        expectancy_gbp=expectancy,
    )


def summarize(equity: pd.Series, trades: pd.DataFrame) -> Dict[str, float]:
    bp = config.BACKTEST_PARAMS
    if equity.empty:
        return dict(start_equity_gbp=0.0, end_equity_gbp=0.0, total_return_pct=0.0,
                    cagr_pct=0.0, sharpe=0.0, sortino=0.0, max_drawdown_pct=0.0,
                    calmar=0.0, **trade_stats(trades))
    s = {
        "start_equity_gbp": float(equity.iloc[0]),
        "end_equity_gbp": float(equity.iloc[-1]),
        "total_return_pct": float(equity.iloc[-1] / equity.iloc[0] - 1.0),
        "cagr_pct": cagr(equity, bp.trading_days_per_year),
        "sharpe": sharpe(equity, bp.risk_free_rate, bp.trading_days_per_year),
        "sortino": sortino(equity, bp.risk_free_rate, bp.trading_days_per_year),
        "max_drawdown_pct": max_drawdown(equity),
        "calmar": calmar(equity, bp.trading_days_per_year),
    }
    s.update(trade_stats(trades))
    return s


def vs_benchmark(strategy_equity: pd.Series, benchmark_prices: pd.Series,
                 starting_capital_gbp: float) -> pd.DataFrame:
    """Side-by-side strategy vs buy-and-hold benchmark, both starting at capital."""
    if benchmark_prices.empty or strategy_equity.empty:
        return pd.DataFrame()
    # tz-naive align
    if strategy_equity.index.tz is not None:
        strategy_equity.index = strategy_equity.index.tz_localize(None)
    bench = benchmark_prices.copy()
    if hasattr(bench.index, "tz") and bench.index.tz is not None:
        bench.index = bench.index.tz_localize(None)

    bench = bench.reindex(strategy_equity.index).ffill().bfill()
    if bench.empty or bench.iloc[0] <= 0:
        return pd.DataFrame()
    bench_eq = (bench / bench.iloc[0]) * starting_capital_gbp
    out = pd.DataFrame({"strategy_gbp": strategy_equity, "benchmark_gbp": bench_eq})
    out["alpha_gbp"] = out["strategy_gbp"] - out["benchmark_gbp"]
    return out
