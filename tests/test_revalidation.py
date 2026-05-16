"""Tests for paper-status dashboard and revalidation orchestrator."""
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# paper_status
# ---------------------------------------------------------------------------
def test_paper_status_empty_db(tmp_path, monkeypatch):
    """No paper data → result has n_days=0 and no verdict."""
    from ics import config
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    from ics import db
    db.init_db()
    from ics.paper_status import evaluate
    result = evaluate()
    assert result.n_days == 0
    assert result.overall_pass is None


def test_paper_status_insufficient_data(tmp_path, monkeypatch):
    """Less than min_days → overall_pass=None (insufficient)."""
    from ics import config
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    from ics import db
    db.init_db()
    # Insert 5 days of equity
    base = datetime(2024, 1, 1)
    for i in range(5):
        db.insert_equity(
            timestamp=(base + timedelta(days=i)).isoformat(),
            equity_gbp=30000.0 + i * 100,
            cash_gbp=0.0, open_positions=1, source="paper",
        )
    from ics.paper_status import evaluate
    result = evaluate(min_days=30)
    assert result.n_days == 5
    assert result.overall_pass is None


def test_paper_status_passes_when_metrics_good(tmp_path, monkeypatch):
    """Build up enough data with strong metrics to pass."""
    from ics import config
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    from ics import db
    db.init_db()
    # 60 days of monotonically-rising equity → high Sharpe, low MDD
    base = datetime(2024, 1, 1)
    for i in range(60):
        db.insert_equity(
            timestamp=(base + timedelta(days=i)).isoformat(),
            equity_gbp=30000.0 + i * 50.0,
            cash_gbp=0.0, open_positions=1, source="paper",
        )
    # 35 paper trades — manually insert with required NOT NULL columns
    with sqlite3.connect(config.DB_PATH) as c:
        try:
            for i in range(35):
                c.execute(
                    "INSERT INTO trades (ticker, tier, entry_ts, entry_usd, "
                    "shares, fx_entry, pnl_gbp, source) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("AAPL", 1, base.isoformat(), 100.0,
                     10, 0.8, 50.0, "paper"),
                )
        except sqlite3.OperationalError:
            pytest.skip("trades table schema differs in this build")
    # Write a fake WFO report — modest Sharpe so our great paper Sharpe clears
    wfo_dir = tmp_path / "reports" / "fake_wfo"
    wfo_dir.mkdir(parents=True)
    wfo = pd.DataFrame([
        {"Window": "w1", "OOS_Sharpe": 1.0, "OOS_MaxDD_%": 10.0},
        {"Window": "w2", "OOS_Sharpe": 1.2, "OOS_MaxDD_%": 12.0},
    ])
    wfo.to_csv(wfo_dir / "wfo_summary.csv", index=False)

    from ics.paper_status import evaluate
    result = evaluate(min_days=30, min_trades=30)
    assert result.n_days == 60
    assert result.n_trades == 35
    assert result.wfo is not None
    # Monotonic curve → near-infinite Sharpe → comfortably above threshold
    assert result.criteria["sharpe_vs_wfo"]["pass"] is True
    # No drawdowns at all → 0% MDD → far under any threshold
    assert result.criteria["mdd_vs_wfo"]["pass"] is True


# ---------------------------------------------------------------------------
# revalidation: trigger logic
# ---------------------------------------------------------------------------
def test_trigger_first_run_fires_when_no_status_file(tmp_path, monkeypatch):
    """No prior run on record → should_revalidate=True."""
    from ics import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    from ics import db
    db.init_db()
    from ics.revalidation import should_revalidate
    decision = should_revalidate()
    assert decision.should_run is True
    assert "first" in decision.reason.lower() or "no prior" in decision.reason.lower()


def test_trigger_cadence_fires_after_n_days(tmp_path, monkeypatch):
    from ics import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    from ics import db
    db.init_db()
    # Write a status file with last_completed_at 200 days ago
    state_dir = tmp_path / "revalidation"
    state_dir.mkdir(parents=True)
    last = datetime.utcnow() - timedelta(days=200)
    (state_dir / "last_revalidation.json").write_text(
        json.dumps({"last_completed_at": last.isoformat()})
    )
    from ics.revalidation import should_revalidate
    decision = should_revalidate(cadence_days=180)
    assert decision.should_run is True
    assert "cadence" in decision.reason


