"""
paper_status.py
---------------
Compare the LIVE paper-trading equity curve against the most recent WFO
out-of-sample metrics, and apply the paper-to-live pass criteria.

Pure function: doesn't auto-trigger anything, doesn't change parameters.
This is a status check, not a controller.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd

from . import config
from .performance import (
    sharpe as _sharpe,
    sortino as _sortino,
    max_drawdown as _mdd,
    cagr as _cagr,
)
from .logging_utils import get_logger

log = get_logger("ics.paper_status")


# ---------------------------------------------------------------------------
# Data fetchers
# ---------------------------------------------------------------------------
def _load_paper_equity() -> pd.Series:
    """Return paper equity curve as date-indexed GBP series, empty if no rows."""
    with sqlite3.connect(config.DB_PATH) as c:
        c.row_factory = sqlite3.Row
        try:
            rows = c.execute(
                "SELECT timestamp, equity_gbp FROM equity "
                "WHERE source = 'paper' ORDER BY timestamp ASC"
            ).fetchall()
        except sqlite3.OperationalError:
            return pd.Series(dtype=float, name="equity_gbp")
    if not rows:
        return pd.Series(dtype=float, name="equity_gbp")
    df = pd.DataFrame([dict(r) for r in rows])
    df["ts"] = pd.to_datetime(df["timestamp"])
    df["date"] = df["ts"].dt.normalize()
    # Multiple snapshots per day → keep the LAST (end-of-day equity)
    df = df.sort_values("ts").drop_duplicates("date", keep="last")
    s = df.set_index("date")["equity_gbp"].astype(float)
    s.name = "equity_gbp"
    return s


def _count_paper_trades() -> int:
    """Count rows in the trades table tagged source='paper'."""
    with sqlite3.connect(config.DB_PATH) as c:
        try:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM trades WHERE source = 'paper'"
            ).fetchone()
            return int(row[0]) if row else 0
        except sqlite3.OperationalError:
            return 0


def _find_latest_wfo_report(name: Optional[str] = None) -> Optional[Path]:
    """Most recent wfo_summary.csv under data/reports, or one matching `name`."""
    reports_dir = config.DATA_DIR / "reports"
    if not reports_dir.exists():
        return None
    if name:
        candidate = reports_dir / name / "wfo_summary.csv"
        return candidate if candidate.exists() else None
    candidates = list(reports_dir.glob("*/wfo_summary.csv"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _load_wfo_baseline(report_path: Path) -> dict:
    """Read per-window OOS metrics, return summary aggregates."""
    df = pd.read_csv(report_path)
    needed = {"OOS_Sharpe", "OOS_MaxDD_%"}
    if not needed.issubset(df.columns):
        raise ValueError(
            f"WFO report at {report_path} missing columns: {needed - set(df.columns)}"
        )
    return {
        "avg_oos_sharpe": float(df["OOS_Sharpe"].mean()),
        "median_oos_sharpe": float(df["OOS_Sharpe"].median()),
        "worst_oos_mdd_pct": float(df["OOS_MaxDD_%"].max()),
        "median_oos_mdd_pct": float(df["OOS_MaxDD_%"].median()),
        "n_windows": int(len(df)),
        "report_path": str(report_path),
        "report_name": report_path.parent.name,
    }


# ---------------------------------------------------------------------------
# Result container + criteria evaluation
# ---------------------------------------------------------------------------
@dataclass
class PaperStatusResult:
    n_days: int
    n_trades: int
    paper_sharpe: float
    paper_mdd_pct: float
    paper_cagr_pct: float
    paper_sortino: float
    wfo: Optional[dict]
    criteria: dict
    overall_pass: Optional[bool]    # None = insufficient data
    paper_sharpe_drift_pct: Optional[float] = None  # how far paper is below WFO


def evaluate(
    min_days: int = 30,
    min_trades: int = 30,
    sharpe_ratio_threshold: float = 0.5,
    mdd_ratio_threshold: float = 1.5,
    wfo_name: Optional[str] = None,
) -> PaperStatusResult:
    """Build a PaperStatusResult from current DB + report state."""
    eq = _load_paper_equity()
    n_days = len(eq)
    n_trades = _count_paper_trades()

    if n_days == 0:
        return PaperStatusResult(
            n_days=0, n_trades=n_trades,
            paper_sharpe=0.0, paper_mdd_pct=0.0, paper_cagr_pct=0.0, paper_sortino=0.0,
            wfo=None, criteria={}, overall_pass=None, paper_sharpe_drift_pct=None,
        )

    paper_sharpe = _sharpe(eq)
    paper_sortino = _sortino(eq)
    paper_mdd_pct = abs(_mdd(eq)) * 100.0
    paper_cagr_pct = _cagr(eq) * 100.0

    wfo_path = _find_latest_wfo_report(wfo_name)
    wfo = None
    if wfo_path is not None:
        try:
            wfo = _load_wfo_baseline(wfo_path)
        except Exception as e:
            log.warning("Could not load WFO baseline from %s: %s", wfo_path, e)

    criteria: dict = {}
    criteria["days_of_data"] = {
        "required": f">= {min_days}",
        "actual": str(n_days),
        "pass": bool(n_days >= min_days),
    }
    criteria["trade_count"] = {
        "required": f">= {min_trades}",
        "actual": str(n_trades),
        "pass": bool(n_trades >= min_trades),
    }

    drift_pct: Optional[float] = None
    if wfo is not None:
        sharpe_target = wfo["avg_oos_sharpe"] * sharpe_ratio_threshold
        mdd_target_pct = wfo["worst_oos_mdd_pct"] * mdd_ratio_threshold
        criteria["sharpe_vs_wfo"] = {
            "required": (f">= {sharpe_target:.3f}  "
                         f"({sharpe_ratio_threshold*100:.0f}% of WFO {wfo['avg_oos_sharpe']:.3f})"),
            "actual": f"{paper_sharpe:.3f}",
            "pass": bool(paper_sharpe >= sharpe_target),
        }
        criteria["mdd_vs_wfo"] = {
            "required": (f"<= {mdd_target_pct:.2f}%  "
                         f"({mdd_ratio_threshold:.1f}x WFO worst {wfo['worst_oos_mdd_pct']:.2f}%)"),
            "actual": f"{paper_mdd_pct:.2f}%",
            "pass": bool(paper_mdd_pct <= mdd_target_pct),
        }
        # Drift = how far paper Sharpe sits below WFO avg (as % of WFO).
        # Negative number means paper is BELOW WFO (the worry case).
        if wfo["avg_oos_sharpe"] > 0:
            drift_pct = (paper_sharpe - wfo["avg_oos_sharpe"]) / wfo["avg_oos_sharpe"] * 100.0
    else:
        criteria["wfo_baseline"] = {
            "required": "WFO report under data/reports/",
            "actual": "none found — run `multi-wfo` first",
            "pass": False,
        }

    overall: Optional[bool]
    if n_days < min_days:
        overall = None
    else:
        overall = all(c["pass"] for c in criteria.values())

    return PaperStatusResult(
        n_days=n_days, n_trades=n_trades,
        paper_sharpe=paper_sharpe, paper_mdd_pct=paper_mdd_pct,
        paper_cagr_pct=paper_cagr_pct, paper_sortino=paper_sortino,
        wfo=wfo, criteria=criteria, overall_pass=overall,
        paper_sharpe_drift_pct=drift_pct,
    )


# ---------------------------------------------------------------------------
# Human-readable report
# ---------------------------------------------------------------------------
def format_report(result: PaperStatusResult) -> str:
    lines = ["=" * 72, "ICS PAPER-TO-LIVE STATUS CHECK", "=" * 72, ""]
    lines.append(f"Paper data:")
    lines.append(f"  Equity snapshots:  {result.n_days} days")
    lines.append(f"  Paper trades:      {result.n_trades}")
    lines.append("")

    if result.n_days == 0:
        lines.append("⚠ No paper equity snapshots in the database yet.")
        lines.append("  - Is the live process running? It needs to be up around US market close")
        lines.append("    each day to write the daily snapshot.")
        lines.append("  - Check `record_equity_snapshot` is being called once per day from live.py.")
        return "\n".join(lines)

    lines.append("Paper metrics:")
    lines.append(f"  Sharpe:            {result.paper_sharpe:.3f}")
    lines.append(f"  Sortino:           {result.paper_sortino:.3f}")
    lines.append(f"  Max drawdown:      {result.paper_mdd_pct:.2f}%")
    lines.append(f"  Annualized return: {result.paper_cagr_pct:.2f}%")
    lines.append("")

    if result.wfo is not None:
        lines.append(f"WFO baseline ({result.wfo['report_name']}):")
        lines.append(f"  Avg OOS Sharpe:    {result.wfo['avg_oos_sharpe']:.3f}  "
                     f"(median {result.wfo['median_oos_sharpe']:.3f})")
        lines.append(f"  Worst OOS MDD:     {result.wfo['worst_oos_mdd_pct']:.2f}%  "
                     f"(median {result.wfo['median_oos_mdd_pct']:.2f}%)")
        lines.append(f"  Windows in WFO:    {result.wfo['n_windows']}")
        if result.paper_sharpe_drift_pct is not None:
            sign = "+" if result.paper_sharpe_drift_pct >= 0 else ""
            lines.append(f"  Paper-vs-WFO drift: {sign}{result.paper_sharpe_drift_pct:.1f}% "
                         f"(of WFO Sharpe)")
        lines.append("")
    else:
        lines.append("⚠ No WFO baseline found — run `python -m ics.cli multi-wfo` first.")
        lines.append("")

    lines.append("Pass criteria:")
    for name, info in result.criteria.items():
        mark = "✓" if info["pass"] else "✗"
        lines.append(f"  [{mark}] {name}")
        lines.append(f"        required: {info['required']}")
        lines.append(f"        actual:   {info['actual']}")
    lines.append("")
    lines.append("Manual self-attest (criterion 4 — not checked automatically):")
    lines.append("  [ ] Did I follow the bot's signals without manual override?")
    lines.append("      If you ignored a stop, took a trade the bot didn't fire,")
    lines.append("      or paused entries because you didn't 'like' a signal —")
    lines.append("      the paper period doesn't count. Restart the clock.")
    lines.append("")

    if result.overall_pass is None:
        lines.append(f"VERDICT: ⏳ INSUFFICIENT DATA "
                     f"(need {result.criteria['days_of_data']['required']} days, "
                     f"have {result.n_days})")
    elif result.overall_pass:
        lines.append("VERDICT: ✅ AUTOMATED CRITERIA PASS")
        lines.append("  Manually self-attest criterion 4 (above) before going live.")
    else:
        lines.append("VERDICT: ❌ NOT READY FOR LIVE")
        failing = [n for n, c in result.criteria.items() if not c["pass"]]
        lines.append(f"  Failing: {', '.join(failing)}")
    return "\n".join(lines)
