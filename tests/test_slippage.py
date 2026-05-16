"""Tests for the slippage / execution-audit feature."""
from datetime import datetime, timedelta

import pytest


# ---------------------------------------------------------------------------
# DB helpers: record_signal_sent / record_user_execution
# ---------------------------------------------------------------------------
def test_record_signal_sent_returns_id(tmp_path, monkeypatch):
    from ics import config
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    from ics import db
    db.init_db()
    sid = db.record_signal_sent(
        ticker="AAPL", tier=1, expected_fill_usd=178.42,
        stop_usd=174.0, target_usd=185.0, shares_planned=25,
    )
    assert isinstance(sid, int) and sid >= 1


def test_record_user_execution_by_id_computes_slippage(tmp_path, monkeypatch):
    """Filled at 179.00 vs expected 178.42 → slippage = +0.325%."""
    from ics import config
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    from ics import db
    db.init_db()
    sid = db.record_signal_sent(
        ticker="AAPL", tier=1, expected_fill_usd=178.42,
        shares_planned=25,
    )
    row = db.record_user_execution(
        signal_id=sid, user_fill_usd=179.00, user_shares=25, outcome="executed",
    )
    assert row is not None
    assert row["outcome"] == "executed"
    assert row["slippage_pct"] == pytest.approx((179.00 - 178.42) / 178.42, rel=1e-6)


def test_record_user_execution_by_ticker_picks_latest_pending(tmp_path, monkeypatch):
    """When two AAPL alerts are pending, /done AAPL resolves the latest."""
    from ics import config
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    from ics import db
    db.init_db()
    older = db.record_signal_sent(
        ticker="AAPL", tier=1, expected_fill_usd=170.0,
        alert_sent_at="2024-01-01T10:00:00",
    )
    newer = db.record_signal_sent(
        ticker="AAPL", tier=1, expected_fill_usd=180.0,
        alert_sent_at="2024-01-05T10:00:00",
    )
    row = db.record_user_execution(
        ticker="AAPL", user_fill_usd=181.0, outcome="executed",
    )
    assert row is not None
    assert row["id"] == newer
    # Older row still has no outcome
    import sqlite3
    with sqlite3.connect(config.DB_PATH) as c:
        c.row_factory = sqlite3.Row
        r = c.execute("SELECT outcome FROM signals_sent WHERE id = ?", (older,)).fetchone()
        assert r["outcome"] is None


def test_record_user_execution_missing_returns_none(tmp_path, monkeypatch):
    from ics import config
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    from ics import db
    db.init_db()
    assert db.record_user_execution(signal_id=999, user_fill_usd=10.0) is None
    assert db.record_user_execution(ticker="GHOST", user_fill_usd=10.0) is None


def test_missed_outcome_has_no_slippage(tmp_path, monkeypatch):
    """A /missed report shouldn't populate slippage_pct."""
    from ics import config
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    from ics import db
    db.init_db()
    sid = db.record_signal_sent(
        ticker="NVDA", tier=2, expected_fill_usd=500.0,
    )
    row = db.record_user_execution(
        signal_id=sid, outcome="missed", notes="in a meeting",
    )
    assert row is not None
    assert row["outcome"] == "missed"
    assert row["slippage_pct"] is None
    assert row["notes"] == "in a meeting"


def test_get_pending_signals_excludes_resolved(tmp_path, monkeypatch):
    from ics import config
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    from ics import db
    db.init_db()
    a = db.record_signal_sent(ticker="A", tier=1, expected_fill_usd=10.0)
    b = db.record_signal_sent(ticker="B", tier=1, expected_fill_usd=20.0)
    db.record_user_execution(signal_id=a, user_fill_usd=10.05, outcome="executed")
    pending = db.get_pending_signals(within_days=7)
    assert len(pending) == 1
    assert pending[0]["ticker"] == "B"


# ---------------------------------------------------------------------------
# Slippage aggregation
# ---------------------------------------------------------------------------
def test_aggregate_with_no_data_returns_safe_zeros():
    from ics.slippage import _aggregate
    rep = _aggregate([], period_days=30, n_pending=0)
    assert rep.n_alerts == 0
    assert rep.n_executed == 0
    assert rep.execution_rate == 0.0
    assert rep.mean_slippage_pct == 0.0


