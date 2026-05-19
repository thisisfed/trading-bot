"""
26_drawdown_analysis.py  (v2)

Drawdown and risk-adjusted-return analysis for an ICS run.

== Preferred input: equity_gbp.csv ==
The portfolio equity series, written by every backtest/WFO run to
data/reports/<name>/. This is the correct source for drawdown analysis
on a multi-position strategy because it reflects actual portfolio equity
— including position sizing, concurrent trades, and cash drag.

== Optional: --contributions ==
If passed, cumulative contributions are subtracted from equity so all
metrics are computed on strategy P&L only. This is what you want when
judging strategy edge: ongoing capital injection inflates equity in
ways that have nothing to do with the strategy.

== Fallback input: trades CSV ==
If you point this script at a trades CSV (cols: return/return_pct,
entry_date/entry_ts, exit_date/exit_ts), it falls back to building an
APPROXIMATE equity curve by sequentially compounding per-trade returns.
This is wrong for multi-position strategies — it implicitly assumes
each trade bets 100% of equity — and dramatically inflates drawdowns.
The script will print a loud warning. Use only when no equity series
is available (e.g. external trade lists with no companion curve).

== Outputs ==
  - Total return, CAGR
  - Max drawdown: depth, peak/trough/recovery dates, days underwater
  - Top-N drawdown episodes (non-overlapping)
  - Pain index (avg DD), Ulcer index (RMS DD), % time underwater
  - Calmar (CAGR / |MaxDD|)
  - Sortino (annualised, from periodic equity returns)
  - Recovery factor (total return / |MaxDD|)
  - Verdict tiers + combined decision

== Usage ==
    # Correct path — analyse the bot's actual equity curve
    python src/26_drawdown_analysis.py data/reports/lunch_bt/equity_gbp.csv

    # Strip out capital contributions to isolate strategy P&L
    python src/26_drawdown_analysis.py data/reports/lunch_bt/equity_gbp.csv \\
        --contributions data/reports/lunch_bt/contributions_gbp.csv \\
        --label "Lunch BT (strategy only)"

    # Trade-CSV fallback — prints a warning and uses approximate equity
    python src/26_drawdown_analysis.py data/some_external_trades.csv
"""

import argparse
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd


# ===========================================================================
# Input loading — equity series or trades fallback
# ===========================================================================

def _load_equity_csv(path: Path) -> pd.Series:
    """Read a single-column equity series indexed by date."""
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    if df.shape[1] < 1:
        raise ValueError(f"{path} has no value column.")
    eq = df.iloc[:, 0].astype(float).sort_index()
    eq.name = df.columns[0]
    return eq


def _load_contributions_csv(path: Path) -> pd.Series:
    """Read a contributions series indexed by date (one inflow per row)."""
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    return df.iloc[:, 0].astype(float).sort_index()


def _trades_to_approx_equity(path: Path) -> pd.Series:
    """Fallback: build an APPROXIMATE equity curve from trade returns.

    Sequentially compounds per-trade returns ordered by exit_date. This
    inflates drawdowns for multi-position strategies — see module docstring.
    """
    df = pd.read_csv(path)
    cols = list(df.columns)

    # Normalise raw bot column names if needed.
    rename = {}
    if "return_pct" in cols and "return" not in cols:
        rename["return_pct"] = "return"
    if "entry_ts" in cols and "entry_date" not in cols:
        rename["entry_ts"] = "entry_date"
    if "exit_ts" in cols and "exit_date" not in cols:
        rename["exit_ts"] = "exit_date"
    if rename:
        df = df.rename(columns=rename)

    required = {"return", "entry_date", "exit_date"}
    if not required.issubset(df.columns):
        raise ValueError(
            f"Trades CSV missing required columns. Need {required}, "
            f"found {list(df.columns)}."
        )

    df["entry_date"] = pd.to_datetime(df["entry_date"])
    df["exit_date"] = pd.to_datetime(df["exit_date"])
    df = df.sort_values("exit_date")

    eq = (1.0 + df["return"].astype(float)).cumprod()
    eq.index = pd.to_datetime(df["exit_date"].values)
    eq.name = "approx_equity_per_trade_compound"

    first_entry = pd.to_datetime(df["entry_date"].min())
    if first_entry < eq.index[0]:
        baseline = pd.Series([1.0], index=[first_entry], name=eq.name)
        eq = pd.concat([baseline, eq])
    return eq


