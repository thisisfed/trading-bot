"""
Tests for the parameter stability analyser.
Pure logic — no backtests, no network.
"""
import pytest

from ics.stability import (
    analyse_combos,
    render_report,
    DISABLED_SENTINELS,
)


# ---------------------------------------------------------------------------
# Synthetic combo generators
# ---------------------------------------------------------------------------
def _combos(*combos: dict) -> list:
    """Just sugar to make tests read more naturally."""
    return list(combos)


# ---------------------------------------------------------------------------
# Strong signal: same value picked in every window
# ---------------------------------------------------------------------------
def test_strong_signal_when_unanimous():
    combos = _combos(
        {"rsi_min": 55.0}, {"rsi_min": 55.0}, {"rsi_min": 55.0},
        {"rsi_min": 55.0}, {"rsi_min": 55.0}, {"rsi_min": 55.0},
        {"rsi_min": 55.0}, {"rsi_min": 55.0},
    )
    grid = {"rsi_min": [50.0, 55.0, 60.0]}
    rep = analyse_combos(combos, grid=grid)
    p = rep.parameters[0]
    assert p.verdict == "STRONG SIGNAL"
    assert p.mode_value == 55.0
    assert p.mode_share == 1.0


def test_strong_signal_at_threshold():
    """75% mode share is the threshold — exactly 75% should still be STRONG."""
    combos = _combos(
        *([{"rsi_min": 55.0}] * 6),
        *([{"rsi_min": 60.0}] * 2),
    )
    grid = {"rsi_min": [50.0, 55.0, 60.0]}
    rep = analyse_combos(combos, grid=grid)
    p = rep.parameters[0]
    assert p.mode_share == 0.75
    assert p.verdict == "STRONG SIGNAL"


# ---------------------------------------------------------------------------
# Noise: spread across the grid
# ---------------------------------------------------------------------------
def test_noise_when_uniformly_spread():
    """If every grid value gets roughly equal share, that's pure noise."""
    combos = _combos(
        {"rsi_min": 50.0}, {"rsi_min": 55.0}, {"rsi_min": 60.0},
        {"rsi_min": 50.0}, {"rsi_min": 55.0}, {"rsi_min": 60.0},
        {"rsi_min": 50.0}, {"rsi_min": 55.0}, {"rsi_min": 60.0},
    )
    grid = {"rsi_min": [50.0, 55.0, 60.0]}
    rep = analyse_combos(combos, grid=grid)
    p = rep.parameters[0]
    assert p.verdict == "NOISE"
    assert p.distinct_values_picked == 3


# ---------------------------------------------------------------------------
# Moderate signal — between thresholds
# ---------------------------------------------------------------------------
def test_moderate_signal_between_thresholds():
    """5/8 = 62.5% mode share → MODERATE (>50%, <75%)."""
    combos = _combos(
        *([{"rsi_min": 55.0}] * 5),
        *([{"rsi_min": 60.0}] * 3),
    )
    grid = {"rsi_min": [50.0, 55.0, 60.0]}
    rep = analyse_combos(combos, grid=grid)
    p = rep.parameters[0]
    assert p.mode_share == 0.625
    assert p.verdict == "MODERATE"


# ---------------------------------------------------------------------------
# Dead-off: optimiser always picks the disabled sentinel
# ---------------------------------------------------------------------------
def test_dead_off_when_filter_always_disabled():
    """vix_max=999 means VIX check off; always picking it means filter is unhelpful."""
    combos = _combos(*([{"vix_max": 999.0}] * 8))
    grid = {"vix_max": [25.0, 999.0]}
    rep = analyse_combos(combos, grid=grid)
    p = rep.parameters[0]
    assert p.verdict == "DEAD-OFF"
    assert "999" in repr(p.mode_value) or p.mode_value == 999.0


def test_dead_off_for_drawdown_filter():
    combos = _combos(*([{"max_spy_drawdown_pct": 0.99}] * 8))
    grid = {"max_spy_drawdown_pct": [0.05, 0.99]}
    rep = analyse_combos(combos, grid=grid)
    assert rep.parameters[0].verdict == "DEAD-OFF"


