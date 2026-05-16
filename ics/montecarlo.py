"""
montecarlo.py
-------------
Monte Carlo robustness testing for a trade list.

1) shuffle_mc      : shuffle realised trade order, recompute equity path.
2) parametric_mc   : draw from win/loss distributions with jittered win-rate
                     and slippage; runs full mc_runs simulations.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from . import config
from .performance import cagr, max_drawdown, sharpe
from .logging_utils import get_logger

log = get_logger("ics.mc")


def _equity_from_trades(starting_capital: float, returns_seq: np.ndarray) -> np.ndarray:
    eq = np.empty(len(returns_seq) + 1)
    eq[0] = starting_capital
    for i, r in enumerate(returns_seq, 1):
        eq[i] = eq[i - 1] * (1.0 + r)
    return eq


def shuffle_mc(
    trades: pd.DataFrame, starting_capital_gbp: float = None,
    runs: int = None, seed: Optional[int] = 42,
) -> pd.DataFrame:
    starting_capital_gbp = starting_capital_gbp or config.STARTING_CAPITAL_GBP
    runs = runs or config.BACKTEST_PARAMS.mc_runs
    if trades.empty or "pnl_gbp" not in trades.columns:
        return pd.DataFrame()

    trade_returns = (trades["pnl_gbp"] / starting_capital_gbp).to_numpy(dtype=float)
    rng = np.random.default_rng(seed)
    rows = []
    for _ in range(runs):
        order = rng.permutation(len(trade_returns))
        path = _equity_from_trades(starting_capital_gbp, trade_returns[order])
        eq_series = pd.Series(path)
        rows.append(dict(
            end_equity_gbp=float(path[-1]),
            total_return_pct=float(path[-1] / starting_capital_gbp - 1.0),
            cagr_pct=cagr(eq_series, periods_per_year=config.BACKTEST_PARAMS.trading_days_per_year),
            max_drawdown_pct=max_drawdown(eq_series),
            sharpe=sharpe(eq_series, periods_per_year=config.BACKTEST_PARAMS.trading_days_per_year),
        ))
    df = pd.DataFrame(rows)
    log.info("Shuffle MC done (%d runs): median end £%.0f, p5 £%.0f, p95 £%.0f",
             len(df), df["end_equity_gbp"].median(),
             df["end_equity_gbp"].quantile(0.05), df["end_equity_gbp"].quantile(0.95))
    return df


def parametric_mc(
    trades: pd.DataFrame, starting_capital_gbp: float = None,
    runs: int = None,
    slippage_jitter_pct: Optional[float] = None,
    winrate_jitter: Optional[float] = None,
    seed: Optional[int] = 7,
) -> pd.DataFrame:
    starting_capital_gbp = starting_capital_gbp or config.STARTING_CAPITAL_GBP
    bp = config.BACKTEST_PARAMS
    runs = runs or bp.mc_runs
    sj = slippage_jitter_pct if slippage_jitter_pct is not None else bp.mc_slippage_jitter_pct
    wj = winrate_jitter if winrate_jitter is not None else bp.mc_winrate_jitter

    if trades.empty or "pnl_gbp" not in trades.columns:
        return pd.DataFrame()

    rets = (trades["pnl_gbp"] / starting_capital_gbp).to_numpy(dtype=float)
    n = len(rets)
    wins = rets[rets > 0]
    losses = rets[rets <= 0]
    if len(wins) == 0 or len(losses) == 0:
        log.warning("Parametric MC needs both wins and losses; falling back to shuffle MC.")
        return shuffle_mc(trades, starting_capital_gbp, runs, seed)

    p = len(wins) / n
    rng = np.random.default_rng(seed)
    rows = []
    for _ in range(runs):
        p_jit = float(np.clip(p + rng.uniform(-wj, wj), 0.05, 0.95))
        seq = np.empty(n)
        for i in range(n):
            if rng.random() < p_jit:
                base = float(rng.choice(wins))
            else:
                base = float(rng.choice(losses))
            slip = rng.uniform(-sj, sj)
            seq[i] = base + slip
        path = _equity_from_trades(starting_capital_gbp, seq)
        eq_series = pd.Series(path)
        rows.append(dict(
            end_equity_gbp=float(path[-1]),
            total_return_pct=float(path[-1] / starting_capital_gbp - 1.0),
            cagr_pct=cagr(eq_series, bp.trading_days_per_year),
            max_drawdown_pct=max_drawdown(eq_series),
            sharpe=sharpe(eq_series, bp.risk_free_rate, bp.trading_days_per_year),
            win_rate=p_jit,
        ))
    df = pd.DataFrame(rows)
    log.info("Parametric MC done (%d runs): median CAGR %.2f%%, p5 MDD %.2f%%, p95 MDD %.2f%%",
             len(df), df["cagr_pct"].median() * 100,
             df["max_drawdown_pct"].quantile(0.05) * 100,
             df["max_drawdown_pct"].quantile(0.95) * 100)
    return df


def percentiles(df: pd.DataFrame, cols: Optional[list] = None,
                qs=(0.05, 0.25, 0.50, 0.75, 0.95)) -> pd.DataFrame:
    if df.empty:
        return df
    cols = cols or list(df.columns)
    return df[cols].quantile(list(qs)).rename(index=lambda q: f"p{int(q*100):02d}")
