"""
revalidation.py
---------------
Scheduler + drift-detector for periodic strategy re-validation.

WHAT THIS DOES
--------------
- Decides whether it's time to re-run the WFO (cadence-based or
  paper-drift-triggered).
- Runs `multi_wfo` with a date-stamped report name.
- Diffs the new report against the previous one — counts how many
  windows changed their best-IS combo ("category shift").
- Sends a Telegram alert summarising what changed and what (if anything)
  the user should do.
- Writes a status file under data/revalidation/ so the live engine knows
  when the last check ran.

WHAT THIS DOES NOT DO
---------------------
- It does NOT auto-apply new parameters.  Default `auto_apply=False`,
  enforced by a print-on-startup warning if the user ever flips it.
- It does NOT use the WFO output to "improve" anything.  WFO is the
  audit; this module shows you the audit's findings.

The orchestrator never mutates config.py or any other source-of-truth
parameter file.  Parameter changes are a human decision.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

from . import config
from .logging_utils import get_logger
from .paper_status import evaluate as paper_evaluate

log = get_logger("ics.revalidation")


# ---------------------------------------------------------------------------
# State file
# ---------------------------------------------------------------------------
def _status_dir() -> Path:
    d = config.DATA_DIR / "revalidation"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _status_path() -> Path:
    return _status_dir() / "last_revalidation.json"


def _read_status() -> dict:
    p = _status_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception as e:
        log.warning("Could not read revalidation status: %s", e)
        return {}


def _write_status(payload: dict) -> None:
    _status_path().write_text(json.dumps(payload, indent=2, default=str))


# ---------------------------------------------------------------------------
# Trigger logic — pure function, no side effects
# ---------------------------------------------------------------------------
@dataclass
class TriggerDecision:
    should_run: bool
    reason: str
    days_since_last: Optional[int] = None
    paper_drift_pct: Optional[float] = None


def should_revalidate(
    now: Optional[datetime] = None,
    cadence_days: Optional[int] = None,
    paper_drift_threshold_pct: Optional[float] = None,
) -> TriggerDecision:
    """
    Decide whether to re-run the WFO right now.

    Two triggers, OR'd together:
      1. Cadence: last revalidation was more than `cadence_days` ago.
      2. Paper drift: paper Sharpe is below WFO Sharpe by more than
         `paper_drift_threshold_pct`%, AND we have enough paper data to
         judge (min 30 days).

    Parameters default to `config.REVALIDATION`.
    """
    cfg = config.REVALIDATION
    now = now or datetime.utcnow()
    cadence_days = cadence_days if cadence_days is not None else cfg.cadence_days
    drift_thresh = paper_drift_threshold_pct if paper_drift_threshold_pct is not None else cfg.paper_drift_threshold_pct

    status = _read_status()
    last_ts_str = status.get("last_completed_at")

    # Cadence check
    days_since_last: Optional[int] = None
    if last_ts_str is None:
        # Never run before → trigger
        return TriggerDecision(True, "first revalidation (no prior run on record)",
                               days_since_last=None)
    try:
        last_ts = datetime.fromisoformat(last_ts_str)
    except Exception:
        return TriggerDecision(True, f"unparsable last-run timestamp: {last_ts_str!r}")
    days_since_last = (now - last_ts).days

    if days_since_last >= cadence_days:
        return TriggerDecision(
            True, f"cadence trigger: {days_since_last} days since last run "
                  f"(threshold {cadence_days})",
            days_since_last=days_since_last,
        )

    # Paper-drift check
    drift_pct: Optional[float] = None
    try:
        ps = paper_evaluate()
        drift_pct = ps.paper_sharpe_drift_pct
    except Exception as e:
        log.warning("Paper status check failed during trigger eval: %s", e)
        drift_pct = None

    if drift_pct is not None and drift_pct <= -abs(drift_thresh):
        return TriggerDecision(
            True, f"paper-drift trigger: paper Sharpe is {drift_pct:.1f}% "
                  f"below WFO avg (threshold {-abs(drift_thresh):.1f}%)",
            days_since_last=days_since_last, paper_drift_pct=drift_pct,
        )

    return TriggerDecision(
        False, f"not due (last run {days_since_last}d ago, "
               f"drift {drift_pct if drift_pct is not None else 'n/a'}%)",
        days_since_last=days_since_last, paper_drift_pct=drift_pct,
    )


# ---------------------------------------------------------------------------
# Drift detection between two WFO reports
# ---------------------------------------------------------------------------
@dataclass
class DriftReport:
    """Summarises how much two WFO runs disagree about best parameters."""
    prev_name: str
    curr_name: str
    windows_compared: int
    windows_with_combo_shift: int
    avg_sharpe_change: float
    worst_sharpe_change: float
    per_window: list = field(default_factory=list)


def diff_wfo_reports(prev_csv: Path, curr_csv: Path) -> DriftReport:
    """
    Compare two wfo_summary.csv files.  Joins on Window column and counts
    how many windows have a different `Params` value (the best-IS combo).
    Also computes Sharpe changes per window.
    """
    prev = pd.read_csv(prev_csv)
    curr = pd.read_csv(curr_csv)
    if "Window" not in prev.columns or "Window" not in curr.columns:
        raise ValueError("Both reports must have a 'Window' column")

    join = prev.merge(curr, on="Window", suffixes=("_prev", "_curr"), how="inner")
    n = len(join)
    if n == 0:
        return DriftReport(
            prev_name=prev_csv.parent.name, curr_name=curr_csv.parent.name,
            windows_compared=0, windows_with_combo_shift=0,
            avg_sharpe_change=0.0, worst_sharpe_change=0.0, per_window=[],
        )

    # Compare Params column ("category shift" = different best-IS combo)
    shifts = 0
    per_window = []
    for _, row in join.iterrows():
        pp = str(row.get("Params_prev", ""))
        cp = str(row.get("Params_curr", ""))
        shifted = pp != cp
        if shifted:
            shifts += 1
        ds = float(row.get("OOS_Sharpe_curr", 0.0)) - float(row.get("OOS_Sharpe_prev", 0.0))
        per_window.append({
            "window": row["Window"],
            "prev_params": pp, "curr_params": cp,
            "prev_oos_sharpe": float(row.get("OOS_Sharpe_prev", 0.0)),
            "curr_oos_sharpe": float(row.get("OOS_Sharpe_curr", 0.0)),
            "delta_oos_sharpe": ds,
            "combo_shifted": shifted,
        })

    sharpe_changes = [w["delta_oos_sharpe"] for w in per_window]
    return DriftReport(
        prev_name=prev_csv.parent.name, curr_name=curr_csv.parent.name,
        windows_compared=n, windows_with_combo_shift=shifts,
        avg_sharpe_change=sum(sharpe_changes) / n if sharpe_changes else 0.0,
        worst_sharpe_change=min(sharpe_changes) if sharpe_changes else 0.0,
        per_window=per_window,
    )


def find_previous_wfo_report(current_name: str) -> Optional[Path]:
    """
    Find the most recent multi/wfo report that is NOT `current_name`.
    Looks under data/reports/.
    """
    reports = config.DATA_DIR / "reports"
    if not reports.exists():
        return None
    candidates = []
    for csv in reports.glob("*/wfo_summary.csv"):
        if csv.parent.name == current_name:
            continue
        candidates.append(csv)
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------
def format_drift_alert(drift: DriftReport,
                       shift_threshold: int = 4) -> str:
    """Produce a short text suitable for a Telegram alert."""
    lines = []
    if drift.windows_compared == 0:
        lines.append("⚠ Re-validation complete but no overlapping windows found "
                     f"between {drift.prev_name} and {drift.curr_name}.")
        return "\n".join(lines)

    flagged = drift.windows_with_combo_shift >= shift_threshold
    icon = "⚠" if flagged else "✓"
    lines.append(f"{icon} ICS re-validation complete: {drift.curr_name}")
    lines.append(f"  vs prev: {drift.prev_name}")
    lines.append(f"  Windows compared:    {drift.windows_compared}")
    lines.append(f"  Best-combo shifts:   {drift.windows_with_combo_shift} "
                 f"({'>= ' + str(shift_threshold) if flagged else '< ' + str(shift_threshold)})")
    lines.append(f"  Avg OOS Sharpe Δ:    {drift.avg_sharpe_change:+.3f}")
    lines.append(f"  Worst OOS Sharpe Δ:  {drift.worst_sharpe_change:+.3f}")
    lines.append("")
    if flagged:
        lines.append("ACTION: investigate the new report before adjusting anything.")
        lines.append("  Open data/reports/" + drift.curr_name + "/wfo_summary.csv")
        lines.append("  Auto-apply remains OFF.  Parameter changes are a human decision.")
    else:
        lines.append("ACTION: none.  Strategy is stable.  Auto-apply remains OFF either way.")
    return "\n".join(lines)


def run_scheduled_revalidation(
    universe: str = "nasdaq100",
    start: str = "2020-01-01",
    is_days: int = 504,
    oos_days: int = 252,
    step_days: int = 252,
    name_override: Optional[str] = None,
    notify: bool = True,
    dry_run: bool = False,
) -> dict:
    """
    Run a multi-WFO, diff against the previous, alert if drift exceeds
    threshold, save status.  Returns a dict with the outcomes.

    `dry_run=True` skips the WFO itself (useful for testing the trigger
    + diff + notify pipeline without 20 minutes of compute).
    """
    cfg = config.REVALIDATION
    now = datetime.utcnow()
    # Use ISO-week-friendly name: multi_<YYYY>q<Q>
    quarter = (now.month - 1) // 3 + 1
    name = name_override or f"multi_{now.year}q{quarter}"

    log.info("=== Scheduled revalidation: %s ===", name)
    log.info("  cadence_days=%d, drift_threshold=%.1f%%, auto_apply=%s",
             cfg.cadence_days, cfg.paper_drift_threshold_pct, cfg.auto_apply)

    if cfg.auto_apply:
        log.warning("=" * 60)
        log.warning("AUTO-APPLY IS ENABLED.  Parameter changes from this WFO")
        log.warning("WILL be written to disk WITHOUT human review.  This is")
        log.warning("STRONGLY DISCOURAGED.  Set config.REVALIDATION.auto_apply")
        log.warning("= False to restore safe defaults.")
        log.warning("=" * 60)

    summary = {
        "name": name,
        "started_at": now.isoformat(),
        "dry_run": dry_run,
        "auto_apply": cfg.auto_apply,
        "wfo_ran": False,
        "drift": None,
        "notified": False,
        "applied": False,
    }

    if not dry_run:
        from .multi_wfo import run_multi_wfo
        try:
            run_multi_wfo(
                universe=universe, start=start,
                is_days=is_days, oos_days=oos_days, step_days=step_days,
                name=name,
            )
            summary["wfo_ran"] = True
        except Exception as e:
            log.error("WFO run failed: %s", e)
            summary["error"] = str(e)
            return summary

    # Locate the report we just produced (or the latest if dry_run)
    curr_csv = config.DATA_DIR / "reports" / name / "wfo_summary.csv"
    if not curr_csv.exists():
        # Fall back to most recent
        from .paper_status import _find_latest_wfo_report
        latest = _find_latest_wfo_report()
        if latest is None:
            log.warning("No WFO report found post-run.  Aborting drift step.")
            return summary
        curr_csv = latest
        name = curr_csv.parent.name

    prev_csv = find_previous_wfo_report(current_name=name)
    if prev_csv is None:
        log.info("No previous WFO report — skipping drift comparison.")
        msg = f"✓ Initial revalidation complete: {name}.  No previous report to compare."
        summary["drift"] = None
    else:
        drift = diff_wfo_reports(prev_csv, curr_csv)
        summary["drift"] = {
            "prev_name": drift.prev_name,
            "curr_name": drift.curr_name,
            "windows_compared": drift.windows_compared,
            "windows_with_combo_shift": drift.windows_with_combo_shift,
            "avg_sharpe_change": drift.avg_sharpe_change,
            "worst_sharpe_change": drift.worst_sharpe_change,
        }
        msg = format_drift_alert(drift, shift_threshold=cfg.combo_shift_threshold)

    log.info("\n%s", msg)

    if notify and cfg.notify_telegram:
        try:
            from . import alerts
            alerts.send_telegram(msg)
            summary["notified"] = True
        except Exception as e:
            log.warning("Telegram notify failed: %s", e)

    # auto-apply is intentionally NOT implemented in this version.  Even if
    # the flag is True, we only log a final warning.  Parameter changes
    # require code edits in config.py.  Future maintainers: leave it that way.
    if cfg.auto_apply:
        log.warning("auto_apply=True but parameter writeback is intentionally "
                    "not implemented.  Edit config.py manually if the drift "
                    "report justifies a change.")

    # Persist last-run state
    summary["completed_at"] = datetime.utcnow().isoformat()
    state = _read_status()
    state["last_completed_at"] = summary["completed_at"]
    state["last_name"] = name
    state["last_summary"] = summary
    _write_status(state)

    return summary
