"""
Tests for the paper trading layer.

Mocks data.get_history so we can drive the simulation through known scenarios:
entry → trail → target hit, entry → stop hit, entry → no exit (stays open).
"""
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

# Use a fresh DB for every test to avoid cross-test contamination
@pytest.fixture(autouse=True)
def _fresh_db(monkeypatch):
    tmpdir = tempfile.mkdtemp()
    db_path = Path(tmpdir) / "test_ics.db"
    from ics import config
    monkeypatch.setattr(config, "DB_PATH", db_path)
    # Reset paper trader singleton between tests
    from ics import paper_trader
    paper_trader.reset_for_tests()
    yield
    # teardown
    paper_trader.reset_for_tests()


def _signal(ticker="AAPL", entry=100.0, stop=95.0, target=115.0, tier=1):
    return {
        "ticker": ticker, "tier": tier,
        "entry_price": entry, "stop_loss": stop, "target_price": target,
        "atr": 2.0, "rsi": 60.0, "rs_score": 1.0,
        "breakout": True, "flag_active": False, "reasons": "test",
    }


def _make_history(prices, start="2025-01-01"):
    """Return an OHLCV DataFrame from a list of close prices."""
    idx = pd.bdate_range(start, periods=len(prices))
    close = np.array(prices, dtype=float)
    return pd.DataFrame({
        "Open": close, "High": close * 1.005, "Low": close * 0.995,
        "Close": close, "Volume": 1_000_000,
    }, index=idx)


# ---------------------------------------------------------------------------
# Basic open / close
# ---------------------------------------------------------------------------
def test_open_position_records_in_db():
    from ics.paper_trader import PaperTrader
    trader = PaperTrader()
    sig = _signal("AAPL", entry=100, stop=95, target=115)

    opened = trader.process_signal(sig, fx_gbp_per_usd=0.79)
    assert opened
    assert "AAPL" in trader.positions
    assert trader.positions["AAPL"].shares > 0


def test_open_position_blocked_when_at_max():
    from ics.paper_trader import PaperTrader
    from ics import config
    cfg = config.PaperTradingConfig(max_open_positions=2)
    trader = PaperTrader(cfg=cfg)
    assert trader.process_signal(_signal("AAPL", 100, 95, 115), 0.79)
    assert trader.process_signal(_signal("MSFT", 200, 190, 230), 0.79)
    assert not trader.process_signal(_signal("GOOG", 150, 140, 170), 0.79)
    assert len(trader.positions) == 2


def test_duplicate_signal_ignored():
    from ics.paper_trader import PaperTrader
    trader = PaperTrader()
    sig = _signal("AAPL", 100, 95, 115)
    assert trader.process_signal(sig, 0.79)
    assert not trader.process_signal(sig, 0.79)


def test_insufficient_cash_blocks_entry():
    from ics.paper_trader import PaperTrader
    from ics import config
    cfg = config.PaperTradingConfig(starting_capital_gbp=100)  # too small
    trader = PaperTrader(cfg=cfg)
    sig = _signal("AAPL", 1000, 990, 1020)  # would cost more than £100
    assert not trader.process_signal(sig, 0.79)


# ---------------------------------------------------------------------------
# Mark-to-market exits
# ---------------------------------------------------------------------------
def test_target_hit_closes_position():
    from ics.paper_trader import PaperTrader
    from ics import data as data_mod

    trader = PaperTrader()
    sig = _signal("AAPL", entry=100, stop=95, target=115)
    trader.process_signal(sig, fx_gbp_per_usd=0.79)

    # Build a price series that hits the target
    history = _make_history([100, 105, 110, 116])  # day 4 high = 116*1.005 > 115

    with patch.object(data_mod, "get_history", return_value=history):
        results = trader.mark_to_market(fx_gbp_per_usd=0.79)

    assert len(results) == 1
    assert results[0]["ticker"] == "AAPL"
    assert results[0]["reason"] == "target"
    assert results[0]["pnl_gbp"] > 0
    assert "AAPL" not in trader.positions


