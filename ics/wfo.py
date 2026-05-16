"""
wfo.py
------
Walk-forward optimisation with point-in-time NASDAQ-100 constituents.

Changes from the previous version
----------------------------------
1. Universe is now survivorship-bias-free.
   Each IS window calls `constituents.get_universe_at(is_start)` so the
   backtest can only see stocks that were actually in the NASDAQ-100 on that
   date — including the ones that were later removed, went bust, or derated.
   The original code scanned a hand-picked 2024-25 momentum list back to 2019;
   only 29 of those 167 tickers were ever in the NASDAQ-100 in 2019.

2. OOS also uses point-in-time membership.
   The OOS window uses `get_universe_at(oos_start)` independently, so a stock
   that entered the index mid-backtest can't appear in an earlier OOS window.

3. Library fallback.
   If nasdaq_100_ticker_history is not installed the code prints a warning and
   falls back to the watchlist / BASE_UNIVERSE so the WFO still runs (just
   with survivorship bias).  Install with:
     pip install git+https://github.com/jmccarrell/n100tickers.git

Everything else (IS grid search, OOS stitching, reports) is unchanged.
"""
from __future__ import annotations

import itertools
import logging
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

# tqdm and rich are listed as hard dependencies in requirements.txt, but we
# fall back gracefully if either is missing so this module — and any test
# that imports it — never depends on the runtime environment having them.
# (The previous behaviour was to pip-install at import time, which breaks
# in PEP 668 / externally-managed environments and is generally a footgun.)
try:
    from tqdm import tqdm
except ImportError:  # pragma: no cover
    def tqdm(iterable=None, **_kwargs):  # type: ignore[no-redef]
        """No-op replacement when tqdm isn't installed."""
        return iterable if iterable is not None else iter(())

try:
    from rich.console import Console
except ImportError:  # pragma: no cover
    import re as _re

    class Console:  # type: ignore[no-redef]
        """Minimal print-only stand-in for rich.console.Console.

        Strips rich-style markup like [bold] / [/] so the output is readable
        even without rich installed.  Only `print` is needed by this module.
        """
        # Match rich markup: [tag], [tag args], [/tag], or bare [/]
        _MARKUP = _re.compile(r"\[/?(?:[a-zA-Z][^\[\]]*)?\]")

        def print(self, *args, **_kwargs):
            text = " ".join(self._MARKUP.sub("", str(a)) for a in args)
            print(text)

from . import config, watchlist
from .backtest import Backtester
from .constituents import get_universe_at, check_library
from .logging_utils import get_logger

console = Console()
log = get_logger("ics.wfo")


# ---------------------------------------------------------------------------
# Default parameter grid (small on purpose — large grids invite overfit)
#
# The grid is intentionally narrow: 3 signal params + 2 regime params = 5,
# giving 4 * 2 * 3 * 2 * 2 = 96 combos per IS window.  More than that and
# you start optimising for noise.
# ---------------------------------------------------------------------------
DEFAULT_PARAM_GRID: Dict[str, List] = {
    # Signal/risk params (existing)
    "rsi_min":            [50.0, 55.0, 60.0],
    "atr_stop_mult":      [1.75, 2.0, 2.5],
    "target_rr_multiple": [2.5, 3.0],
    # Regime filter params (let the WFO find the best combo)
    "vix_max":              [25.0, 999.0],         # 999 = VIX check disabled
    "max_spy_drawdown_pct": [0.05, 0.99],          # 0.99 = drawdown check disabled
    # NOTE: require_weekly_hma_bullish was REMOVED from the grid (May 2026).
    # Stability analysis showed it as NOISE (54%/46% across pooled windows).
    # Re-add only if stability conditions change (different universe, regime).
}
# 3 × 3 × 2 × 2 × 2 = 72 combinations per IS window


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_windows(
    start: str, end: Optional[str],
    is_days: int, oos_days: int, step_days: int,
) -> List[dict]:
    start_ts = pd.Timestamp(start).normalize()
    end_ts = (pd.Timestamp(end).normalize() if end
              else pd.Timestamp.utcnow().tz_localize(None).normalize())
    windows = []
    cur = start_ts
    is_td, oos_td, step_td = (timedelta(days=d) for d in (is_days, oos_days, step_days))
    while True:
        is_end   = cur + is_td
        oos_end  = is_end + oos_td
        if oos_end > end_ts:
            break
        windows.append({
            "is_start":  cur.date(),
            "is_end":    is_end.date(),
            "oos_start": is_end.date(),
            "oos_end":   oos_end.date(),
        })
        cur += step_td
    return windows


