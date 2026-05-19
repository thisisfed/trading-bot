"""
25_sharpe_analysis.py

Honest Sharpe analysis for a backtest trade list. Takes a CSV with
'return', 'entry_date', 'exit_date' columns and reports:

  - Trade count, span in years, trades per year
  - Per-trade Sharpe (the noisy version we've been using)
  - Annualised Sharpe (= per-trade Sharpe × √trades_per_year)
  - Harvey-Liu Bonferroni-haircut Sharpe given the parameter grid size
  - Per-year breakdown to spot consistency vs single-quarter concentration
  - Verdict against the academic thresholds:
        ≥ 0.5  market-beating
        ≥ 1.0  retail-grade real edge
        ≥ 2.0  institutional-grade (almost certainly an error if you see this)

Why Bonferroni: it's the most conservative of the three methods Harvey &
Liu (2015) recommend (the others are Holm and BHY). Conservative = harder
to falsely claim edge. If a strategy passes Bonferroni at N=100 tests, it's
also passed Holm and BHY. The haircut is "if you tested N parameter combos
and picked the best, this is the Sharpe a single pre-specified test would
have produced — i.e. the Sharpe you'd actually expect to see live."

Usage:
    python src/25_sharpe_analysis.py data/wfo_filtered_oos_trades.csv --tests 108
    python src/25_sharpe_analysis.py data/hma_leaps_oos_trades.csv --tests 54

If --tests is omitted, only the raw Sharpe is reported (no haircut).
"""

import argparse
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd


# ===========================================================================
# Normal distribution helpers (no scipy dependency)
# ===========================================================================