def test_stop_hit_closes_position():
    from ics.paper_trader import PaperTrader
    from ics import data as data_mod

    trader = PaperTrader()
    sig = _signal("AAPL", entry=100, stop=95, target=115)
    trader.process_signal(sig, fx_gbp_per_usd=0.79)

    # Price drops to test stop
    history = _make_history([100, 99, 96, 94])  # day 4 low = 94*0.995 < 95

    with patch.object(data_mod, "get_history", return_value=history):
        results = trader.mark_to_market(fx_gbp_per_usd=0.79)

    assert len(results) == 1
    assert results[0]["reason"] in ("stop", "trailing_stop")
    assert results[0]["pnl_gbp"] < 0


def test_no_exit_when_price_in_range():
    from ics.paper_trader import PaperTrader
    from ics import data as data_mod

    trader = PaperTrader()
    sig = _signal("AAPL", entry=100, stop=95, target=115)
    trader.process_signal(sig, fx_gbp_per_usd=0.79)

    history = _make_history([100, 101, 102, 103])  # mid-range

    with patch.object(data_mod, "get_history", return_value=history):
        results = trader.mark_to_market(fx_gbp_per_usd=0.79)

    assert results == []
    assert "AAPL" in trader.positions


# ---------------------------------------------------------------------------
# Trailing stop
# ---------------------------------------------------------------------------
def test_high_water_updates_on_new_high():
    from ics.paper_trader import PaperTrader
    from ics import data as data_mod

    trader = PaperTrader()
    sig = _signal("AAPL", entry=100, stop=95, target=130)
    trader.process_signal(sig, fx_gbp_per_usd=0.79)
    assert trader.positions["AAPL"].high_water_usd == pytest.approx(100 * 1.0001, rel=0.01)
    # Bar with higher high should bump high_water
    history = _make_history([100, 105, 108, 110])
    with patch.object(data_mod, "get_history", return_value=history):
        trader.mark_to_market(fx_gbp_per_usd=0.79)
    if "AAPL" in trader.positions:
        # Should have updated to roughly the latest high
        assert trader.positions["AAPL"].high_water_usd > 100


# ---------------------------------------------------------------------------
# Equity / summary
# ---------------------------------------------------------------------------
def test_summary_with_no_trades():
    from ics.paper_trader import PaperTrader
    s = PaperTrader().summary()
    assert s["n_closed"] == 0
    assert s["total_pnl_gbp"] == 0.0
    assert s["open_positions"] == 0


def test_summary_after_winning_trade():
    from ics.paper_trader import PaperTrader
    from ics import data as data_mod

    trader = PaperTrader()
    trader.process_signal(_signal("AAPL", 100, 95, 115), fx_gbp_per_usd=0.79)
    history = _make_history([100, 110, 116, 120])
    with patch.object(data_mod, "get_history", return_value=history):
        trader.mark_to_market(fx_gbp_per_usd=0.79)

    s = trader.summary()
    assert s["n_closed"] == 1
    assert s["win_rate_pct"] == 100.0
    assert s["total_pnl_gbp"] > 0


# ---------------------------------------------------------------------------
# Persistence across restart
# ---------------------------------------------------------------------------
def test_open_position_survives_restart():
    from ics.paper_trader import PaperTrader, reset_for_tests
    trader = PaperTrader()
    trader.process_signal(_signal("AAPL", 100, 95, 115), fx_gbp_per_usd=0.79)
    assert "AAPL" in trader.positions
    saved_shares = trader.positions["AAPL"].shares

    # "Restart" — reset singleton and create a new trader (same DB path)
    reset_for_tests()
    trader2 = PaperTrader()
    assert "AAPL" in trader2.positions, "open position must survive restart"
    assert trader2.positions["AAPL"].shares == saved_shares


# ---------------------------------------------------------------------------
# Priority ordering — Tier 1 must consume slots before Tier 2
# ---------------------------------------------------------------------------
def test_priority_sort_signature_tier1_first():
    """The exact sort key used in live.py — Tier 1 should be first after sort."""
    class FakeSig:
        def __init__(self, ticker, tier, score):
            self.ticker = ticker
            self.tier = tier
            self.score = score
    sigs = [
        FakeSig("AAA", 2, 5),
        FakeSig("BBB", 1, 4),
        FakeSig("CCC", 2, 3),
        FakeSig("DDD", 1, 6),
    ]
    sorted_sigs = sorted(sigs, key=lambda s: (s.tier, -s.score))
    order = [s.ticker for s in sorted_sigs]
    # DDD (tier 1, score 6) > BBB (tier 1, score 4) > AAA (tier 2, score 5) > CCC (tier 2, score 3)
    assert order == ["DDD", "BBB", "AAA", "CCC"]


