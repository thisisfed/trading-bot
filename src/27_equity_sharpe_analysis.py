"""
27_equity_sharpe_analysis.py

Sharpe analysis at the EQUITY CURVE level, not the per-trade level.
Companion to 25_sharpe_analysis.py for strategies where the edge lives
in position sizing, R:R asymmetry, concurrency, or compounding rather
than in per-trade signal quality.

== What it answers ==
25_sharpe_analysis.py : "does my signal have edge in %% returns per trade?"
27_equity_sharpe_analysis.py: "does my full strategy (signal + sizing + R:R
                              + concurrency + compounding) produce edge in
                              equity returns over time?"

For a strategy like ICS where per-trade Sharpe rounds to zero but equity
grew 300%%+, this is the metric that matters. The gap between the two
Sharpes is itself diagnostic: a large gap means the edge is implementation,
not signal.

== Inputs ==
  - equity_gbp.csv (preferred — full portfolio equity series)
  - Optional --contributions to subtract cumulative deposits and isolate
    strategy P&L
  - --tests for Bonferroni haircut against parameter grid size (e.g. 72
    for the default ICS WFO grid)
  - Optional --resample {D,W,M,Q} to compute returns at a lower frequency.
    Daily returns are typically autocorrelated, which inflates the
    significance test. Monthly or quarterly gives a more conservative
    haircut Sharpe at the cost of fewer observations.

== Methodology notes ==
The t-statistic assumes IID periodic returns. Real equity returns have
serial correlation, especially at daily frequency. The Lo (2002)
autocorrelation adjustment is the standard fix; not applied here. This
means the raw daily-frequency haircut Sharpe is slightly OPTIMISTIC.
For a more honest read, resample to monthly:

    python src/27_equity_sharpe_analysis.py ... --resample M --tests 72

== Output ==
Mirrors 25_sharpe_analysis.py:
  - Observation count, span, periods per year
  - Per-period and annualised Sharpe (raw)
  - Bonferroni-haircut Sharpe with t-stat and p-value
  - Per-year breakdown
  - Concentration test (top quarter / top 3 quarters)
  - Verdict against academic thresholds
  - Combined decision plus comparison hint vs per-trade Sharpe

== Usage ==
    python src/27_equity_sharpe_analysis.py data/reports/v3_wfo/equity_gbp.csv --tests 72
    python src/27_equity_sharpe_analysis.py data/reports/v3_wfo/equity_gbp.csv \\
        --contributions data/reports/v3_wfo/contributions_gbp.csv \\
        --tests 72 --label "v3 WFO equity (strategy only)"
    python src/27_equity_sharpe_analysis.py data/reports/v3_wfo/equity_gbp.csv \\
        --tests 72 --resample M --label "v3 WFO (monthly, conservative)"
"""

import argparse
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd


# ===========================================================================
# Normal distribution helpers (stdlib only — same as 25_sharpe_analysis.py)
# ===========================================================================