def load_input(csv_path: Path,
               contributions_path: Path | None
               ) -> tuple[pd.Series, str, bool, str | None]:
    """Detect input type and load the equity series to analyse.

    Returns:
        (equity_series_to_analyse, source_label, is_approximate, contributions_note)

    If --contributions is passed AND the input is an equity series,
    the returned series is contribution-adjusted (strategy-only equity).
    """
    df_peek = pd.read_csv(csv_path, nrows=5)
    cols = set(df_peek.columns)

    if any(c in cols for c in ("equity_gbp", "equity_usd", "equity")):
        eq = _load_equity_csv(csv_path)
        if contributions_path:
            contribs = _load_contributions_csv(contributions_path)
            cum = contribs.cumsum().reindex(eq.index, method="ffill").fillna(0)
            strategy_eq = (eq - cum).rename("strategy_only_equity")
            note = (f"subtracted cumulative contributions "
                    f"(total {float(contribs.sum()):,.2f})")
            return strategy_eq, "equity series (strategy-only)", False, note
        return eq, "equity series (incl. contributions)", False, None

    # Fall back to trades CSV.
    if contributions_path:
        print("WARNING: --contributions ignored when input is a trades CSV.",
              file=sys.stderr)
    eq = _trades_to_approx_equity(csv_path)
    return eq, "trades CSV (approximate)", True, None


# ===========================================================================
# Drawdown analysis
# ===========================================================================

def drawdown_series(equity: pd.Series) -> pd.Series:
    """Drawdown at each point: (equity / running_max) - 1, always <= 0."""
    running_max = equity.cummax()
    return equity / running_max - 1.0


def max_drawdown(equity: pd.Series) -> dict:
    """Depth, peak/trough/recovery dates, underwater duration."""
    dd = drawdown_series(equity)
    trough_date = dd.idxmin()
    depth = float(dd.min())
    peak_date = equity.loc[:trough_date].idxmax()
    peak_eq = float(equity.loc[peak_date])

    after = equity.loc[trough_date:]
    recovered = after[after >= peak_eq]
    recovery_date = recovered.index[0] if len(recovered) > 0 else None

    dur_peak_to_trough = (trough_date - peak_date).days
    dur_underwater = ((recovery_date - peak_date).days
                      if recovery_date is not None
                      else (equity.index[-1] - peak_date).days)

    return {
        "depth_pct":           depth * 100,
        "peak_date":           peak_date,
        "trough_date":         trough_date,
        "recovery_date":       recovery_date,
        "days_peak_to_trough": dur_peak_to_trough,
        "days_underwater":     dur_underwater,
        "recovered":           recovery_date is not None,
    }


def top_n_drawdowns(equity: pd.Series, n: int = 5) -> list[dict]:
    """Non-overlapping drawdown episodes, sorted by depth (deepest first).

    An episode runs from a peak through a trough back to a new high (or
    end of series). Walks the equity curve in one pass.
    """
    episodes = []
    in_dd = False
    peak_eq = float(equity.iloc[0])
    peak_date = equity.index[0]
    trough_eq = peak_eq
    trough_date = peak_date

    for date, val in equity.items():
        val = float(val)
        if not in_dd:
            if val > peak_eq:
                peak_eq = val
                peak_date = date
            elif val < peak_eq:
                in_dd = True
                trough_eq = val
                trough_date = date
        else:
            if val < trough_eq:
                trough_eq = val
                trough_date = date
            if val >= peak_eq:
                episodes.append({
                    "peak_date":           peak_date,
                    "trough_date":         trough_date,
                    "recovery_date":       date,
                    "depth_pct":           (trough_eq / peak_eq - 1) * 100,
                    "days_underwater":     (date - peak_date).days,
                    "days_peak_to_trough": (trough_date - peak_date).days,
                    "recovered":           True,
                })
                in_dd = False
                peak_eq = val
                peak_date = date

    if in_dd:
        episodes.append({
            "peak_date":           peak_date,
            "trough_date":         trough_date,
            "recovery_date":       None,
            "depth_pct":           (trough_eq / peak_eq - 1) * 100,
            "days_underwater":     (equity.index[-1] - peak_date).days,
            "days_peak_to_trough": (trough_date - peak_date).days,
            "recovered":           False,
        })

    episodes.sort(key=lambda e: e["depth_pct"])
    return episodes[:n]


def underwater_stats(equity: pd.Series) -> dict:
    """Pain index (avg DD), Ulcer index (RMS DD), % of time underwater."""
    dd = drawdown_series(equity).values
    pain_idx = float(np.mean(np.abs(dd))) * 100
    ulcer_idx = float(np.sqrt(np.mean(dd ** 2))) * 100
    pct_underwater = float((dd < -1e-9).mean()) * 100
    return {
        "pain_pct":            pain_idx,
        "ulcer_pct":           ulcer_idx,
        "pct_time_underwater": pct_underwater,
    }


