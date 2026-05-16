"""
compare_variants.py
-------------------
Run the walk-forward optimiser TWICE — once with a feature flag off
(baseline) and once with it on (variant) — and report the per-window
out-of-sample improvement using the WFO-pass criterion:

    A variant "passes" if, across the OOS windows, BOTH
      - OOS Sharpe improves vs baseline in ≥ N_pass windows, AND
      - OOS Calmar improves vs baseline in ≥ N_pass windows.

    Default N_pass = 5 of 8 windows (62.5% — better than coin flip).

Designed for testing additive features:
    --feature vol_targeting       — toggles RiskParams.vol_targeting_enabled
    --feature mean_reversion      — toggles SignalParams.mean_reversion_enabled

Outputs
-------
data/reports/compare_<feature>/
    baseline_wfo_summary.csv       — per-window OOS metrics, baseline
    variant_wfo_summary.csv        — per-window OOS metrics, variant
    per_window_diff.csv            — joined table with deltas
    verdict.txt                    — human-readable summary + verdict

The harness re-uses `wfo.run_wfo`, so contributions, point-in-time
universe selection, and all other strategy invariants are identical
between baseline and variant.  Only the toggled flag differs.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Optional, Tuple

import pandas as pd

from . import config
from .wfo import run_wfo
from .logging_utils import get_logger

log = get_logger("ics.compare_variants")


# ---------------------------------------------------------------------------
# Feature registry — maps a CLI-friendly name to the dataclass + field to flip
# ---------------------------------------------------------------------------
FEATURES = {
    "vol_targeting": {
        "field": "vol_targeting_enabled",
        "config_attr": "RISK_PARAMS",
        "description": "Volatility targeting on position sizing",
    },
    "mean_reversion": {
        "field": "mean_reversion_enabled",
        "config_attr": "SIGNAL_PARAMS",
        "description": "Mean-reversion sleeve (RSI-2 oversold-bounce)",
    },
}


# ---------------------------------------------------------------------------
# Verdict logic
# ---------------------------------------------------------------------------
def _per_window_diff(
    baseline_summary: pd.DataFrame,
    variant_summary: pd.DataFrame,
) -> pd.DataFrame:
    """Join baseline and variant per-window summaries by Window column."""
    if baseline_summary.empty or variant_summary.empty:
        return pd.DataFrame()
    keep_cols = ["Window", "OOS_CAGR_%", "OOS_MaxDD_%", "OOS_Sharpe", "OOS_PF", "OOS_Trades"]
    b = baseline_summary[keep_cols].add_prefix("base_").rename(columns={"base_Window": "Window"})
    v = variant_summary[keep_cols].add_prefix("var_").rename(columns={"var_Window": "Window"})
    out = b.merge(v, on="Window", how="outer")

    # Calmar isn't directly in the per-window summary but we can derive it:
    #   Calmar = CAGR / MaxDD  (avoid div-zero)
    def _calmar(cagr_pct, mdd_pct):
        if pd.isna(cagr_pct) or pd.isna(mdd_pct) or mdd_pct == 0:
            return float("nan")
        return cagr_pct / mdd_pct

    out["base_Calmar"] = out.apply(
        lambda r: _calmar(r["base_OOS_CAGR_%"], r["base_OOS_MaxDD_%"]), axis=1)
    out["var_Calmar"]  = out.apply(
        lambda r: _calmar(r["var_OOS_CAGR_%"], r["var_OOS_MaxDD_%"]), axis=1)

    out["delta_Sharpe"] = out["var_OOS_Sharpe"] - out["base_OOS_Sharpe"]
    out["delta_Calmar"] = out["var_Calmar"]    - out["base_Calmar"]
    out["delta_CAGR_%"] = out["var_OOS_CAGR_%"] - out["base_OOS_CAGR_%"]
    out["delta_MaxDD_%"] = out["var_OOS_MaxDD_%"] - out["base_OOS_MaxDD_%"]
    return out


def _verdict(diff: pd.DataFrame, n_pass: int) -> Tuple[bool, str]:
    """Apply the pass criterion.  Returns (pass, multi-line text)."""
    n_windows = len(diff)
    if n_windows == 0:
        return False, "No windows to compare — verdict UNDEFINED."

    sharpe_better = (diff["delta_Sharpe"] > 0).sum()
    calmar_better = (diff["delta_Calmar"] > 0).sum()

    sharpe_pass = bool(sharpe_better >= n_pass)
    calmar_pass = bool(calmar_better >= n_pass)
    overall = bool(sharpe_pass and calmar_pass)

    avg_dsharpe = diff["delta_Sharpe"].mean()
    avg_dcalmar = diff["delta_Calmar"].mean()
    med_dsharpe = diff["delta_Sharpe"].median()
    med_dcalmar = diff["delta_Calmar"].median()

    lines = [
        f"Windows: {n_windows}",
        f"Pass threshold: {n_pass} / {n_windows}",
        "",
        f"  Sharpe improvement in {sharpe_better} / {n_windows} windows  "
        f"{'PASS' if sharpe_pass else 'FAIL'}",
        f"    avg delta_Sharpe = {avg_dsharpe:+.3f}, median = {med_dsharpe:+.3f}",
        f"  Calmar improvement in {calmar_better} / {n_windows} windows  "
        f"{'PASS' if calmar_pass else 'FAIL'}",
        f"    avg delta_Calmar = {avg_dcalmar:+.3f}, median = {med_dcalmar:+.3f}",
        "",
    ]
    if overall:
        lines.append("VERDICT: ✅ MERGE — variant improves both Sharpe and Calmar in ≥N windows.")
    else:
        lines.append("VERDICT: ❌ DO NOT MERGE — pass criterion not met.")
    return overall, "\n".join(lines)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def compare(
    feature: str,
    start: str = "2019-01-01",
    end: Optional[str] = None,
    is_days: int = 504,
    oos_days: int = 252,
    step_days: int = 252,
    objective: str = "sharpe",
    universe: str = "nasdaq100",
    universe_cap: int = 105,
    n_pass: int = 5,
    out_dir: Optional[Path] = None,
) -> dict:
    """
    Run baseline (feature OFF) and variant (feature ON) WFOs back-to-back,
    join their per-window OOS metrics, and emit the verdict.

    Returns a dict with `passed` (bool), `diff_df`, `verdict_text`, `out_dir`.
    """
    if feature not in FEATURES:
        raise ValueError(
            f"Unknown feature {feature!r}.  Known: {list(FEATURES)}"
        )
    spec = FEATURES[feature]

    # Each run uses a unique sub-name so the WFO reporter doesn't clobber
    # one with the other.
    base_run_name = f"compare_{feature}_baseline"
    var_run_name  = f"compare_{feature}_variant"

    # The WFO uses config.SIGNAL_PARAMS / config.RISK_PARAMS as the starting
    # point for grid combos.  We need the BASELINE run to start with the
    # feature OFF and the VARIANT run with it ON.  Since we can't safely
    # mutate global config (other modules may read it concurrently), we
    # achieve this by passing a custom param_grid that pins the field to
    # one value across all windows.
    grid_off = {spec["field"]: [False]}
    grid_on  = {spec["field"]: [True]}

    log.info("=== compare_variants: %s ===", feature)
    log.info("  baseline (%s = False) ...", spec["field"])
    base_out = run_wfo(
        start=start, end=end, objective=objective, name=base_run_name,
        is_days=is_days, oos_days=oos_days, step_days=step_days,
        universe=universe, universe_cap=universe_cap,
        param_grid=grid_off,
    )
    log.info("  variant  (%s = True)  ...", spec["field"])
    var_out = run_wfo(
        start=start, end=end, objective=objective, name=var_run_name,
        is_days=is_days, oos_days=oos_days, step_days=step_days,
        universe=universe, universe_cap=universe_cap,
        param_grid=grid_on,
    )

    base_summary = base_out["summary"].get("windows")
    var_summary  = var_out["summary"].get("windows")
    if base_summary is None or var_summary is None:
        raise RuntimeError("WFO did not return per-window summary — cannot compare.")

    base_df = pd.DataFrame(base_summary) if not isinstance(base_summary, pd.DataFrame) else base_summary
    var_df  = pd.DataFrame(var_summary)  if not isinstance(var_summary,  pd.DataFrame) else var_summary

    diff = _per_window_diff(base_df, var_df)
    passed, verdict_text = _verdict(diff, n_pass=n_pass)

    out = out_dir or (config.DATA_DIR / "reports" / f"compare_{feature}")
    out.mkdir(parents=True, exist_ok=True)
    base_df.to_csv(out / "baseline_wfo_summary.csv", index=False)
    var_df.to_csv(out / "variant_wfo_summary.csv", index=False)
    diff.to_csv(out / "per_window_diff.csv", index=False)

    header = (
        f"Variant comparison: {feature}\n"
        f"  ({spec['description']})\n"
        f"Universe: {universe} (cap={universe_cap})\n"
        f"Period:   {start} → {end or 'today'}\n"
        f"Windows:  IS={is_days}d, OOS={oos_days}d, step={step_days}d\n"
        f"Objective: {objective}\n"
        f"\n"
    )
    (out / "verdict.txt").write_text(header + verdict_text)

    print(header + verdict_text)
    log.info("Wrote %s/", out)

    return {
        "passed": passed,
        "diff_df": diff,
        "verdict_text": verdict_text,
        "out_dir": out,
    }