def _norm_cdf(x: float) -> float:
    """Standard normal CDF using math.erf (stdlib)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """Inverse standard normal CDF (probit). Wichura 1988 rational
    approximation, accurate to ~1e-9 over (0, 1)."""
    if p <= 0.0:
        return -math.inf
    if p >= 1.0:
        return math.inf

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
# Sharpe ↔ p-value conversions (same machinery as 25, applied to equity returns)
# ===========================================================================

def per_period_sharpe_to_tstat(sharpe: float, n_obs: int) -> float:
    """t = Sharpe × √N. Assumes IID periodic returns — caveat in docstring."""
    if n_obs < 2:
        return 0.0
    return sharpe * math.sqrt(n_obs)


def tstat_to_pvalue(t: float) -> float:
    return 2.0 * (1.0 - _norm_cdf(abs(t)))


def pvalue_to_per_period_sharpe(p_value: float, n_obs: int, sign: int = 1) -> float:
    if p_value >= 1.0 or n_obs < 2:
        return 0.0
    if p_value <= 0.0:
        return float("inf") * sign
    t = _norm_ppf(1 - p_value / 2)
    return sign * t / math.sqrt(n_obs)


def bonferroni_haircut(raw_per_period_sharpe: float, n_obs: int,
                       n_tests: int) -> tuple[float, float, float, float]:
    """Apply Bonferroni multiple-testing adjustment to a per-period Sharpe.

    Returns: (raw_t, raw_p, adj_p, adj_per_period_sharpe).
    """
    raw_t = per_period_sharpe_to_tstat(raw_per_period_sharpe, n_obs)
    raw_p = tstat_to_pvalue(raw_t)
    adj_p = min(raw_p * n_tests, 1.0)
    sign = 1 if raw_per_period_sharpe >= 0 else -1
    adj_sharpe = pvalue_to_per_period_sharpe(adj_p, n_obs, sign=sign)
    return raw_t, raw_p, adj_p, adj_sharpe


# ===========================================================================
# Input loading — mirrors 26_drawdown_analysis.py v2
# ===========================================================================

def _load_equity_csv(path: Path) -> pd.Series:
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    if df.shape[1] < 1:
        raise ValueError(f"{path} has no value column.")
    eq = df.iloc[:, 0].astype(float).sort_index()
    eq.name = df.columns[0]
    return eq


def _load_contributions_csv(path: Path) -> pd.Series:
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    return df.iloc[:, 0].astype(float).sort_index()


def load_equity(csv_path: Path,
                contributions_path: Path | None
                ) -> tuple[pd.Series, str | None]:
    """Load equity series, optionally contribution-adjusted.

    Returns: (equity_series, contributions_note_or_None).
    """
    eq = _load_equity_csv(csv_path)
    if contributions_path:
        contribs = _load_contributions_csv(contributions_path)
        cum = contribs.cumsum().reindex(eq.index, method="ffill").fillna(0)
        eq = (eq - cum).rename("strategy_only_equity")
        note = (f"subtracted cumulative contributions "
                f"(total {float(contribs.sum()):,.2f})")
        return eq, note
    return eq, None


def resample_equity(equity: pd.Series, freq: str) -> pd.Series:
    """Resample equity to a given frequency (W, M, Q). Uses end-of-period.

    Tries the modern pandas alias first ('ME', 'QE') then falls back to
    the legacy alias ('M', 'Q') for older pandas versions.
    """
    if freq.upper() == "D":
        return equity
    rule_modern = {"W": "W-FRI", "M": "ME", "Q": "QE"}.get(freq.upper())
    rule_legacy = {"W": "W-FRI", "M": "M",  "Q": "Q" }.get(freq.upper())
    if rule_modern is None:
        raise ValueError(f"Unknown resample frequency: {freq}. Use D, W, M, or Q.")
    try:
        return equity.resample(rule_modern).last().dropna()
    except ValueError:
        return equity.resample(rule_legacy).last().dropna()


# ===========================================================================
# Metric helpers
# ===========================================================================

def periods_per_year_from_index(idx: pd.DatetimeIndex) -> float:
    """Infer annualisation factor from the actual index spacing."""
    if len(idx) < 2:
        return 252.0
    span_days = (idx[-1] - idx[0]).days
    if span_days < 1:
        return 252.0
    return len(idx) / (span_days / 365.25)


def per_year_breakdown(returns: pd.Series, ppy: float) -> pd.DataFrame:
    """Group periodic returns by calendar year, annualise within each year."""
    df = returns.to_frame(name="ret").copy()
    df["year"] = df.index.year
    rows = []
    for yr, g in df.groupby("year"):
        rs = g["ret"].values
        if len(rs) < 2 or rs.std() == 0:
            sh = 0.0
        else:
            sh = (rs.mean() / rs.std()) * math.sqrt(ppy)
        compounded = float((1 + g["ret"]).prod() - 1)
        rows.append({
            "year":         int(yr),
            "n_obs":        len(g),
            "year_return":  compounded * 100,
            "pos_periods":  float((rs > 0).mean() * 100),
            "ann_sharpe":   sh,
        })
    return pd.DataFrame(rows)


def concentration(returns: pd.Series) -> dict:
    """Concentration of returns by calendar quarter.

    Uses LOG returns so per-quarter values are additive (a clean way to
    measure "what fraction of total log-return came from the top quarter")
    without the negative-denominator gotcha that bites the per-trade
    concentration test when total return is near zero.
    """
    log_rets = np.log1p(returns)
    df = log_rets.to_frame(name="logret").copy()
    df["quarter"] = df.index.to_period("Q")
    by_q = df.groupby("quarter")["logret"].sum().sort_values(ascending=False)
    total_log = float(by_q.sum())
    top1_log = float(by_q.iloc[0]) if len(by_q) >= 1 else 0.0
    top3_log = float(by_q.iloc[:3].sum()) if len(by_q) >= 3 else float(by_q.sum())
    return {
        "n_quarters":   len(by_q),
        "top1_pct":     (top1_log / total_log * 100) if abs(total_log) > 1e-9 else float("nan"),
        "top3_pct":     (top3_log / total_log * 100) if abs(total_log) > 1e-9 else float("nan"),
        "positive_qs":  int((by_q > 0).sum()),
        "hit_rate_q":   float((by_q > 0).mean() * 100),
        "best_q_pct":   (math.exp(top1_log) - 1) * 100,
        "worst_q_pct":  (math.exp(float(by_q.iloc[-1])) - 1) * 100 if len(by_q) > 0 else 0.0,
    }


# ===========================================================================
# Output
# ===========================================================================

def _line(c="="):
    print(c * 78)


def fmt_pct(x: float) -> str:
    return f"{x:+.2f}%"


def report(equity: pd.Series, label: str, n_tests: int | None,
           contributions_note: str | None, resample_freq: str) -> None:
    _line()
    print(f"  EQUITY-LEVEL SHARPE ANALYSIS — {label}")
    _line()

    if len(equity) < 3:
        print(f"  Only {len(equity)} equity observation(s). Need at least 3.")
        return

    returns = equity.pct_change().dropna()
    if len(returns) < 2:
        print("  Cannot compute returns from this equity series.")
        return

    # Basic stats
    mean_ret = float(returns.mean())
    std_ret = float(returns.std())
    per_period_sharpe = mean_ret / std_ret if std_ret > 0 else 0.0
    ppy = periods_per_year_from_index(equity.index)
    ann_sharpe = per_period_sharpe * math.sqrt(ppy)

    first = equity.index[0]
    last = equity.index[-1]
    span_days = (last - first).days
    span_years = max(span_days / 365.25, 0.01)
    total_return_pct = (float(equity.iloc[-1]) / float(equity.iloc[0]) - 1) * 100
    cagr = (float(equity.iloc[-1]) / float(equity.iloc[0])) ** (1 / span_years) - 1
    pos_periods_pct = float((returns > 0).mean() * 100)

    print()
    print(f"  {'Source':32s}  "
          f"equity series{' (strategy-only)' if contributions_note else ' (incl. contributions)'}")
    if contributions_note:
        print(f"  {'Contributions':32s}  {contributions_note}")
    print(f"  {'Resampled to':32s}  "
          f"{ {'D':'daily (native)','W':'weekly','M':'monthly','Q':'quarterly'}.get(resample_freq.upper(), resample_freq) }")
    print(f"  {'Observations':32s}  {len(equity):,}")
    print(f"  {'Return periods (N)':32s}  {len(returns):,}")
    print(f"  {'Periods per year':32s}  {ppy:.1f}")
    print(f"  {'Date range':32s}  {first.date()} → {last.date()}")
    print(f"  {'Span':32s}  {span_years:.2f} years")
    print(f"  {'Total return':32s}  {fmt_pct(total_return_pct)}")
    print(f"  {'CAGR':32s}  {fmt_pct(cagr * 100)}")
    print(f"  {'Mean per-period return':32s}  {fmt_pct(mean_ret * 100)}")
    print(f"  {'Std per-period return':32s}  {std_ret * 100:.3f}%")
    print(f"  {'Positive periods':32s}  {pos_periods_pct:.1f}%")
    print()

    # Sharpe ladder
    _line("-")
    print("  SHARPE LADDER")
    _line("-")
    print(f"  {'Per-period Sharpe (raw)':32s}  {per_period_sharpe:+.3f}")
    print(f"  {'Annualised Sharpe (raw)':32s}  {ann_sharpe:+.3f}")

    if n_tests is None or n_tests <= 1:
        print()
        print("  No --tests value provided. Raw Sharpe shown only.")
        print("  Re-run with --tests N to apply the Harvey-Liu Bonferroni haircut")
        print("  against the parameter grid size used in WFO (e.g. 72 for ICS).")
        haircut_ann = ann_sharpe
    else:
        raw_t, raw_p, adj_p, adj_pp = bonferroni_haircut(
            per_period_sharpe, len(returns), n_tests)
        haircut_ann = adj_pp * math.sqrt(ppy)
        haircut_pct = ((ann_sharpe - haircut_ann) / ann_sharpe * 100
                       if ann_sharpe != 0 else 0)
        print()
        print(f"  {'Raw t-statistic':32s}  {raw_t:+.3f}")
        print(f"  {'Raw p-value (single test)':32s}  {raw_p:.4g}")
        print(f"  {'N parameter tests':32s}  {n_tests}")
        print(f"  {'Bonferroni-adjusted p':32s}  {adj_p:.4g}")
        print(f"  {'Haircut Sharpe (annualised)':32s}  {haircut_ann:+.3f}")
        print(f"  {'Haircut %':32s}  {haircut_pct:.0f}%")
        print()
        print(f"  Interpretation: after correction for the {n_tests}-combo WFO")
        print(f"  parameter search, the equity-level Sharpe is estimated at")
        print(f"  {haircut_ann:+.2f} annualised.")
        if resample_freq.upper() == "D":
            print()
            print("  Caveat: daily returns have positive autocorrelation, which")
            print("  inflates this t-statistic. Re-run with --resample M for a")
            print("  more conservative read (probably 10-30% lower).")

    print()

    # Per-year breakdown
    _line("-")
    print("  PER-YEAR BREAKDOWN")
    _line("-")
    py = per_year_breakdown(returns, ppy)
    print(f"  {'Year':>6s}  {'N obs':>6s}  {'Year ret':>10s}  "
          f"{'Pos %':>8s}  {'Ann. Sharpe':>13s}")
    print("  " + "-" * 56)
    for _, row in py.iterrows():
        print(f"  {int(row['year']):>6d}  {int(row['n_obs']):>6d}  "
              f"{fmt_pct(row['year_return']):>10s}  "
              f"{row['pos_periods']:>7.1f}%  "
              f"{row['ann_sharpe']:>+13.3f}")
    print()

    # Concentration test (uses log returns — additive without sign gotchas)
    _line("-")
    print("  CONCENTRATION TEST")
    _line("-")
    conc = concentration(returns)
    print(f"  {'Quarters covered':32s}  {conc['n_quarters']}")
    print(f"  {'Positive quarters':32s}  "
          f"{conc['positive_qs']} ({conc['hit_rate_q']:.0f}%)")
    print(f"  {'Best quarter':32s}  {fmt_pct(conc['best_q_pct'])}")
    print(f"  {'Worst quarter':32s}  {fmt_pct(conc['worst_q_pct'])}")
    print(f"  {'Top 1 Q contribution (log)':32s}  "
          f"{conc['top1_pct']:.0f}% of total log-return")
    print(f"  {'Top 3 Q contribution (log)':32s}  "
          f"{conc['top3_pct']:.0f}% of total log-return")
    if not math.isnan(conc["top1_pct"]) and conc["top1_pct"] > 50:
        print("  ⚠  Single quarter > 50% of return — strategy is concentrated")
    elif not math.isnan(conc["top3_pct"]) and conc["top3_pct"] > 80:
        print("  ⚠  Top 3 Qs > 80% of return — most of equity growth in a few windows")
    print()

    # Verdict
    _line()
    print("  VERDICT vs ACADEMIC THRESHOLDS")
    _line()
    target = haircut_ann
    label_used = ("haircut annualised Sharpe"
                  if (n_tests is not None and n_tests > 1)
                  else "raw annualised Sharpe (no haircut)")
    print(f"  Comparison metric: {label_used} = {target:+.3f}")
    print()
    print(f"  {'Tier':30s}  {'Threshold':>12s}  {'Status':>10s}")
    print("  " + "-" * 56)
    tiers = [
        ("S&P 500 baseline",    0.5),
        ("Retail real edge",    1.0),
        ("Institutional-grade", 2.0),
    ]
    for tname, thr in tiers:
        marker = "✓ PASS" if target >= thr else "✗ fail"
        print(f"  {tname:30s}  {thr:>+12.2f}  {marker:>10s}")
    print()

    # Decision
    if target < 0:
        decision = ("EQUITY GOES DOWN ON RISK-ADJUSTED BASIS. Don't deploy.")
    elif target < 0.3:
        decision = ("EQUITY EDGE INDISTINGUISHABLE FROM NOISE. Don't deploy.")
    elif target < 0.5:
        decision = ("UNDERPERFORMS PASSIVE INDEX on risk-adjusted basis. "
                    "Don't deploy real capital.")
    elif target < 1.0:
        decision = ("MARGINAL equity-level edge. Paper-trade for 6+ months "
                    "in live conditions to see if it persists.")
    elif target < 2.0:
        decision = ("LEGITIMATE EQUITY-LEVEL EDGE. Worth deploying at small "
                    "size and monitoring live performance against backtest.")
    else:
        decision = ("EXCEPTIONALLY HIGH equity Sharpe. Re-audit for residual "
                    "bias (regime overfitting, contribution leak, lookahead) "
                    "before believing it.")
    print(f"  Decision: {decision}")
    print()

    # Diagnostic comparison hint
    _line()
    print("  CROSS-CHECK vs PER-TRADE (25_sharpe_analysis.py)")
    _line()
    print("  If per-trade haircut Sharpe << equity-level haircut Sharpe:")
    print("    The edge lives in IMPLEMENTATION (sizing, R:R, concurrency,")
    print("    compounding) — not in signal selection. The strategy depends")
    print("    on the wrapper holding up in live conditions. More fragile")
    print("    than headline equity Sharpe suggests; paper-trade carefully.")
    print()
    print("  If per-trade haircut Sharpe ≈ equity-level haircut Sharpe:")
    print("    Both layers point the same way. Edge is in signal AND")
    print("    implementation. Stronger case for deployment.")
    print()


# ===========================================================================
# Main
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv", type=str,
                        help="Path to equity_gbp.csv (single-column equity "
                             "series with date index)")
    parser.add_argument("--contributions", type=str, default=None,
                        help="Optional contributions_gbp.csv — when provided, "
                             "cumulative contributions are subtracted from "
                             "equity to isolate strategy P&L.")
    parser.add_argument("--tests", type=int, default=None,
                        help="Number of parameter combinations tested in WFO. "
                             "Default ICS grid is 72. Omit to skip Bonferroni.")
    parser.add_argument("--resample", type=str, default="D",
                        help="Frequency to compute returns at: D (native, "
                             "default), W (weekly), M (monthly), Q (quarterly). "
                             "Monthly/quarterly is more conservative for the "
                             "significance test on autocorrelated equity.")
    parser.add_argument("--label", type=str, default=None,
                        help="Display label (defaults to filename stem)")
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
        equity, contributions_note = load_equity(csv_path, contributions_path)
        equity = resample_equity(equity, args.resample)
    except ValueError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    label = args.label or csv_path.stem
    report(equity, label=label, n_tests=args.tests,
           contributions_note=contributions_note,
           resample_freq=args.resample)


if __name__ == "__main__":
    main()
