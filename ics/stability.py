"""
stability.py
------------
Parameter stability analysis across walk-forward windows.

After a WFO run, each window has produced a "best combo" (the IS-optimal
parameter set).  This module aggregates those combos and tells you, per
parameter, whether the value being picked is:

  STRONG SIGNAL  — the same value wins in ≥75% of windows.  This parameter
                   is doing real work; keep it and lock the value in.

  MODERATE       — the same value wins in 50–75% of windows.  Probably real
                   but with some sensitivity.  Keep but consider testing
                   adjacent values in a refined grid.

  NOISE          — values are spread close to uniformly across the grid.
                   The IS optimiser is flipping randomly between them, which
                   means this parameter doesn't materially affect IS scores.
                   Drop it (lock to a sensible default) to avoid wasting WFO
                   compute and inviting overfitting.

  DEAD-OFF       — the optimiser always picks the "disabled" value of a
                   feature flag (e.g. vix_max=999 means VIX check off).
                   That filter isn't earning its keep — turn it off and
                   simplify.

  DEAD-ON        — the optimiser always picks the "enabled" value.  The
                   filter is universally useful — hard-code it on.

Usage
-----
    from ics.stability import analyse_combos, render_report

    combos = [w["best_combo"] for w in wfo_result["windows"] if w.get("best_combo")]
    report = analyse_combos(combos, grid=DEFAULT_PARAM_GRID)
    print(render_report(report))

When called from multi_wfo, the combos from EVERY objective × EVERY window
get pooled, which gives more samples for stability analysis (8 windows × 3
objectives = 24 picks per parameter instead of 8).  That's the right thing
to do — if a parameter is genuinely useful, it should win regardless of
which objective is being optimised.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pandas as pd


# ---------------------------------------------------------------------------
# Disabled-sentinel registry
# ---------------------------------------------------------------------------
# For feature-flag parameters where one value means "filter disabled", we
# need to know which value is the disabled sentinel so we can emit DEAD-OFF
# vs STRONG SIGNAL correctly.  Update this map when adding new flags to the
# WFO grid.
DISABLED_SENTINELS: Dict[str, Any] = {
    # Regime filter knobs
    "vix_max": 999.0,                 # 999 = VIX check off
    "max_spy_drawdown_pct": 0.99,     # 99% = drawdown check off
    # Boolean flags
    "require_weekly_hma_bullish": False,  # False = filter off
}


@dataclass
class ParamReport:
    name: str
    counts: Dict[Any, int]                  # value → number of windows
    grid_values: List[Any]                  # available values from grid
    n_windows: int
    mode_value: Any
    mode_share: float                       # 0-1
    distinct_values_picked: int
    verdict: str                            # STRONG / MODERATE / NOISE / DEAD-OFF / DEAD-ON
    recommendation: str


@dataclass
class StabilityReport:
    n_windows: int
    n_objectives: Optional[int]
    parameters: List[ParamReport] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
def analyse_combos(
    combos: List[Dict[str, Any]],
    grid: Optional[Dict[str, List]] = None,
    n_objectives: Optional[int] = None,
    *,
    strong_threshold: float = 0.75,
    moderate_threshold: float = 0.50,
    noise_threshold: float = 0.40,  # mode share below this on a multi-value grid → noise
) -> StabilityReport:
    """
    Aggregate a list of best-combo dicts into a per-parameter stability report.

    Parameters
    ----------
    combos : list of dict
        One per WFO window (or per window × objective when pooled).
    grid : dict | None
        The parameter grid that was searched.  Used to know what values were
        available — important for distinguishing "always picks X" from
        "always picks X because X is the only option".  If None, derived
        from the union of values seen across combos.
    n_objectives : int | None
        How many objectives were pooled in `combos`, for the header.  None
        if the caller doesn't know.
    strong_threshold, moderate_threshold, noise_threshold : float
        Thresholds for the verdict tiers.

    Returns
    -------
    StabilityReport
    """
    if not combos:
        return StabilityReport(n_windows=0, n_objectives=n_objectives, parameters=[])

    # Discover parameter names from the union of all combo keys
    all_keys = sorted({k for c in combos for k in c.keys()})

    reports: List[ParamReport] = []
    for key in all_keys:
        values_picked = [c[key] for c in combos if key in c]
        if not values_picked:
            continue

        counts: Dict[Any, int] = {}
        for v in values_picked:
            counts[v] = counts.get(v, 0) + 1

        # Sort counts descending for the report
        counts = dict(sorted(counts.items(), key=lambda kv: -kv[1]))
        mode_value = next(iter(counts))
        mode_share = counts[mode_value] / len(values_picked)

        grid_vals = list(grid.get(key, [])) if grid else sorted(counts.keys(),
                                                                key=lambda x: str(x))
        if not grid_vals:
            grid_vals = sorted(counts.keys(), key=lambda x: str(x))

        distinct_in_grid = len(grid_vals)
        distinct_picked = len(counts)

        # --- Verdict logic ---
        verdict, recommendation = _verdict_for(
            key, mode_value, mode_share,
            distinct_picked, distinct_in_grid,
            strong_threshold, moderate_threshold, noise_threshold,
        )

        reports.append(ParamReport(
            name=key,
            counts=counts,
            grid_values=grid_vals,
            n_windows=len(values_picked),
            mode_value=mode_value,
            mode_share=mode_share,
            distinct_values_picked=distinct_picked,
            verdict=verdict,
            recommendation=recommendation,
        ))

    return StabilityReport(
        n_windows=len(combos),
        n_objectives=n_objectives,
        parameters=reports,
    )


def _verdict_for(
    key: str, mode_value: Any, mode_share: float,
    distinct_picked: int, distinct_in_grid: int,
    strong: float, moderate: float, noise: float,
) -> tuple[str, str]:
    """Return (verdict_label, recommendation_text)."""
    sentinel = DISABLED_SENTINELS.get(key)
    is_disabled_pick = (sentinel is not None) and _values_equal(mode_value, sentinel)

    # If the grid only had one value, there's nothing to learn
    if distinct_in_grid <= 1:
        return ("SINGLETON",
                f"Only one value in the grid; no stability info available for {key}.")

    if mode_share >= strong:
        if is_disabled_pick:
            return (
                "DEAD-OFF",
                f"The IS optimiser disabled this filter in {mode_share*100:.0f}% of "
                f"windows.  Recommendation: turn {key} OFF permanently and remove "
                f"it from the grid (saves WFO compute)."
            )
        # Boolean True picked overwhelmingly
        if mode_value is True and sentinel is False:
            return (
                "DEAD-ON",
                f"The IS optimiser kept {key}=True in {mode_share*100:.0f}% of "
                f"windows.  Recommendation: hard-code {key}=True and remove from "
                f"the grid."
            )
        return (
            "STRONG SIGNAL",
            f"{key} converged on {mode_value!r} in {mode_share*100:.0f}% of "
            f"windows.  Lock this value and consider testing values "
            f"adjacent to it in a refined grid."
        )

    # Below strong threshold.  Decide noise vs moderate based on how close
    # the distribution is to uniform-across-the-grid.
    #
    # Uniform mode share for a grid of size N is ~1/N (e.g. 50% for 2 values,
    # 33% for 3).  We call something "noise" if the mode share is within a
    # margin of that uniform value AND most/all grid values were sampled.
    #
    # The fixed `noise` threshold from the function args is kept as an
    # absolute floor — anything below it is noise regardless.
    uniform_share = 1.0 / distinct_in_grid if distinct_in_grid > 0 else 0
    near_uniform = mode_share <= uniform_share + 0.10
    well_explored = distinct_picked >= max(2, int(distinct_in_grid * 0.66))

    if mode_share <= noise or (near_uniform and well_explored):
        return (
            "NOISE",
            f"Mode share only {mode_share*100:.0f}% across {distinct_picked} "
            f"of {distinct_in_grid} grid values (uniform would be "
            f"{uniform_share*100:.0f}%).  This parameter doesn't materially "
            f"affect the IS score.  Recommendation: lock to its default and "
            f"remove from the grid."
        )

    if mode_share >= moderate:
        return (
            "MODERATE",
            f"Most-common value {mode_value!r} won {mode_share*100:.0f}% of "
            f"windows; the rest were spread across other values.  Real but "
            f"sensitive.  Keep in the grid for now; revisit after more data."
        )

    return (
        "WEAK",
        f"Mode share {mode_share*100:.0f}%; not converged but not pure noise. "
        f"Consider narrowing the grid or collecting more windows before deciding."
    )


def _values_equal(a: Any, b: Any) -> bool:
    """Tolerant equality for floats that might come back as 0.99 vs 0.99000001."""
    try:
        return abs(float(a) - float(b)) < 1e-9
    except (TypeError, ValueError):
        return a == b


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
VERDICT_EMOJI = {
    "STRONG SIGNAL": "✅",
    "MODERATE":      "⚠️ ",
    "WEAK":          "⚠️ ",
    "NOISE":         "❌",
    "DEAD-OFF":      "🚫",
    "DEAD-ON":       "🔒",
    "SINGLETON":     "—",
}


def render_report(report: StabilityReport) -> str:
    """Format a StabilityReport as a human-readable string."""
    if report.n_windows == 0:
        return "No combos to analyse — did the WFO run produce any windows?\n"

    header = ["=" * 80,
              "  PARAMETER STABILITY ANALYSIS",
              "=" * 80,
              ""]
    if report.n_objectives:
        header.append(
            f"  Pooled across {report.n_objectives} objectives × "
            f"{report.n_windows // report.n_objectives} windows = "
            f"{report.n_windows} parameter picks per row.\n"
        )
    else:
        header.append(f"  {report.n_windows} windows analysed.\n")

    body = []
    for p in report.parameters:
        emoji = VERDICT_EMOJI.get(p.verdict, "  ")
        body.append("-" * 80)
        body.append(f"  {emoji}  {p.name}   →   {p.verdict}")
        body.append("-" * 80)

        # Distribution
        body.append("  Picks across windows:")
        for v, c in p.counts.items():
            pct = c / p.n_windows * 100
            bar = "█" * int(round(pct / 5))  # each block = 5%
            body.append(f"    {repr(v):>12s}  {c:>3d} / {p.n_windows} "
                        f"({pct:5.1f}%) {bar}")

        # Show unpicked grid values too
        unpicked = [v for v in p.grid_values if v not in p.counts]
        if unpicked:
            body.append("  Grid values never picked:")
            for v in unpicked:
                body.append(f"    {repr(v):>12s}")

        body.append("")
        body.append(f"  → {p.recommendation}")
        body.append("")

    summary = ["=" * 80,
               "  ACTIONABLE SUMMARY",
               "=" * 80,
               ""]
    keep_strong = [p.name for p in report.parameters if p.verdict == "STRONG SIGNAL"]
    drop_noise  = [p.name for p in report.parameters if p.verdict == "NOISE"]
    dead_off    = [p.name for p in report.parameters if p.verdict == "DEAD-OFF"]
    dead_on     = [p.name for p in report.parameters if p.verdict == "DEAD-ON"]
    moderate    = [p.name for p in report.parameters if p.verdict in ("MODERATE", "WEAK")]

    if keep_strong:
        summary.append(f"✅ KEEP & LOCK ({len(keep_strong)}): " + ", ".join(keep_strong))
    if dead_on:
        summary.append(f"🔒 LOCK ON ({len(dead_on)}): " + ", ".join(dead_on))
    if dead_off:
        summary.append(f"🚫 TURN OFF ({len(dead_off)}): " + ", ".join(dead_off))
    if drop_noise:
        summary.append(f"❌ DROP FROM GRID ({len(drop_noise)}): " + ", ".join(drop_noise))
    if moderate:
        summary.append(f"⚠️  KEEP, MONITOR ({len(moderate)}): " + ", ".join(moderate))

    if not (keep_strong or dead_off or dead_on or drop_noise or moderate):
        summary.append("Inconclusive — collect more windows or run more objectives.")

    summary.append("")
    return "\n".join(header + body + summary) + "\n"