# ===========================================================================
# Risk-adjusted ratios
# ===========================================================================

def cagr(equity: pd.Series) -> float:
    """Compound annual growth rate from first to last point."""
    start = float(equity.iloc[0])
    end = float(equity.iloc[-1])
    if start <= 0 or end <= 0:
        return float("nan")
    span_days = (equity.index[-1] - equity.index[0]).days
    if span_days < 1:
        return 0.0
    years = span_days / 365.25
    return (end / start) ** (1 / years) - 1


def calmar(cagr_val: float, max_dd_pct: float) -> float:
    """CAGR / |Max Drawdown|. Higher = more pain-efficient compounding."""
    if max_dd_pct >= 0:
        return float("inf")
    return cagr_val / abs(max_dd_pct / 100)


def periods_per_year_from_index(idx: pd.DatetimeIndex) -> float:
    """Infer annualisation factor from the actual sampling rate of the index."""
    if len(idx) < 2:
        return 252.0
    span_days = (idx[-1] - idx[0]).days
    if span_days < 1:
        return 252.0
    return len(idx) / (span_days / 365.25)


def sortino_from_equity(equity: pd.Series, mar_annual: float = 0.0) -> float:
    """Annualised Sortino from periodic equity returns.

    mar_annual is the annualised minimum acceptable return; converted to
    a per-period MAR using the inferred sampling rate. Default 0 means
    downside is measured relative to "no loss" rather than a benchmark.
    """
    rets = equity.pct_change().dropna()
    if len(rets) < 2:
        return 0.0
    ppy = periods_per_year_from_index(equity.index)
    mar_per_period = mar_annual / ppy
    excess = rets - mar_per_period
    downside = excess[excess < 0]
    if len(downside) == 0:
        return float("inf")
    downside_dev = math.sqrt((downside ** 2).mean())
    if downside_dev == 0:
        return 0.0
    return (excess.mean() / downside_dev) * math.sqrt(ppy)


def recovery_factor(equity: pd.Series, max_dd_pct: float) -> float:
    """Total return / |Max DD|. Higher = strategy earns back its pain faster."""
    total_return_pct = (float(equity.iloc[-1]) / float(equity.iloc[0]) - 1) * 100
    if max_dd_pct >= 0:
        return float("inf")
    return total_return_pct / abs(max_dd_pct)


# ===========================================================================
# Output
# ===========================================================================

def _line(c="="):
    print(c * 78)


def fmt_pct(x: float) -> str:
    return f"{x:+.2f}%"


def _verdict(value: float, tiers: list[tuple[float, str]],
             higher_is_better: bool = True) -> str:
    """Pick a label from tiers based on the value.

    Tiers are (threshold, label) pairs.
    - higher_is_better=True : take the HIGHEST tier whose threshold <= value.
    - higher_is_better=False: take the LOWEST tier whose threshold >= value
      (i.e. the first band the value fits inside, walking up).
    """
    sorted_tiers = sorted(tiers, key=lambda t: t[0])
    if higher_is_better:
        label = sorted_tiers[0][1]
        for thr, lab in sorted_tiers:
            if value >= thr:
                label = lab
        return label
    for thr, lab in sorted_tiers:
        if value <= thr:
            return lab
    return sorted_tiers[-1][1]


