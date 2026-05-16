"""Tests for the variant-comparison helpers."""
import pandas as pd
import pytest

from ics.compare_variants import (
    FEATURES, _per_window_diff, _verdict,
)


def test_known_features_registered():
    assert "vol_targeting" in FEATURES
    assert "mean_reversion" in FEATURES
    for name, spec in FEATURES.items():
        assert "field" in spec
        assert "config_attr" in spec
        assert "description" in spec


def test_per_window_diff_joins_on_window_and_computes_deltas():
    base = pd.DataFrame([
        {"Window": "2020-2021", "OOS_CAGR_%": 10.0, "OOS_MaxDD_%": 5.0,
         "OOS_Sharpe": 0.8, "OOS_PF": 1.5, "OOS_Trades": 30},
        {"Window": "2021-2022", "OOS_CAGR_%": -5.0, "OOS_MaxDD_%": 12.0,
         "OOS_Sharpe": -0.3, "OOS_PF": 0.8, "OOS_Trades": 40},
    ])
    var = pd.DataFrame([
        {"Window": "2020-2021", "OOS_CAGR_%": 12.0, "OOS_MaxDD_%": 4.0,
         "OOS_Sharpe": 1.1, "OOS_PF": 1.7, "OOS_Trades": 28},
        {"Window": "2021-2022", "OOS_CAGR_%": -3.0, "OOS_MaxDD_%": 10.0,
         "OOS_Sharpe": -0.1, "OOS_PF": 0.9, "OOS_Trades": 35},
    ])
    out = _per_window_diff(base, var)
    assert len(out) == 2
    # Sharpe delta on row 0 = 1.1 - 0.8 = 0.3
    row0 = out[out["Window"] == "2020-2021"].iloc[0]
    assert row0["delta_Sharpe"] == pytest.approx(0.3)
    # Calmar derived: base = 10/5 = 2.0, var = 12/4 = 3.0, delta = +1.0
    assert row0["base_Calmar"] == pytest.approx(2.0)
    assert row0["var_Calmar"] == pytest.approx(3.0)
    assert row0["delta_Calmar"] == pytest.approx(1.0)


def test_per_window_diff_handles_zero_drawdown_safely():
    """A window with zero MDD shouldn't crash the calmar derivation."""
    base = pd.DataFrame([
        {"Window": "w1", "OOS_CAGR_%": 5.0, "OOS_MaxDD_%": 0.0,
         "OOS_Sharpe": 1.0, "OOS_PF": 2.0, "OOS_Trades": 10},
    ])
    var = pd.DataFrame([
        {"Window": "w1", "OOS_CAGR_%": 6.0, "OOS_MaxDD_%": 0.0,
         "OOS_Sharpe": 1.1, "OOS_PF": 2.1, "OOS_Trades": 10},
    ])
    out = _per_window_diff(base, var)
    # Calmar = NaN when MDD=0, which makes delta_Calmar = NaN — that's fine
    assert pd.isna(out.iloc[0]["base_Calmar"])
    assert pd.isna(out.iloc[0]["delta_Calmar"])


def test_per_window_diff_empty_inputs_return_empty():
    assert _per_window_diff(pd.DataFrame(), pd.DataFrame()).empty


def test_verdict_passes_when_both_metrics_clear_threshold():
    diff = pd.DataFrame({
        "Window": [f"w{i}" for i in range(8)],
        "delta_Sharpe": [0.3, 0.2, 0.1, 0.4, 0.5, -0.1, 0.0, 0.2],   # 5 strictly > 0
        "delta_Calmar": [1.0, 0.5, 0.3, 0.8, -0.2, 0.4, -0.1, 0.6],  # 6 strictly > 0
    })
    ok, txt = _verdict(diff, n_pass=5)
    assert ok is True
    assert "MERGE" in txt
    assert "✅" in txt


def test_verdict_fails_when_only_one_metric_clears():
    diff = pd.DataFrame({
        "Window": [f"w{i}" for i in range(8)],
        "delta_Sharpe": [0.3, 0.2, -0.1, 0.4, -0.5, -0.1, -0.2, 0.2],  # 4 of 8
        "delta_Calmar": [1.0, 0.5, 0.3, 0.8, 0.2, 0.4, 0.1, 0.6],     # 8 of 8
    })
    ok, txt = _verdict(diff, n_pass=5)
    assert ok is False
    assert "DO NOT MERGE" in txt


def test_verdict_fails_when_neither_metric_clears():
    diff = pd.DataFrame({
        "Window": [f"w{i}" for i in range(8)],
        "delta_Sharpe": [0.3, -0.2, -0.1, 0.4, -0.5, -0.1, -0.2, -0.2],
        "delta_Calmar": [1.0, -0.5, -0.3, 0.8, -0.2, -0.4, -0.1, -0.6],
    })
    ok, txt = _verdict(diff, n_pass=5)
    assert ok is False


def test_verdict_handles_zero_windows_gracefully():
    ok, txt = _verdict(pd.DataFrame(), n_pass=5)
    assert ok is False
    assert "UNDEFINED" in txt
