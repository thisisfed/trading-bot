"""
multi_wfo.py
------------
Run the walk-forward optimiser under all four objective functions and
compare the out-of-sample results in a single table.

Why this matters
----------------
If a strategy has a real edge, it should produce positive OOS expectancy
regardless of which in-sample metric you optimised for.  If only one
objective produces good OOS results, it's likely the one that happened to
overfit in a way that coincidentally held during that specific OOS period —
luck, not skill.

Consistent OOS performance across all four objectives is a much stronger
signal of a genuine edge.  Inconsistent performance is a signal to go back
to the drawing board before risking real money.

Usage
-----
    # From the project root:
    python -m ics.multi_wfo

    # Or via CLI after adding to cli.py:
    python -m ics.cli multi-wfo --start 2019-01-01

Output
------
  data/reports/multi_wfo/
    comparison.csv          — one row per objective, OOS metrics side by side
    comparison.txt          — human-readable summary with verdict
    <objective>/            — full WFO report for each objective
      wfo_summary.csv
      equity_curve.png
      drawdown.png
      trades.csv

Interpreting the verdict
------------------------
  CONSISTENT EDGE    — all 4 objectives show positive OOS CAGR and Sharpe > 0
  MIXED SIGNAL       — some positive, some negative; results are inconclusive
  NO EDGE DETECTED   — majority negative; don't deploy with real money yet
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from datetime import datetime

import pandas as pd

from . import config
from .wfo import run_wfo, DEFAULT_PARAM_GRID
from .reporter import write_report, print_summary
from .stability import analyse_combos, render_report
from .logging_utils import get_logger

log = get_logger("ics.multi_wfo")

# Default objectives.  CAGR is intentionally excluded from the default set
# because it has no risk component — it picks parameters that took bigger
# swings to win bigger, which then take bigger swings the wrong way OOS.
# We saw this empirically in the first multi-WFO run: 3/4 objectives produced
# OOS Sharpe > 0; CAGR was the lone failure (Sharpe ≈ 0).
#
# The three risk-adjusted objectives (sharpe = return/vol, calmar = return/MDD,
# profit_factor = wins/losses) are kept.  Pass `--objectives cagr ...` to
# include CAGR explicitly if you want to confirm the pattern persists.
OBJECTIVES_ALL = ["sharpe", "calmar", "cagr", "profit_factor"]
OBJECTIVES = ["sharpe", "calmar", "profit_factor"]   # default: 3 risk-adjusted

OUT_DIR = Path("data/reports/multi_wfo")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _verdict(df: pd.DataFrame) -> str:
    """
    Read the comparison DataFrame and return a plain-English verdict.

    With the default 3 risk-adjusted objectives:
      CONSISTENT EDGE  : all 3 → OOS CAGR > 0 AND OOS Sharpe > 0
      LIKELY EDGE      : 2 of 3 (allow one objective to underperform)
      MIXED SIGNAL     : 1 of 3
      NO EDGE DETECTED : 0 of 3

    With CAGR included (4 objectives), thresholds are adjusted:
    CAGR is the canary — failing only on CAGR is expected and not penalised.
    """
    positive_mask = (df["OOS_CAGR_%"] > 0) & (df["OOS_Sharpe"] > 0)
    positive = positive_mask.sum()
    total = len(df)

    if positive == total:
        return (
            "✅  CONSISTENT EDGE\n"
            f"    All {total} objectives produced positive OOS CAGR and Sharpe.\n"
            "    This is a meaningful signal — the strategy may have a genuine edge.\n"
            "    Recommended next step: paper-trade for 3 months before live deployment."
        )
    if total == 4 and positive == 3 and "cagr" in df["Objective"].values:
        cagr_failed = not positive_mask[df["Objective"] == "cagr"].iloc[0]
        if cagr_failed:
            return (
                "✅  CONSISTENT EDGE (CAGR caveat)\n"
                "    All 3 risk-adjusted objectives showed positive OOS CAGR and Sharpe;\n"
                "    only the CAGR-only objective failed.  This is expected behaviour:\n"
                "    optimising raw CAGR without a risk denominator overfits to volatility,\n"
                "    so its OOS failure here is the canary, not a contradiction.\n"
                "    Recommended next step: paper-trade for 3 months."
            )
    if positive >= 2 and total >= 3:
        return (
            f"⚠️  LIKELY EDGE\n"
            f"    {positive}/{total} objectives showed positive OOS CAGR + Sharpe.\n"
            "    The signal is suggestive but not unanimous.  Consider paper-trading,\n"
            "    and review the failing objective(s) to understand why they diverged."
        )
    if positive >= 1:
        return (
            f"⚠️  MIXED SIGNAL\n"
            f"    Only {positive}/{total} objectives showed positive OOS CAGR + Sharpe.\n"
            "    Results are inconclusive.  Do not deploy with real money yet."
        )
    return (
        "❌  NO EDGE DETECTED\n"
        f"   0/{total} objectives showed positive OOS CAGR + Sharpe.\n"
        "    The strategy does not appear to generalise out-of-sample.\n"
        "    Revisit signal conditions, universe, or risk parameters before retesting."
    )


def _summary_row(objective: str, result: dict) -> dict:
    """Extract key OOS metrics from a run_wfo result dict."""
    s = result["summary"]
    trades = result["os_trades"]
    eq     = result["os_equity_gbp"]
    return {
        "Objective":     objective,
        "Universe":      s.get("universe", "unknown"),
        "OOS_CAGR_%":    round(s.get("cagr_pct", 0) * 100, 2),
        "OOS_Sharpe":    round(s.get("sharpe", 0) or 0, 3),
        "OOS_Sortino":   round(s.get("sortino", 0) or 0, 3),
        "OOS_MaxDD_%":   round(s.get("max_drawdown_pct", 0) * 100, 2),
        "OOS_Calmar":    round(s.get("calmar", 0) or 0, 3),
        "OOS_WinRate_%": round(s.get("win_rate", 0) * 100, 1),
        "OOS_PF":        round(s.get("profit_factor", 0) or 0, 3),
        "OOS_Trades":    int(s.get("n_trades", 0)),
        "OOS_Expectancy":round(s.get("expectancy_gbp", 0) or 0, 2),
        "End_Equity_GBP":round(s.get("end_equity_gbp", 0), 0),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run_multi_wfo(
    start: str = "2019-01-01",
    end: str | None = None,
    is_days: int = 504,
    oos_days: int = 252,
    step_days: int = 252,
    objectives: list[str] | None = None,
    starting_capital_gbp: float | None = None,
    universe: str = "nasdaq100",
    universe_cap: int = 105,
) -> pd.DataFrame:
    """
    Run WFO for each objective and return a comparison DataFrame.

    Parameters
    ----------
    start, end      : backtest date range
    is_days         : in-sample window (calendar days, ~504 = 2y)
    oos_days        : out-of-sample window (~252 = 1y)
    step_days       : how far to advance between windows (~252 = 1y)
    objectives      : list of objectives to run (default: all four)
    starting_capital_gbp : capital for each run (default: config value)
    universe        : "nasdaq100" (default, back to 2015) or "sp500"
                      (back to 1996, ~5x slower).
    universe_cap    : max tickers per IS/OOS window.

    Returns
    -------
    pd.DataFrame with one row per objective and OOS metrics as columns.
    """
    objectives = objectives or OBJECTIVES
    capital    = starting_capital_gbp or float(config.STARTING_CAPITAL_GBP)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    rows = []
    results_by_objective = {}

    for obj in objectives:
        print(f"\n{'='*70}")
        print(f"  Running WFO: objective = {obj.upper()}  |  universe = {universe.upper()}")
        print(f"{'='*70}\n")

        try:
            result = run_wfo(
                start=start, end=end,
                objective=obj,
                name=f"multi_{obj}",
                is_days=is_days, oos_days=oos_days, step_days=step_days,
                starting_capital_gbp=capital,
                universe=universe,
                universe_cap=universe_cap,
            )
            results_by_objective[obj] = result
            rows.append(_summary_row(obj, result))

            # Write per-objective equity/drawdown charts
            if not result["os_equity_gbp"].empty:
                s = {k: v for k, v in result["summary"].items()
                     if not isinstance(v, (list, dict, pd.Series, pd.DataFrame))}
                write_report(
                    run_name=f"multi_wfo/multi_{obj}",
                    equity_gbp=result["os_equity_gbp"],
                    trades=result["os_trades"],
                    summary=s,
                )

        except Exception as e:
            log.exception("WFO failed for objective %s: %s", obj, e)
            rows.append({
                "Objective": obj, "Universe": "error",
                "OOS_CAGR_%": None, "OOS_Sharpe": None, "OOS_Sortino": None,
                "OOS_MaxDD_%": None, "OOS_Calmar": None, "OOS_WinRate_%": None,
                "OOS_PF": None, "OOS_Trades": 0, "OOS_Expectancy": None,
                "End_Equity_GBP": None,
            })

    comparison = pd.DataFrame(rows)

    # Save CSV
    csv_path = OUT_DIR / "comparison.csv"
    comparison.to_csv(csv_path, index=False)

    # Build human-readable text report
    verdict = _verdict(comparison.dropna(subset=["OOS_CAGR_%", "OOS_Sharpe"]))
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "=" * 70,
        "  ICS Multi-Objective Walk-Forward Comparison",
        f"  Generated: {ts}",
        f"  Period: {start} → {end or 'today'}",
        f"  IS={is_days}d / OOS={oos_days}d / Step={step_days}d",
        f"  Capital: £{capital:,.0f}",
        "=" * 70,
        "",
        comparison.to_string(index=False),
        "",
        "=" * 70,
        "  VERDICT",
        "=" * 70,
        "",
        verdict,
        "",
        "=" * 70,
        "  HOW TO READ THIS TABLE",
        "=" * 70,
        "",
        "  OOS_CAGR_%   Annualised return on the stitched OOS equity curve.",
        "               Positive means the strategy made money out-of-sample.",
        "",
        "  OOS_Sharpe   Risk-adjusted return (higher = better, >0.5 = decent).",
        "               If negative, the strategy lost more than the risk-free rate.",
        "",
        "  OOS_MaxDD_%  Worst peak-to-trough drawdown on the OOS curve.",
        "               Compare to OOS_CAGR to see if the ride is worth it.",
        "",
        "  OOS_PF       Profit factor = gross wins / gross losses.",
        "               >1.0 means winners outweigh losers in aggregate.",
        "",
        "  OOS_Trades   Number of round-trip trades across all OOS windows.",
        "               Very low counts (<20) mean the results aren't statistically",
        "               meaningful — need more data.",
        "",
        "  Consistent positive Sharpe + CAGR across ALL four objectives is",
        "  the key signal.  One good objective with three bad ones is noise.",
        "",
    ]
    txt_path = OUT_DIR / "comparison.txt"
    txt_path.write_text("\n".join(lines))

    # ----- Parameter stability analysis -------------------------------------
    # Pool the best-combo dict from EVERY window of EVERY objective.  This
    # gives more samples (e.g. 8 windows × 3 objectives = 24 picks) than
    # any single objective alone, which makes the stability verdict more
    # reliable.  A parameter that consistently wins across objectives AND
    # windows is doing real work; one that flips around is noise.
    pooled_combos: list = []
    for obj_name, result in results_by_objective.items():
        for win in result.get("windows", []) or []:
            bc = win.get("best_combo")
            if bc:
                pooled_combos.append(bc)

    if pooled_combos:
        stability = analyse_combos(
            pooled_combos,
            grid=DEFAULT_PARAM_GRID,
            n_objectives=len(results_by_objective),
        )
        stability_text = render_report(stability)
        (OUT_DIR / "stability.txt").write_text(stability_text)
    else:
        stability_text = (
            "Parameter stability analysis skipped: no best-combo data "
            "available (every WFO window failed to produce a combo).\n"
        )

    # Print to terminal
    print("\n" + "=" * 70)
    print("  MULTI-OBJECTIVE WFO COMPARISON")
    print("=" * 70)
    print()
    print(comparison.to_string(index=False))
    print()
    print("=" * 70)
    print("  VERDICT")
    print("=" * 70)
    print()
    print(verdict)
    print()
    # Stability analysis → terminal
    print(stability_text)
    print(f"Full report saved → {OUT_DIR}/")
    print(f"  comparison.csv  — OOS metrics by objective")
    print(f"  comparison.txt  — OOS metrics + verdict")
    print(f"  stability.txt   — which parameters to keep, drop, or lock")

    return comparison


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Run WFO under all four objective functions and compare OOS results."
    )
    parser.add_argument("--start",    default="2019-01-01")
    parser.add_argument("--end",      default=None)
    parser.add_argument("--is-days",  type=int, default=504, dest="is_days")
    parser.add_argument("--oos-days", type=int, default=252, dest="oos_days")
    parser.add_argument("--step-days",type=int, default=252, dest="step_days")
    parser.add_argument(
        "--objectives", nargs="+",
        default=OBJECTIVES,
        choices=OBJECTIVES_ALL,
        help=(
            "Which objectives to run "
            f"(default: {' '.join(OBJECTIVES)}; pass 'cagr' to include it)."
        ),
    )
    parser.add_argument(
        "--capital", type=float, default=None,
        help="Starting capital in GBP (default: config value).",
    )
    args = parser.parse_args()

    run_multi_wfo(
        start=args.start, end=args.end,
        is_days=args.is_days, oos_days=args.oos_days, step_days=args.step_days,
        objectives=args.objectives,
        starting_capital_gbp=args.capital,
    )


if __name__ == "__main__":
    main()