def report(equity: pd.Series, label: str, source_label: str,
           top_n: int, mar_annual: float,
           contributions_note: str | None,
           is_approximate: bool) -> None:
    _line()
    print(f"  DRAWDOWN & RISK-ADJUSTED ANALYSIS — {label}")
    _line()

    if is_approximate:
        print()
        print("  ⚠  APPROXIMATE MODE")
        print("     Equity curve was built by compounding per-trade returns.")
        print("     This misrepresents drawdowns for strategies that hold")
        print("     multiple concurrent positions sized as a fraction of")
        print("     equity. For accurate metrics, pass equity_gbp.csv instead.")
        print()

    if len(equity) < 2:
        print(f"  Only {len(equity)} equity observation(s). Need at least 2.")
        return

    total_return_pct = (float(equity.iloc[-1]) / float(equity.iloc[0]) - 1) * 100
    span_days = (equity.index[-1] - equity.index[0]).days
    span_years = max(span_days / 365.25, 0.01)
    cagr_val = cagr(equity)

    print()
    print(f"  {'Source':32s}  {source_label}")
    if contributions_note:
        print(f"  {'Contributions':32s}  {contributions_note}")
    print(f"  {'Observations':32s}  {len(equity):,}")
    print(f"  {'Date range':32s}  {equity.index[0].date()} → {equity.index[-1].date()}")
    print(f"  {'Span':32s}  {span_years:.2f} years")
    print(f"  {'Start equity':32s}  {float(equity.iloc[0]):,.2f}")
    print(f"  {'End equity':32s}  {float(equity.iloc[-1]):,.2f}")
    print(f"  {'Total return':32s}  {fmt_pct(total_return_pct)}")
    print(f"  {'CAGR':32s}  {fmt_pct(cagr_val * 100)}")
    print()

    # Max drawdown
    _line("-")
    print("  MAX DRAWDOWN")
    _line("-")
    mdd = max_drawdown(equity)
    print(f"  {'Depth':32s}  {fmt_pct(mdd['depth_pct'])}")
    print(f"  {'Peak date':32s}  {mdd['peak_date'].date()}")
    print(f"  {'Trough date':32s}  {mdd['trough_date'].date()}")
    if mdd["recovered"]:
        print(f"  {'Recovery date':32s}  {mdd['recovery_date'].date()}")
    else:
        print(f"  {'Recovery date':32s}  (never — still underwater)")
    print(f"  {'Days peak → trough':32s}  {mdd['days_peak_to_trough']:,}")
    print(f"  {'Days underwater (total)':32s}  {mdd['days_underwater']:,}")
    print()

    # Top-N drawdowns
    _line("-")
    print(f"  TOP {top_n} DRAWDOWN EPISODES")
    _line("-")
    eps = top_n_drawdowns(equity, n=top_n)
    print(f"  {'#':>3s}  {'Depth':>9s}  {'Peak':>11s}  {'Trough':>11s}  "
          f"{'Recovery':>11s}  {'Days UW':>8s}")
    print("  " + "-" * 64)
    for i, ep in enumerate(eps, 1):
        rec = ep["recovery_date"].date() if ep["recovery_date"] else "ongoing"
        print(f"  {i:>3d}  {fmt_pct(ep['depth_pct']):>9s}  "
              f"{str(ep['peak_date'].date()):>11s}  "
              f"{str(ep['trough_date'].date()):>11s}  "
              f"{str(rec):>11s}  "
              f"{ep['days_underwater']:>8d}")
    print()

    # Underwater stats
    _line("-")
    print("  UNDERWATER STATISTICS")
    _line("-")
    uw = underwater_stats(equity)
    print(f"  {'Pain index (avg DD)':32s}  {uw['pain_pct']:.2f}%")
    print(f"  {'Ulcer index (RMS DD)':32s}  {uw['ulcer_pct']:.2f}%")
    print(f"  {'% of time underwater':32s}  {uw['pct_time_underwater']:.1f}%")
    print()

    # Risk-adjusted ratios
    _line("-")
    print("  RISK-ADJUSTED RATIOS")
    _line("-")
    calmar_val = calmar(cagr_val, mdd["depth_pct"])
    sortino_val = sortino_from_equity(equity, mar_annual=mar_annual)
    rec_factor = recovery_factor(equity, mdd["depth_pct"])
    print(f"  {'Calmar (CAGR / |MaxDD|)':32s}  {calmar_val:+.3f}")
    print(f"  {'Sortino (ann., MAR=' + f'{mar_annual*100:.1f}%' + ')':32s}  {sortino_val:+.3f}")
    print(f"  {'Recovery factor':32s}  {rec_factor:+.2f}")
    print()

    # Verdicts
    _line()
    print("  VERDICT — INDIVIDUAL METRICS")
    _line()

    calmar_label = _verdict(calmar_val, [
        (-math.inf, "negative (loses money)"),
        (0.0,       "poor (< 0.3)"),
        (0.3,       "acceptable (0.3-0.5)"),
        (0.5,       "respectable (0.5-1.0)"),
        (1.0,       "strong (1.0-3.0)"),
        (3.0,       "suspicious — likely curve-fit (>3.0)"),
    ])
    print(f"  {'Calmar':14s}  {calmar_val:+.2f}   →  {calmar_label}")

    sortino_label = _verdict(sortino_val, [
        (-math.inf, "negative"),
        (0.0,       "weak (< 1.0)"),
        (1.0,       "reasonable (1.0-2.0)"),
        (2.0,       "strong (2.0-3.0)"),
        (3.0,       "exceptional / verify (>3.0)"),
    ])
    print(f"  {'Sortino':14s}  {sortino_val:+.2f}   →  {sortino_label}")

    mdd_abs = abs(mdd["depth_pct"])
    mdd_label = _verdict(mdd_abs, [
        (math.inf, "catastrophic (>50%)"),
        (50.0,     "severe (35-50%)"),
        (35.0,     "rough (20-35%)"),
        (20.0,     "tolerable (10-20%)"),
        (10.0,     "smooth (<10%)"),
    ], higher_is_better=False)
    print(f"  {'Max drawdown':14s}  {mdd['depth_pct']:+.1f}%  →  {mdd_label}")

    uw_label = _verdict(uw["pct_time_underwater"], [
        (math.inf, "constant pain (>70%)"),
        (70.0,     "bumpy (50-70%)"),
        (50.0,     "typical (30-50%)"),
        (30.0,     "smooth (<30%)"),
    ], higher_is_better=False)
    print(f"  {'Time UW':14s}  {uw['pct_time_underwater']:.0f}%    →  {uw_label}")

    rec_label = _verdict(rec_factor, [
        (-math.inf, "negative"),
        (0.0,       "weak (<1)"),
        (1.0,       "marginal (1-3)"),
        (3.0,       "solid (3-10)"),
        (10.0,      "very efficient / verify (>10)"),
    ])
    print(f"  {'Recovery':14s}  {rec_factor:+.2f}   →  {rec_label}")
    print()

    # Combined decision
    _line()
    print("  COMBINED DECISION")
    _line()
    if cagr_val <= 0:
        decision = ("STRATEGY DOES NOT COMPOUND. Drawdown analysis is moot; "
                    "the equity curve goes nowhere. Don't deploy.")
    elif mdd_abs > 50:
        decision = ("MAX DD > 50%. Even if Sharpe/Calmar look acceptable, "
                    "this is the kind of drawdown that breaks discipline "
                    "in live trading. Don't deploy real capital without "
                    "either reducing exposure or layering in a regime filter.")
    elif calmar_val < 0.3:
        decision = ("CALMAR < 0.3. Returns not compensating for pain. "
                    "S&P buy-and-hold typically clears this. Don't deploy.")
    elif calmar_val < 0.5:
        decision = ("MARGINAL. Calmar in the buy-and-hold range. Paper-trade "
                    "and watch whether haircut Sharpe and Calmar persist.")
    elif calmar_val < 1.0:
        decision = ("DEPLOYABLE at small size. Calmar > 0.5 with a tolerable "
                    "drawdown profile. Watch live for the first 3 months "
                    "and tighten if forward DD exceeds backtest max DD.")
    elif calmar_val < 3.0:
        decision = ("STRONG. Calmar > 1.0. Deployable. Cross-check that the "
                    "Sharpe haircut from 25_sharpe_analysis.py also clears "
                    "1.0 — if it does, you have two independent confirmations.")
    else:
        decision = ("SUSPICIOUSLY HIGH Calmar. Re-audit for data leakage, "
                    "survivorship bias, or sample-size artefacts before "
                    "believing it. Real strategies rarely clear Calmar 3.")

    print(f"  {decision}")
    print()


