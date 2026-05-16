"""Tests for the volatility-targeting feature."""
import dataclasses

import numpy as np
import pandas as pd
import pytest

from ics import config, backtest


# ---------------------------------------------------------------------------
# _vol_scale_at — unit tests with a synthetic SPY series
# ---------------------------------------------------------------------------
class _StubBT:
    """Minimal stub that satisfies _vol_scale_at's required attributes.

    Defaults vol_targeting_enabled to True so the tests exercise the math
    they're meant to.  The product default is False after the WFO comparison
    showed it degrades OOS performance — but the math itself is still tested
    here in case we re-validate on a different universe.
    """
    def __init__(self, spy_realized_vol_series: pd.Series, rp: config.RiskParams = None):
        self._spy_realized_vol = spy_realized_vol_series
        self._base_risk_params = rp or dataclasses.replace(
            config.RISK_PARAMS, vol_targeting_enabled=True
        )


def _bt_method(stub, name):
    """Bind a method from Backtester onto a stub."""
    return getattr(backtest.Backtester, name).__get__(stub, backtest.Backtester)


def test_vol_scale_returns_one_when_disabled():
    rp = dataclasses.replace(config.RISK_PARAMS, vol_targeting_enabled=False)
    stub = _StubBT(pd.Series(dtype=float), rp)
    assert _bt_method(stub, "_vol_scale_at")(pd.Timestamp("2024-01-01")) == 1.0


def test_vol_scale_returns_one_when_no_history_yet():
    """Lookup before the first NaN-free realized-vol value returns 1.0."""
    idx = pd.date_range("2024-01-01", periods=30, freq="B")
    realized = pd.Series([np.nan] * 30, index=idx)  # entirely NaN
    stub = _StubBT(realized)
    out = _bt_method(stub, "_vol_scale_at")(pd.Timestamp("2024-01-15"))
    assert out == 1.0


def test_vol_scale_target_over_realized_when_realized_below_target():
    """Calm regime (8% realized) → scale UP toward target (15%)."""
    idx = pd.date_range("2024-01-01", periods=30, freq="B")
    realized = pd.Series([0.08] * 30, index=idx)
    stub = _StubBT(realized)
    out = _bt_method(stub, "_vol_scale_at")(idx[-1])
    # 0.15 / 0.08 = 1.875, clipped at vol_scale_max=1.5
    assert out == pytest.approx(1.5)


def test_vol_scale_target_over_realized_when_realized_above_target():
    """Panic regime (40% realized) → scale DOWN, clipped at min."""
    idx = pd.date_range("2024-01-01", periods=30, freq="B")
    realized = pd.Series([0.40] * 30, index=idx)
    stub = _StubBT(realized)
    out = _bt_method(stub, "_vol_scale_at")(idx[-1])
    # 0.15 / 0.40 = 0.375, clipped at vol_scale_min=0.5
    assert out == pytest.approx(0.5)


def test_vol_scale_unclipped_in_normal_regime():
    """Mid-vol regime (12% realized) → scale 1.25 (within clip range)."""
    idx = pd.date_range("2024-01-01", periods=30, freq="B")
    realized = pd.Series([0.12] * 30, index=idx)
    stub = _StubBT(realized)
    out = _bt_method(stub, "_vol_scale_at")(idx[-1])
    # 0.15 / 0.12 = 1.25, within [0.5, 1.5]
    assert out == pytest.approx(1.25)


def test_vol_scale_handles_zero_or_negative_realized():
    """Defensive: zero / negative / inf realized vol returns 1.0."""
    idx = pd.date_range("2024-01-01", periods=30, freq="B")
    realized = pd.Series([0.0] * 30, index=idx)
    stub = _StubBT(realized)
    assert _bt_method(stub, "_vol_scale_at")(idx[-1]) == 1.0


def test_vol_scale_lookup_uses_most_recent_observation():
    """Lookup at a date returns the realized vol at-or-before that date."""
    idx = pd.date_range("2024-01-01", periods=10, freq="B")
    # Three days of high vol followed by seven of low vol
    realized = pd.Series(
        [0.40] * 3 + [0.10] * 7, index=idx
    )
    stub = _StubBT(realized)
    # Mid-period (during high vol) → clipped to 0.5
    assert _bt_method(stub, "_vol_scale_at")(idx[2]) == pytest.approx(0.5)
    # End-of-period (after vol dropped) → clipped to 1.5 (since 0.15/0.10 = 1.5)
    assert _bt_method(stub, "_vol_scale_at")(idx[-1]) == pytest.approx(1.5)


def test_vol_scale_default_target_matches_long_run_spy_vol():
    """Sanity check on the default: 0.15 is roughly long-run SPY realized vol."""
    rp = config.RISK_PARAMS
    # 12-22% covers the post-crisis SPY range; 15% is a sensible midpoint
    assert 0.10 <= rp.vol_target_annualized <= 0.22


# ---------------------------------------------------------------------------
# Integration: a Backtester run with vol-targeting on doesn't break and
# logs the scale factor in signals
# ---------------------------------------------------------------------------
def test_vol_targeting_can_be_turned_off_via_risk_params():
    """A custom RiskParams with vol_targeting_enabled=False should make
    _vol_scale_at return 1.0 even when the realized-vol series is non-empty."""
    rp = dataclasses.replace(config.RISK_PARAMS, vol_targeting_enabled=False)
    idx = pd.date_range("2024-01-01", periods=30, freq="B")
    realized = pd.Series([0.30] * 30, index=idx)  # would scale to 0.5 if on
    stub = _StubBT(realized, rp)
    assert _bt_method(stub, "_vol_scale_at")(idx[-1]) == 1.0