def test_aggregate_computes_stats_correctly():
    """5 fills with known slippages → known stats."""
    rows = [
        {"id": 1, "ticker": "A", "tier": 1, "outcome": "executed",
         "expected_fill_usd": 100.0, "user_fill_usd": 100.50,
         "user_shares": 10, "shares_planned": 10, "slippage_pct": 0.005},
        {"id": 2, "ticker": "B", "tier": 1, "outcome": "executed",
         "expected_fill_usd": 200.0, "user_fill_usd": 199.00,
         "user_shares": 10, "shares_planned": 10, "slippage_pct": -0.005},
        {"id": 3, "ticker": "C", "tier": 2, "outcome": "executed",
         "expected_fill_usd": 50.0, "user_fill_usd": 50.10,
         "user_shares": 20, "shares_planned": 20, "slippage_pct": 0.002},
        {"id": 4, "ticker": "D", "tier": 2, "outcome": "executed",
         "expected_fill_usd": 80.0, "user_fill_usd": 80.40,
         "user_shares": 10, "shares_planned": 10, "slippage_pct": 0.005},
        {"id": 5, "ticker": "E", "tier": 1, "outcome": "missed",
         "expected_fill_usd": 100.0, "user_fill_usd": None,
         "slippage_pct": None},
    ]
    from ics.slippage import _aggregate
    rep = _aggregate(rows, period_days=30, n_pending=2)
    assert rep.n_alerts == 7  # 5 resolved + 2 pending
    assert rep.n_resolved == 5
    assert rep.n_executed == 4
    assert rep.n_missed == 1
    assert rep.n_pending == 2
    assert rep.execution_rate == pytest.approx(0.8)
    # Mean of [0.005, -0.005, 0.002, 0.005] = 0.00175
    assert rep.mean_slippage_pct == pytest.approx(0.00175)
    # Tier breakdown
    assert rep.by_tier["tier_1"]["n"] == 2
    assert rep.by_tier["tier_2"]["n"] == 2


def test_aggregate_ignores_missed_rows_in_slippage_stats():
    """Missed fills should not pollute the slippage distribution."""
    rows = [
        {"id": 1, "ticker": "A", "tier": 1, "outcome": "executed",
         "expected_fill_usd": 100.0, "user_fill_usd": 100.0,
         "user_shares": 1, "shares_planned": 1, "slippage_pct": 0.0},
        {"id": 2, "ticker": "B", "tier": 1, "outcome": "missed",
         "expected_fill_usd": 100.0, "user_fill_usd": None,
         "slippage_pct": None},
    ]
    from ics.slippage import _aggregate
    rep = _aggregate(rows, period_days=30, n_pending=0)
    assert rep.n_executed == 1
    assert rep.mean_slippage_pct == 0.0
    # The 'missed' row should not have contributed at all
    assert rep.by_tier["tier_1"]["n"] == 1


def test_format_report_grades_verdict_by_median_slippage():
    """Verify the verdict-line thresholds work as documented."""
    from ics.slippage import SlippageReport, format_report

    def _make(median):
        return SlippageReport(
            period_days=30, n_alerts=10, n_resolved=10, n_executed=10,
            n_missed=0, n_pending=0, execution_rate=1.0,
            mean_slippage_pct=median, median_slippage_pct=median,
            p10_slippage_pct=median, p90_slippage_pct=median,
            worst_slippage_pct=median, total_slippage_cost_usd=0.0,
            by_tier={"tier_1": {"n": 10, "mean_slippage_pct": median,
                                "median_slippage_pct": median},
                     "tier_2": {"n": 0}},
            worst_offenders=[],
        )

    assert "frictionless" in format_report(_make(0.0005)).lower()
    assert "normal" in format_report(_make(0.002)).lower()
    assert "sizable" in format_report(_make(0.004)).lower()
    assert "large" in format_report(_make(0.008)).lower()