def test_trigger_does_not_fire_within_cadence(tmp_path, monkeypatch):
    from ics import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    from ics import db
    db.init_db()
    state_dir = tmp_path / "revalidation"
    state_dir.mkdir(parents=True)
    last = datetime.utcnow() - timedelta(days=30)  # 30 < 180
    (state_dir / "last_revalidation.json").write_text(
        json.dumps({"last_completed_at": last.isoformat()})
    )
    from ics.revalidation import should_revalidate
    decision = should_revalidate(cadence_days=180)
    assert decision.should_run is False
    assert decision.days_since_last == 30


# ---------------------------------------------------------------------------
# revalidation: drift diff
# ---------------------------------------------------------------------------
def test_diff_wfo_reports_counts_combo_shifts(tmp_path):
    """Build two synthetic wfo_summary.csv files and check the diff."""
    prev_dir = tmp_path / "multi_2025q4"; prev_dir.mkdir()
    curr_dir = tmp_path / "multi_2026q2"; curr_dir.mkdir()
    prev = pd.DataFrame([
        {"Window": "w1", "Params": "rsi=55", "OOS_Sharpe": 1.0},
        {"Window": "w2", "Params": "rsi=60", "OOS_Sharpe": 1.2},
        {"Window": "w3", "Params": "rsi=65", "OOS_Sharpe": 1.4},
        {"Window": "w4", "Params": "rsi=55", "OOS_Sharpe": 0.9},
    ])
    curr = pd.DataFrame([
        {"Window": "w1", "Params": "rsi=55", "OOS_Sharpe": 1.1},   # same
        {"Window": "w2", "Params": "rsi=70", "OOS_Sharpe": 1.5},   # shifted
        {"Window": "w3", "Params": "rsi=65", "OOS_Sharpe": 1.3},   # same
        {"Window": "w4", "Params": "rsi=70", "OOS_Sharpe": 1.0},   # shifted
    ])
    prev.to_csv(prev_dir / "wfo_summary.csv", index=False)
    curr.to_csv(curr_dir / "wfo_summary.csv", index=False)

    from ics.revalidation import diff_wfo_reports
    drift = diff_wfo_reports(prev_dir / "wfo_summary.csv",
                              curr_dir / "wfo_summary.csv")
    assert drift.windows_compared == 4
    assert drift.windows_with_combo_shift == 2  # w2 and w4
    # Avg Sharpe change: (0.1 + 0.3 + (-0.1) + 0.1) / 4 = 0.1
    assert drift.avg_sharpe_change == pytest.approx(0.1, abs=1e-6)
    # Worst (min) change: -0.1
    assert drift.worst_sharpe_change == pytest.approx(-0.1, abs=1e-6)


def test_format_drift_alert_flags_above_threshold(tmp_path):
    """4+ shifts triggers the warning icon and ACTION text."""
    from ics.revalidation import DriftReport, format_drift_alert
    drift = DriftReport(
        prev_name="multi_2025q4", curr_name="multi_2026q2",
        windows_compared=7, windows_with_combo_shift=5,
        avg_sharpe_change=-0.2, worst_sharpe_change=-0.6,
    )
    text = format_drift_alert(drift, shift_threshold=4)
    assert "⚠" in text
    assert "investigate" in text.lower()


def test_format_drift_alert_clean_when_below_threshold(tmp_path):
    from ics.revalidation import DriftReport, format_drift_alert
    drift = DriftReport(
        prev_name="multi_2025q4", curr_name="multi_2026q2",
        windows_compared=7, windows_with_combo_shift=1,
        avg_sharpe_change=0.02, worst_sharpe_change=-0.05,
    )
    text = format_drift_alert(drift, shift_threshold=4)
    assert "✓" in text
    assert "stable" in text.lower() or "no" in text.lower()


# ---------------------------------------------------------------------------
# Safety invariant: auto-apply is OFF by default
# ---------------------------------------------------------------------------
def test_revalidation_default_auto_apply_is_false():
    """The single most important configuration check."""
    from ics import config
    assert config.REVALIDATION.auto_apply is False, (
        "auto_apply must be False by default — auto-tuning trading systems "
        "is a known way to destroy them quietly."
    )