# ===========================================================================
# Main
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv", type=str,
                        help="Path to equity_gbp.csv (preferred) or trades CSV "
                             "(approximate fallback)")
    parser.add_argument("--contributions", type=str, default=None,
                        help="Optional contributions_gbp.csv — when provided, "
                             "cumulative contributions are subtracted from "
                             "equity to isolate strategy P&L. "
                             "Equity input only; ignored with trades CSV.")
    parser.add_argument("--label", type=str, default=None,
                        help="Display label (defaults to filename stem)")
    parser.add_argument("--top-n", type=int, default=5,
                        help="Number of top drawdown episodes to list (default 5)")
    parser.add_argument("--mar", type=float, default=0.0,
                        help="Minimum acceptable return for Sortino, expressed "
                             "as an ANNUALISED rate (e.g. 0.04 = 4%%/yr). "
                             "Default 0.")
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"ERROR: {csv_path} not found.")
        sys.exit(1)

    contributions_path = None
    if args.contributions:
        contributions_path = Path(args.contributions)
        if not contributions_path.exists():
            print(f"ERROR: {contributions_path} not found.")
            sys.exit(1)

    try:
        equity, source_label, is_approx, contributions_note = load_input(
            csv_path, contributions_path)
    except ValueError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    label = args.label or csv_path.stem
    report(equity, label=label, source_label=source_label,
           top_n=args.top_n, mar_annual=args.mar,
           contributions_note=contributions_note,
           is_approximate=is_approx)


if __name__ == "__main__":
    main()