def test_paper_priority_tier1_consumes_slots_first():
    """When max_open=2 and we have 4 signals, the 2 Tier-1 slots must win."""
    from ics.paper_trader import PaperTrader
    from ics import config
    cfg = config.PaperTradingConfig(max_open_positions=2)
    trader = PaperTrader(cfg=cfg)

    # Build sigs in arbitrary scan order; sort the way live.py does
    class FakeSig:
        def __init__(self, ticker, tier, score):
            self.ticker = ticker
            self.tier = tier
            self.score = score
        def to_dict(self):
            return {
                "ticker": self.ticker, "tier": self.tier,
                "entry_price": 100, "stop_loss": 95, "target_price": 115,
                "atr": 2, "rsi": 60, "rs_score": 1,
                "breakout": True, "flag_active": False, "reasons": "test",
            }

    sigs = [
        FakeSig("LOWB", 2, 3),  # weak Tier 2 — would consume a slot if scan-order
        FakeSig("LOWA", 2, 3),  # weak Tier 2
        FakeSig("HIGH", 1, 6),  # strongest
        FakeSig("MID",  1, 4),  # also Tier 1
    ]
    sorted_sigs = sorted(sigs, key=lambda s: (s.tier, -s.score))
    for s in sorted_sigs:
        trader.process_signal(s.to_dict(), fx_gbp_per_usd=0.79)

    # Only the two Tier 1 signals should be open
    assert "HIGH" in trader.positions
    assert "MID"  in trader.positions
    assert "LOWA" not in trader.positions
    assert "LOWB" not in trader.positions


# ---------------------------------------------------------------------------
# Per-scan entry cap and tier breakdown
# ---------------------------------------------------------------------------
def test_paper_config_has_new_caps():
    """Sanity check defaults match the documented values."""
    from ics import config
    cfg = config.PaperTradingConfig()
    assert cfg.max_open_positions == 8
    assert cfg.max_new_entries_per_scan == 3


def test_summary_includes_tier_breakdown_keys():
    """summary() must always include the tier_breakdown structure."""
    from ics.paper_trader import PaperTrader
    trader = PaperTrader()
    s = trader.summary()
    assert "tier_breakdown" in s
    assert "open_tier1" in s
    assert "open_tier2" in s


def test_summary_tier_breakdown_with_real_trades():
    """Open and close trades across both tiers, verify breakdown is correct."""
    from ics.paper_trader import PaperTrader
    from ics import data as data_mod

    trader = PaperTrader()
    # 1 Tier 1 winner
    trader.process_signal(_signal("AAPL", 100, 95, 115, tier=1), 0.79)
    history = _make_history([100, 110, 116, 120])
    with patch.object(data_mod, "get_history", return_value=history):
        trader.mark_to_market(0.79)
    # 1 Tier 2 loser
    trader.process_signal(_signal("MSFT", 200, 190, 230, tier=2), 0.79)
    history = _make_history([200, 195, 188, 185])
    with patch.object(data_mod, "get_history", return_value=history):
        trader.mark_to_market(0.79)

    s = trader.summary()
    tb = s["tier_breakdown"]
    assert tb[1]["n"] == 1
    assert tb[1]["win_rate_pct"] == 100.0
    assert tb[1]["total_pnl_gbp"] > 0
    assert tb[2]["n"] == 1
    assert tb[2]["win_rate_pct"] == 0.0
    assert tb[2]["total_pnl_gbp"] < 0


def test_summary_open_position_tier_counts():
    from ics.paper_trader import PaperTrader
    trader = PaperTrader()
    trader.process_signal(_signal("AAPL", 100, 95, 115, tier=1), 0.79)
    trader.process_signal(_signal("MSFT", 200, 190, 230, tier=1), 0.79)
    trader.process_signal(_signal("GOOG", 150, 142, 170, tier=2), 0.79)
    s = trader.summary()
    assert s["open_tier1"] == 2
    assert s["open_tier2"] == 1