def _grid_combos(grid: Dict[str, List]) -> List[Dict]:
    keys = list(grid.keys())
    return [dict(zip(keys, v)) for v in itertools.product(*[grid[k] for k in keys])]


def _apply_combo(combo: Dict) -> Tuple[config.SignalParams, config.RiskParams, config.RegimeFilters]:
    """
    Patch SignalParams + RiskParams + RegimeFilters with the given combo.
    Returns immutable copies — global config is never mutated.
    """
    sp = replace(config.SIGNAL_PARAMS)
    rp = replace(config.RISK_PARAMS)
    rf = replace(config.REGIME_FILTERS)
    for k, v in combo.items():
        if hasattr(sp, k):
            sp = replace(sp, **{k: v})
        elif hasattr(rp, k):
            rp = replace(rp, **{k: v})
        elif hasattr(rf, k):
            rf = replace(rf, **{k: v})
        else:
            log.warning("Unknown param %s in combo, ignoring.", k)
    return sp, rp, rf


def _objective_value(summary: dict, objective: str) -> float:
    if int(summary.get("n_trades", 0)) < 5:
        return float("-inf")
    key_map = {
        "sharpe":        "sharpe",
        "cagr":          "cagr_pct",
        "calmar":        "calmar",
        "profit_factor": "profit_factor",
    }
    v = summary.get(key_map.get(objective, "sharpe"), 0.0)
    if v is None:
        return float("-inf")
    try:
        v = float(v)
    except (TypeError, ValueError):
        return float("-inf")
    return float("-inf") if v != v else v  # NaN guard


