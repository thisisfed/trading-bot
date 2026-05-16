"""
slippage.py
-----------
Turn the `signals_sent` audit rows into a slippage report.

What it computes
----------------
For all signals sent in the last N days where outcome IS NOT NULL:
  - Execution rate (executed / total)
  - Per-trade slippage distribution (mean / median / p10 / p90)
  - Total slippage cost in GBP estimate, given recorded share count
  - Worst-N offenders for review
  - Per-tier breakdown (Tier 1 vs Tier 2 — different execution discipline?)

Why this matters
----------------
The single biggest reason retail systematic strategies fail in live
trading is that the user's actual fills differ from the bot's expected
fills.  A momentum strategy with 35% win rate and 2:1 win/loss ratio
has expectancy of ~0.05R per trade.  If average execution slippage is
0.5% on a 2.5% stop distance, that's 0.2R per trade of lost edge — the
strategy goes from profitable to break-even, and the user has no idea
why because they're comparing live results to bot paper results,
neither of which directly tells them slippage is the culprit.

This module makes the gap visible.  When the live engine is running:
  - Every Telegram alert is logged with the bot's expected fill.
  - The user replies `/done <id> <actual_price>` from their phone.
  - This module aggregates the resulting deltas.

After 30 reported fills, the distribution is meaningful.  If median
slippage is over +0.3%, your execution gap is large enough to matter
and you need to either tighten it (faster reaction time on the open)
or budget for it (raise stops by the expected slippage so the bot
doesn't over-bet).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import pandas as pd

from . import db
from .logging_utils import get_logger

log = get_logger("ics.slippage")


@dataclass
class SlippageReport:
    period_days: int
    n_alerts: int            # total alerts sent in window
    n_resolved: int          # of those, how many got outcomes
    n_executed: int          # subset: actually filled
    n_missed: int            # subset: user said no fill
    n_pending: int           # alerts still awaiting user response
    execution_rate: float    # n_executed / n_resolved (excludes pending)
    # Slippage stats over executed rows only
    mean_slippage_pct: float
    median_slippage_pct: float
    p10_slippage_pct: float
    p90_slippage_pct: float
    worst_slippage_pct: float  # max positive (paid more than expected)
    # Aggregate cost estimate (best-effort — depends on recorded shares)
    total_slippage_cost_usd: float
    # Tier breakdown
    by_tier: dict
    worst_offenders: list    # top-N by abs(slippage_pct)


def _aggregate(rows: List[dict], period_days: int, n_pending: int) -> SlippageReport:
    """Pure-function aggregation — easy to test."""
    n_alerts = len(rows) + n_pending
    n_resolved = len(rows)
    n_executed = sum(1 for r in rows if r.get("outcome") == "executed")
    n_missed = n_resolved - n_executed
    execution_rate = (n_executed / n_resolved) if n_resolved else 0.0

    # Slippage stats over executed rows only.  Filter to rows with a real
    # slippage_pct so missing/None values don't poison the distribution.
    slippages = [
        float(r["slippage_pct"]) for r in rows
        if r.get("outcome") == "executed" and r.get("slippage_pct") is not None
    ]
    if slippages:
        s = pd.Series(slippages)
        mean_s = float(s.mean())
        med_s = float(s.median())
        p10 = float(s.quantile(0.10))
        p90 = float(s.quantile(0.90))
        worst = float(s.max())
    else:
        mean_s = med_s = p10 = p90 = worst = 0.0

    # Cost estimate: sum (slippage_pct * expected_fill * user_shares) per row
    cost_usd = 0.0
    for r in rows:
        if r.get("outcome") != "executed":
            continue
        slip = r.get("slippage_pct")
        fill = r.get("expected_fill_usd")
        shares = r.get("user_shares") or r.get("shares_planned")
        if slip is None or fill is None or shares is None:
            continue
        # Positive slippage = user paid more than bot expected = cost
        cost_usd += float(slip) * float(fill) * int(shares)

    # Tier breakdown
    by_tier: dict = {}
    for tier_val in (1, 2):
        subset = [
            float(r["slippage_pct"]) for r in rows
            if r.get("outcome") == "executed"
            and r.get("slippage_pct") is not None
            and int(r.get("tier") or 0) == tier_val
        ]
        if subset:
            ss = pd.Series(subset)
            by_tier[f"tier_{tier_val}"] = {
                "n": len(subset),
                "mean_slippage_pct": float(ss.mean()),
                "median_slippage_pct": float(ss.median()),
            }
        else:
            by_tier[f"tier_{tier_val}"] = {"n": 0}

    # Worst offenders — sort by abs(slippage) so big-in-either-direction
    # outliers show up; cap at 5 for screen.
    rated = [
        r for r in rows
        if r.get("outcome") == "executed" and r.get("slippage_pct") is not None
    ]
    worst_list = sorted(rated, key=lambda r: abs(r["slippage_pct"]), reverse=True)[:5]

    return SlippageReport(
        period_days=period_days,
        n_alerts=n_alerts, n_resolved=n_resolved,
        n_executed=n_executed, n_missed=n_missed, n_pending=n_pending,
        execution_rate=execution_rate,
        mean_slippage_pct=mean_s, median_slippage_pct=med_s,
        p10_slippage_pct=p10, p90_slippage_pct=p90, worst_slippage_pct=worst,
        total_slippage_cost_usd=cost_usd,
        by_tier=by_tier, worst_offenders=worst_list,
    )


def build_report(days: int = 30) -> SlippageReport:
    """Read DB and aggregate. Use directly from CLI or daily/monthly job."""
    resolved = db.get_execution_audit(days=days)
    pending = db.get_pending_signals(within_days=days)
    return _aggregate(resolved, period_days=days, n_pending=len(pending))


def format_report(rep: SlippageReport) -> str:
    """Render as a multi-line string for terminal / Telegram."""
    lines = []
    lines.append("=" * 64)
    lines.append(f"EXECUTION AUDIT — last {rep.period_days} days")
    lines.append("=" * 64)
    lines.append(f"  Alerts sent:       {rep.n_alerts}")
    lines.append(f"  Resolved:          {rep.n_resolved}")
    lines.append(f"    executed:        {rep.n_executed}")
    lines.append(f"    missed:          {rep.n_missed}")
    lines.append(f"  Pending (no /done):{rep.n_pending}")
    lines.append(f"  Execution rate:    {rep.execution_rate*100:.1f}% "
                 f"(of resolved alerts)")
    lines.append("")
    if rep.n_executed == 0:
        lines.append("No executed fills yet — no slippage stats to show.")
        if rep.n_alerts == 0:
            lines.append("  Run the live engine; alerts will be logged automatically.")
        elif rep.n_pending > 0:
            lines.append(f"  {rep.n_pending} alert(s) awaiting your `/done` response.")
        return "\n".join(lines)

    lines.append("Slippage distribution (positive = paid above bot's expected fill):")
    lines.append(f"  Mean:    {rep.mean_slippage_pct*100:+.3f}%")
    lines.append(f"  Median:  {rep.median_slippage_pct*100:+.3f}%")
    lines.append(f"  p10:     {rep.p10_slippage_pct*100:+.3f}%")
    lines.append(f"  p90:     {rep.p90_slippage_pct*100:+.3f}%")
    lines.append(f"  Worst:   {rep.worst_slippage_pct*100:+.3f}%")
    lines.append("")
    lines.append(f"Estimated total slippage cost over period: "
                 f"${rep.total_slippage_cost_usd:+,.2f}")
    lines.append("")
    lines.append("By tier:")
    for tier in ("tier_1", "tier_2"):
        info = rep.by_tier.get(tier, {"n": 0})
        if info["n"] == 0:
            lines.append(f"  {tier:8s}: no executed fills")
        else:
            lines.append(
                f"  {tier:8s}: n={info['n']:3d}  "
                f"mean={info['mean_slippage_pct']*100:+.3f}%  "
                f"median={info['median_slippage_pct']*100:+.3f}%"
            )
    if rep.worst_offenders:
        lines.append("")
        lines.append("Worst offenders:")
        for r in rep.worst_offenders:
            slip = float(r["slippage_pct"]) * 100
            lines.append(
                f"  [#{r['id']}] {r['ticker']:6s}  "
                f"slippage {slip:+.3f}%  "
                f"expected ${float(r['expected_fill_usd']):.2f}  "
                f"actual ${float(r['user_fill_usd']):.2f}"
            )
    lines.append("")
    lines.append("Interpretation:")
    if abs(rep.median_slippage_pct) < 0.001:
        lines.append("  ✓ Execution is essentially frictionless — median slippage "
                     "is under 10 bps.")
    elif abs(rep.median_slippage_pct) < 0.003:
        lines.append("  ✓ Execution slippage is normal for manual retail trading "
                     "(<30 bps median).")
    elif abs(rep.median_slippage_pct) < 0.005:
        lines.append("  ⚠ Execution slippage is sizable (>30 bps median).  Consider "
                     "tightening your reaction time on the open.")
    else:
        lines.append("  ❌ Execution slippage is large (>50 bps median).  The strategy "
                     "has ~5-10 bps of edge per trade — slippage this size is "
                     "eating most of it.  Investigate or budget for it.")
    return "\n".join(lines)