# ---------------------------------------------------------------------------
# Dead-on: boolean flag always True
# ---------------------------------------------------------------------------
def test_dead_on_when_flag_always_true():
    combos = _combos(*([{"require_weekly_hma_bullish": True}] * 8))
    grid = {"require_weekly_hma_bullish": [True, False]}
    rep = analyse_combos(combos, grid=grid)
    p = rep.parameters[0]
    assert p.verdict == "DEAD-ON"


def test_dead_off_when_flag_always_false():
    """A boolean flag whose disabled sentinel is False, always picked False."""
    combos = _combos(*([{"require_weekly_hma_bullish": False}] * 8))
    grid = {"require_weekly_hma_bullish": [True, False]}
    rep = analyse_combos(combos, grid=grid)
    p = rep.parameters[0]
    assert p.verdict == "DEAD-OFF"


# ---------------------------------------------------------------------------
# Singleton grid: nothing to learn
# ---------------------------------------------------------------------------
def test_singleton_grid_emits_singleton_verdict():
    combos = _combos(*([{"foo": 7}] * 5))
    grid = {"foo": [7]}
    rep = analyse_combos(combos, grid=grid)
    assert rep.parameters[0].verdict == "SINGLETON"


# ---------------------------------------------------------------------------
# Multiple parameters at once
# ---------------------------------------------------------------------------
def test_multiple_parameters_separate_verdicts():
    combos = _combos(
        # rsi_min: unanimous 55 → STRONG
        # vix_max: 4-4 split → NOISE
        # require_weekly_hma_bullish: always True → DEAD-ON
        *([{"rsi_min": 55.0, "vix_max": 25.0,  "require_weekly_hma_bullish": True}] * 4),
        *([{"rsi_min": 55.0, "vix_max": 999.0, "require_weekly_hma_bullish": True}] * 4),
    )
    grid = {
        "rsi_min": [50.0, 55.0],
        "vix_max": [25.0, 999.0],
        "require_weekly_hma_bullish": [True, False],
    }
    rep = analyse_combos(combos, grid=grid)
    by_name = {p.name: p for p in rep.parameters}
    assert by_name["rsi_min"].verdict == "STRONG SIGNAL"
    assert by_name["vix_max"].verdict == "NOISE"
    assert by_name["require_weekly_hma_bullish"].verdict == "DEAD-ON"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------
def test_empty_combos_returns_empty_report():
    rep = analyse_combos([], grid={"x": [1, 2]})
    assert rep.n_windows == 0
    assert rep.parameters == []


def test_render_report_handles_empty_input():
    rep = analyse_combos([], grid={})
    text = render_report(rep)
    assert "No combos" in text


def test_render_report_contains_actionable_summary():
    combos = _combos(
        *([{"rsi_min": 55.0, "vix_max": 999.0}] * 6),
    )
    grid = {"rsi_min": [50.0, 55.0, 60.0], "vix_max": [25.0, 999.0]}
    rep = analyse_combos(combos, grid=grid)
    text = render_report(rep)
    assert "ACTIONABLE SUMMARY" in text
    assert "rsi_min" in text  # STRONG
    assert "vix_max" in text  # DEAD-OFF


# ---------------------------------------------------------------------------
# Float tolerance for sentinel matching
# ---------------------------------------------------------------------------
def test_float_sentinel_tolerance():
    """0.99 should match the sentinel 0.99 even if there's microscopic float noise."""
    combos = _combos(*([{"max_spy_drawdown_pct": 0.99 + 1e-12}] * 6))
    grid = {"max_spy_drawdown_pct": [0.05, 0.99]}
    rep = analyse_combos(combos, grid=grid)
    # Should still detect this as the disabled sentinel
    assert rep.parameters[0].verdict == "DEAD-OFF"


# ---------------------------------------------------------------------------
# Pooled across objectives
# ---------------------------------------------------------------------------
def test_n_objectives_in_report_header():
    combos = _combos(*([{"x": 1}] * 24))
    rep = analyse_combos(combos, grid={"x": [1, 2]}, n_objectives=3)
    text = render_report(rep)
    # 24 picks / 3 objectives = 8 windows per objective; header should reflect
    assert "3 objectives" in text
    assert "8 windows" in text