def _get_pit_tickers(as_of: str, universe_cap: int,
                     universe: str = "nasdaq100") -> List[str]:
    """
    Return the point-in-time constituents for the requested universe.

    Parameters
    ----------
    as_of : str  — ISO date
    universe_cap : int  — cap on returned ticker count (saves WFO compute)
    universe : "nasdaq100" | "sp500"
        - nasdaq100: uses constituents.get_universe_at (back to 2015)
        - sp500: uses sp500_constituents.get_universe_at (back to 1996)
    """
    universe = universe.lower()
    if universe == "sp500":
        from .sp500_constituents import get_universe_at as get_spx
        tickers = get_spx(as_of, cap=universe_cap)
    else:
        # default / NDX
        tickers = get_universe_at(as_of, fallback_to_base_universe=True)
        tickers = tickers[:universe_cap]
    return tickers


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def run_wfo(
    tickers: Optional[List[str]] = None,   # ignored when using PIT constituents
    from_watchlist: bool = False,           # used only if library unavailable
    universe_cap: int = 105,               # NDX ~100-103; SPX caps automatically
    start: str = "2019-01-01",
    end: Optional[str] = None,
    objective: str = "sharpe",
    name: str = "wfo",
    is_days: int = 504,
    oos_days: int = 252,
    step_days: int = 252,
    starting_capital_gbp: Optional[float] = None,
    param_grid: Optional[Dict[str, List]] = None,
    mc: bool = False,
    universe: str = "nasdaq100",  # "nasdaq100" or "sp500"
) -> Dict:
    universe = universe.lower()
    if universe not in ("nasdaq100", "sp500"):
        raise ValueError(f"universe must be 'nasdaq100' or 'sp500', got {universe!r}")

    console.print(
        f"[bold green]🚀 ICS Walk-Forward Optimisation "
        f"(Point-in-Time {universe.upper()} Universe)[/]"
    )
    console.print(f"Period: {start} → {end or 'today'} | Objective: {objective}")

    # Universe availability check
    use_pit = False
    if universe == "sp500":
        from .sp500_constituents import check_library as check_spx, available_from
        if check_spx():
            af = available_from()
            console.print(
                "[green]✓ Using point-in-time S&P 500 constituents "
                f"(survivorship-bias-free, data from {af})[/]"
            )
            use_pit = True
        else:
            console.print(
                "[yellow]⚠ SPX constituent dataset unavailable.  Falling back.[/]"
            )
    else:
        if check_library():
            console.print(
                "[green]✓ Using point-in-time NASDAQ-100 constituents "
                "(survivorship-bias-free, data from 2015)[/]"
            )
            use_pit = True
        else:
            console.print(
                "[yellow]⚠ nasdaq_100_ticker_history not installed — "
                "falling back to watchlist / BASE_UNIVERSE (survivorship-biased).\n"
                "  Install: pip install git+https://github.com/jmccarrell/n100tickers.git[/]"
            )

    grid   = param_grid or DEFAULT_PARAM_GRID
    combos = _grid_combos(grid)
    console.print(f"Grid: {len(combos)} combinations × IS window")

    windows = _make_windows(start, end, is_days, oos_days, step_days)
    console.print(f"Windows: [bold]{len(windows)}[/]\n")
    if not windows:
        console.print(
            "[yellow]Not enough history for any window. "
            "Try moving --start earlier or reducing is_days.[/]"
        )
        return {"summary": {"n_trades": 0}, "os_equity_gbp": pd.Series(dtype=float),
                "os_trades": pd.DataFrame(), "windows": []}

    capital = starting_capital_gbp or float(config.STARTING_CAPITAL_GBP)
    rolling_equity = capital

    rows: List[dict] = []
    all_oos_trades: List[pd.DataFrame] = []
    all_oos_equity: List[pd.Series] = []

    for i, win in enumerate(tqdm(windows, desc="WFO", unit="window", ncols=110)):
        console.print(
            f"\n[bold]Window {i+1}/{len(windows)}[/] "
            f"IS {win['is_start']}→{win['is_end']} | "
            f"OOS {win['oos_start']}→{win['oos_end']}"
        )

        # --- IS universe: tickers in NDX at the START of the IS window ---
        if use_pit:
            is_tickers  = _get_pit_tickers(str(win["is_start"]),  universe_cap, universe=universe)
            oos_tickers = _get_pit_tickers(str(win["oos_start"]), universe_cap, universe=universe)
            console.print(
                f"   Universe: {len(is_tickers)} IS tickers / "
                f"{len(oos_tickers)} OOS tickers (point-in-time NDX)"
            )
        else:
            # Fallback: use watchlist if from_watchlist, else BASE_UNIVERSE
            fb = watchlist.get_tickers() if from_watchlist else []
            if not fb:
                fb = config.BASE_UNIVERSE[:universe_cap]
            is_tickers = oos_tickers = fb[:universe_cap]
            console.print(f"   Universe: {len(is_tickers)} tickers (fallback)")

        if not is_tickers:
            console.print("   [red]No tickers — skipping window[/]")
            continue

        # --- IS grid search ---
        best_combo, best_score = None, float("-inf")
        # Contributions ARE included inside WFO windows.  Reasoning:
        #   1. The cap-scaling logic in Backtester behaves differently when
        #      contributions are on vs off — sizing later in a window is
        #      bigger.  WFO has to test the strategy as it will run live.
        #   2. Contributions inflate every IS combo's CAGR/Sharpe equally,
        #      so the *relative* ranking of combos is preserved, which is
        #      what IS grid search uses.
        #   3. Both baseline and variant comparisons run with contributions,
        #      so OOS metric differences are an honest delta even though the
        #      absolute numbers are inflated.
        # Pass an explicit config so we don't accidentally pick up a future
        # change to the global default.
        wfo_contribs = config.CONTRIBUTIONS
        for combo in combos:
            sp, rp, rf = _apply_combo(combo)
            try:
                bt = Backtester(
                    tickers=is_tickers,
                    start=str(win["is_start"]), end=str(win["is_end"]),
                    starting_capital_gbp=capital,
                    signal_params=sp, risk_params=rp, regime_filters=rf,
                    contributions=wfo_contribs,
                )
                res   = bt.run()
                score = _objective_value(res.summary, objective)
                if score > best_score:
                    best_score, best_combo = score, combo
            except Exception as e:
                log.warning("IS combo failed (%s): %s", combo, e)

        if best_combo is None:
            console.print("   [yellow]No valid IS combo — skipping window[/]")
            rows.append({
                "Window": f"{win['oos_start']}→{win['oos_end']}",
                "Universe": "PIT" if use_pit else "Fallback",
                "IS_tickers": len(is_tickers),
                "IS_objective": objective, "IS_score": 0,
                "OOS_CAGR_%": 0, "OOS_MaxDD_%": 0, "OOS_PF": 0,
                "OOS_Sharpe": 0, "OOS_Trades": 0, "Params": "(none)",
            })
            continue

        console.print(
            f"   IS best: [cyan]{best_combo}[/] → {objective}={best_score:.3f}"
        )

        # --- OOS: run best IS combo on the OOS universe + date range ---
        sp, rp, rf = _apply_combo(best_combo)
        try:
            bt = Backtester(
                tickers=oos_tickers,
                start=str(win["oos_start"]), end=str(win["oos_end"]),
                starting_capital_gbp=rolling_equity,
                signal_params=sp, risk_params=rp, regime_filters=rf,
                contributions=wfo_contribs,
            )
            oos = bt.run()
        except Exception as e:
            console.print(f"   [red]OOS run failed: {e}[/]")
            continue

        if not oos.equity_gbp.empty:
            all_oos_equity.append(oos.equity_gbp)
            rolling_equity = float(oos.equity_gbp.iloc[-1])
        if not oos.trades.empty:
            all_oos_trades.append(oos.trades)

        s = oos.summary
        rows.append({
            "Window":        f"{win['oos_start']}→{win['oos_end']}",
            "Universe":      ("PIT-" + universe.upper()) if use_pit else "Fallback",
            "IS_tickers":    len(is_tickers),
            "IS_objective":  objective,
            "IS_score":      round(best_score, 3),
            "OOS_CAGR_%":    round(s.get("cagr_pct", 0) * 100, 2),
            "OOS_MaxDD_%":   round(s.get("max_drawdown_pct", 0) * 100, 2),
            "OOS_PF":        round(s.get("profit_factor", 0) or 0, 3),
            "OOS_Sharpe":    round(s.get("sharpe", 0) or 0, 3),
            "OOS_Trades":    int(s.get("n_trades", 0)),
            "Params":        ", ".join(f"{k}={v}" for k, v in best_combo.items()),
            # Keep the structured best_combo too — used by stability analysis.
            # Not rendered in the table (Pandas printers ignore dict columns
            # when to_string is called) but available in the returned dict.
            "best_combo":    dict(best_combo),
        })
        console.print(
            f"   → OOS [green]{s.get('cagr_pct', 0)*100:.1f}% CAGR[/] | "
            f"[yellow]{s.get('max_drawdown_pct', 0)*100:.1f}% DD[/] | "
            f"Sharpe {s.get('sharpe', 0):.2f} | "
            f"PF {s.get('profit_factor', 0):.2f} | "
            f"{s.get('n_trades', 0)} trades"
        )

    # --- Aggregate ---
    console.print("\n" + "═" * 110)
    console.print("[bold green]WALK-FORWARD OUT-OF-SAMPLE SUMMARY[/]")
    df = pd.DataFrame(rows)
    if not df.empty:
        # Hide the structured best_combo column from display + CSV;
        # it's still available via the returned dict's "windows" entry
        # for downstream consumers (stability analysis, etc).
        display_df = df.drop(columns=["best_combo"], errors="ignore")
        console.print(display_df.to_string(index=False))

    out_dir = Path("data/reports") / f"wfo_{name}"
    out_dir.mkdir(parents=True, exist_ok=True)
    if not df.empty:
        display_df.to_csv(out_dir / "wfo_summary.csv", index=False)

    os_trades = (pd.concat(all_oos_trades, ignore_index=True)
                 if all_oos_trades else pd.DataFrame())
    if all_oos_equity:
        os_equity = pd.concat(all_oos_equity).sort_index()
        os_equity = os_equity[~os_equity.index.duplicated(keep="last")]
        os_equity.name = "equity_gbp"
    else:
        os_equity = pd.Series(dtype=float, name="equity_gbp")

    from .performance import summarize
    summary = summarize(os_equity, os_trades) if not os_equity.empty else {"n_trades": 0}
    summary["windows"]          = df.to_dict("records") if not df.empty else []
    summary["objective"]        = objective
    summary["start_equity_gbp"] = capital
    summary["end_equity_gbp"]   = (float(os_equity.iloc[-1])
                                   if not os_equity.empty else capital)
    summary["universe"]         = ("PIT-" + universe.upper()) if use_pit else "Fallback"

    console.print(f"\n[bold green]✅ WFO done.[/] Reports → [cyan]{out_dir}[/]")
    return {
        "summary":      summary,
        "os_equity_gbp":os_equity,
        "os_trades":    os_trades,
        "windows":      df.to_dict("records") if not df.empty else [],
    }


def cmd_wfo(args):
    return run_wfo(
        from_watchlist=getattr(args, "from_watchlist", False),
        start=args.start,
        end=getattr(args, "end", None),
        objective=getattr(args, "objective", "sharpe"),
        name=getattr(args, "name", "wfo"),
        is_days=getattr(args, "is_days", 504),
        oos_days=getattr(args, "oos_days", 252),
        step_days=getattr(args, "step_days", 252),
        mc=getattr(args, "mc", False),
    )


if __name__ == "__main__":
    run_wfo(start="2019-01-01", name="test")