def _norm_cdf(x: float) -> float:
    """Standard normal CDF using math.erf (stdlib)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """Inverse standard normal CDF (probit). Beasley-Springer-Moro
    approximation, accurate to ~1e-9 over (0, 1)."""
    if p <= 0.0:
        return -math.inf
    if p >= 1.0:
        return math.inf

    # Coefficients for the rational approximation (Wichura 1988)
    a = [-3.969683028665376e+01, 2.209460984245205e+02,
         -2.759285104469687e+02, 1.383577518672690e+02,
         -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02,
         -1.556989798598866e+02, 6.680131188771972e+01,
         -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01,
         -2.400758277161838e+00, -2.549732539343734e+00,
          4.374664141464968e+00,  2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01,
         2.445134137142996e+00, 3.754408661907416e+00]

    plow  = 0.02425
    phigh = 1 - plow

    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p <= phigh:
        q = p - 0.5
        r = q*q
        return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
               (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
            ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)


# ===========================================================================
# Sharpe ↔ p-value conversions
# ===========================================================================

def sharpe_to_tstat(sharpe: float, n_trades: int) -> float:
    """Convert per-trade Sharpe to t-statistic. t = Sharpe × √N."""
    if n_trades < 2:
        return 0.0
    return sharpe * math.sqrt(n_trades)


def tstat_to_pvalue(t: float) -> float:
    """Two-sided p-value from a t-stat, assuming large-N normal approx."""
    return 2.0 * (1.0 - _norm_cdf(abs(t)))


def pvalue_to_sharpe(p_value: float, n_trades: int, sign: int = 1) -> float:
    """Inverse of sharpe → p_value. p_value is two-sided."""
    if p_value >= 1.0 or n_trades < 2:
        return 0.0
    if p_value <= 0.0:
        return float("inf") * sign
    t = _norm_ppf(1 - p_value / 2)
    return sign * t / math.sqrt(n_trades)


def bonferroni_haircut(raw_sharpe: float, n_trades: int,
                       n_tests: int) -> tuple[float, float, float, float]:
    """Apply Bonferroni multiple-testing adjustment.

    Returns: (raw_t, raw_p, adj_p, adj_sharpe).

    Per Harvey & Liu (2015): adjusted_p = min(N_tests × raw_p, 1).
    Then convert back to Sharpe via the inverse t→Sharpe formula.
    """
    raw_t = sharpe_to_tstat(raw_sharpe, n_trades)
    raw_p = tstat_to_pvalue(raw_t)
    adj_p = min(raw_p * n_tests, 1.0)
    sign = 1 if raw_sharpe >= 0 else -1
    adj_sharpe = pvalue_to_sharpe(adj_p, n_trades, sign=sign)
    return raw_t, raw_p, adj_p, adj_sharpe


# ===========================================================================
# Metric helpers
# ===========================================================================

def per_year_breakdown(trades: pd.DataFrame) -> pd.DataFrame:
    """Group trades by exit_date year and report counts + per-trade Sharpe."""
    df = trades.copy()
    df["year"] = df["exit_date"].dt.year
    rows = []
    for yr, g in df.groupby("year"):
        rs = g["return"].values
        sh = (rs.mean() / rs.std()) if (len(rs) > 1 and rs.std() > 0) else 0.0
        rows.append({
            "year":      int(yr),
            "n_trades":  len(g),
            "mean_ret":  rs.mean() * 100,
            "win_rate":  (rs > 0).mean() * 100,
            "per_trade_sharpe":  sh,
        })
    return pd.DataFrame(rows)


def concentration(trades: pd.DataFrame) -> dict:
    """How much of total return came from the top 1 / top 3 quarters?"""
    df = trades.copy()
    df["quarter"] = df["exit_date"].dt.to_period("Q")
    by_q = df.groupby("quarter")["return"].sum().sort_values(ascending=False)
    total = by_q.sum()
    top1 = float(by_q.iloc[0]) if len(by_q) >= 1 else 0.0
    top3 = float(by_q.iloc[:3].sum()) if len(by_q) >= 3 else float(by_q.sum())
    return {
        "n_quarters":   len(by_q),
        "top1_pct":     (top1 / total * 100) if total != 0 else float("nan"),
        "top3_pct":     (top3 / total * 100) if total != 0 else float("nan"),
        "positive_qs":  int((by_q > 0).sum()),
        "hit_rate_q":   float((by_q > 0).mean() * 100),
    }


# ===========================================================================
# Output
# ===========================================================================

def _line(c="="):
    print(c * 78)


def fmt_pct(x: float) -> str:
    return f"{x:+.2f}%"


def report(trades: pd.DataFrame, n_tests: int | None, label: str) -> None:
    _line()
    print(f"  SHARPE ANALYSIS — {label}")
    _line()

    if len(trades) < 2:
        print(f"  Only {len(trades)} trade(s). Need at least 2 to compute Sharpe.")
        return

    # Basic stats
    rs = trades["return"].values
    mean_ret = rs.mean()
    std_ret  = rs.std()
    per_trade_sharpe = mean_ret / std_ret if std_ret > 0 else 0.0
    win_rate = (rs > 0).mean() * 100

    # Time span
    first = trades["entry_date"].min()
    last  = trades["exit_date"].max()
    span_days = (last - first).days
    span_years = max(span_days / 365.25, 0.01)
    trades_per_year = len(trades) / span_years

    # Annualised Sharpe = per-trade × √(trades/year)
    annualised_sharpe = per_trade_sharpe * math.sqrt(trades_per_year)

    print()
    print(f"  {'Trades':32s}  {len(trades):,}")
    print(f"  {'Date range':32s}  {first.date()} → {last.date()}")
    print(f"  {'Span':32s}  {span_years:.2f} years")
    print(f"  {'Trades per year':32s}  {trades_per_year:.1f}")
    print(f"  {'Mean trade return':32s}  {fmt_pct(mean_ret * 100)}")
    print(f"  {'Std trade return':32s}  {std_ret * 100:.2f}%")
    print(f"  {'Win rate':32s}  {win_rate:.1f}%")
    print()

    # Sharpe ladder
    _line("-")
    print("  SHARPE LADDER")
    _line("-")
    print(f"  {'Per-trade Sharpe (raw)':32s}  {per_trade_sharpe:+.3f}")
    print(f"  {'Annualised Sharpe (raw)':32s}  {annualised_sharpe:+.3f}")

    if n_tests is None or n_tests <= 1:
        print()
        print(f"  No --tests value provided. Raw Sharpe shown only.")
        print(f"  If you ran WFO with N parameter combos, re-run with --tests N")
        print(f"  to apply the Harvey-Liu multiple-testing haircut.")
    else:
        raw_t, raw_p, adj_p, adj_sharpe_per_trade = bonferroni_haircut(
            per_trade_sharpe, len(trades), n_tests)
        adj_annualised = adj_sharpe_per_trade * math.sqrt(trades_per_year)
        haircut_pct = ((annualised_sharpe - adj_annualised) /
                       annualised_sharpe * 100) if annualised_sharpe != 0 else 0
        print()
        print(f"  {'Raw t-statistic':32s}  {raw_t:+.3f}")
        print(f"  {'Raw p-value (single test)':32s}  {raw_p:.4f}")
        print(f"  {'N parameter tests':32s}  {n_tests}")
        print(f"  {'Bonferroni-adjusted p':32s}  {adj_p:.4f}")
        print(f"  {'Haircut Sharpe (annualised)':32s}  {adj_annualised:+.3f}")
        print(f"  {'Haircut %':32s}  {haircut_pct:.0f}%")
        print()
        print(f"  Interpretation: after honest correction for the {n_tests}-combo")
        print(f"  parameter search, this strategy's true Sharpe is estimated at")
        print(f"  {adj_annualised:+.2f} annualised.")

    print()

    # Concentration test
    _line("-")
    print("  CONCENTRATION TEST")
    _line("-")
    conc = concentration(trades)
    print(f"  {'Quarters traded':32s}  {conc['n_quarters']}")
    print(f"  {'Positive quarters':32s}  {conc['positive_qs']} ({conc['hit_rate_q']:.0f}%)")
    print(f"  {'Top 1 Q contribution':32s}  {conc['top1_pct']:.0f}% of total return")
    print(f"  {'Top 3 Q contribution':32s}  {conc['top3_pct']:.0f}% of total return")
    if conc["top1_pct"] > 80:
        print(f"  ⚠  Single quarter > 80% of return — strategy is fragile")
    elif conc["top3_pct"] > 100:
        print(f"  ⚠  Top 3 Qs > 100% of total — rest are losing")
    print()

    # Per-year breakdown
    _line("-")
    print("  PER-YEAR BREAKDOWN")
    _line("-")
    py = per_year_breakdown(trades)
    print(f"  {'Year':>6s}  {'Trades':>8s}  {'Mean ret':>10s}  "
          f"{'Win%':>8s}  {'Sharpe/trade':>14s}")
    print("  " + "-" * 56)
    for _, row in py.iterrows():
        print(f"  {int(row['year']):>6d}  {int(row['n_trades']):>8d}  "
              f"{fmt_pct(row['mean_ret']):>10s}  "
              f"{row['win_rate']:>7.1f}%  "
              f"{row['per_trade_sharpe']:>+14.3f}")
    print()

    # Verdict
    _line()
    print("  VERDICT vs ACADEMIC THRESHOLDS")
    _line()
    target = (adj_annualised if (n_tests is not None and n_tests > 1)
              else annualised_sharpe)
    label_used = ("haircut annualised Sharpe"
                  if (n_tests is not None and n_tests > 1)
                  else "raw annualised Sharpe (no haircut)")
    print(f"  Comparison metric: {label_used} = {target:+.3f}")
    print()
    print(f"  {'Tier':30s}  {'Threshold':>12s}  {'Status':>10s}")
    print("  " + "-" * 56)
    tiers = [
        ("S&P 500 baseline",       0.5,  "beat market"),
        ("Retail real edge",       1.0,  "deployable"),
        ("Institutional-grade",    2.0,  "exceptional"),
    ]
    for tname, thr, _ in tiers:
        passes = target >= thr
        marker = "✓ PASS" if passes else "✗ fail"
        print(f"  {tname:30s}  {thr:>+12.2f}  {marker:>10s}")
    print()

    # Decision
    if target < 0:
        decision = ("STRATEGY LOSES MONEY. Don't deploy. "
                    "Investigate why and either fix structurally or drop.")
    elif target < 0.3:
        decision = ("EDGE INDISTINGUISHABLE FROM NOISE. Don't deploy. "
                    "More data won't help; the signal isn't there.")
    elif target < 0.5:
        decision = ("UNDERPERFORMS PASSIVE INDEX. Don't deploy real capital. "
                    "SPY would beat this with less work.")
    elif target < 1.0:
        decision = ("MARGINAL. Paper-trade for 6+ months to see if it persists. "
                    "Sample size and regime stability are the open questions.")
    elif target < 2.0:
        decision = ("LEGITIMATE EDGE. Worth deploying at small size and "
                    "monitoring live performance against the backtest.")
    else:
        decision = ("EXCEPTIONALLY HIGH. Almost certainly an artifact. "
                    "Re-audit for data leakage, look-ahead bias, or sample-size "
                    "issues before believing it.")
    print(f"  Decision: {decision}")
    print()


# ===========================================================================
# Main
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv", type=str, help="Path to trade CSV "
                        "(must have 'return', 'entry_date', 'exit_date' cols)")
    parser.add_argument("--tests", type=int, default=None,
                        help="Number of parameter combinations tested in WFO. "
                             "Common values: 54 (HMA), 108 (catalyst), "
                             "1 (a single pre-specified test). "
                             "Omit to skip Bonferroni haircut.")
    parser.add_argument("--label", type=str, default=None,
                        help="Display label for the report (defaults to filename)")
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"ERROR: {csv_path} not found.")
        sys.exit(1)

    trades = pd.read_csv(csv_path, parse_dates=["entry_date", "exit_date"])
    if "return" not in trades.columns:
        print(f"ERROR: CSV missing 'return' column. Cols: {list(trades.columns)}")
        sys.exit(1)

    label = args.label or csv_path.stem
    report(trades, n_tests=args.tests, label=label)


if __name__ == "__main__":
    main()
