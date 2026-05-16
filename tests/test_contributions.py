"""Tests for the monthly contribution feature."""
import pandas as pd
import pytest
from pandas.tseries.holiday import USFederalHolidayCalendar
from pandas.tseries.offsets import CustomBusinessDay

from ics import config
from ics.backtest import Backtester, _contribution_dates


def _us_calendar(start, end):
    """Approximate SPY trading calendar — federal holidays + Good Friday."""
    us_bd = CustomBusinessDay(calendar=USFederalHolidayCalendar())
    cal = pd.date_range(start, end, freq=us_bd)
    good_fridays = [
        pd.Timestamp("2023-04-07"),
        pd.Timestamp("2024-03-29"),
        pd.Timestamp("2025-04-18"),
    ]
    return cal[~cal.isin(good_fridays)]


def test_contribution_dates_one_per_month():
    cal = _us_calendar("2024-01-01", "2025-12-31")
    dates = _contribution_dates(cal)
    # 24 months → 24 contributions
    assert len(dates) == 24
    # Each (year, month) appears exactly once
    months = [(d.year, d.month) for d in dates]
    assert len(set(months)) == 24


def test_contribution_dates_good_friday_falls_back_to_thursday():
    cal = _us_calendar("2024-01-01", "2025-12-31")
    dates = _contribution_dates(cal)
    march_2024 = [d for d in dates if d.year == 2024 and d.month == 3]
    assert march_2024 == [pd.Timestamp("2024-03-28")]
    assert march_2024[0].day_name() == "Thursday"


def test_contribution_dates_normal_month_picks_friday():
    cal = _us_calendar("2024-01-01", "2025-12-31")
    dates = _contribution_dates(cal)
    # April 2025 last Friday = 2025-04-25
    apr_2025 = [d for d in dates if d.year == 2025 and d.month == 4]
    assert apr_2025 == [pd.Timestamp("2025-04-25")]
    assert apr_2025[0].day_name() == "Friday"


def test_contribution_dates_empty_calendar():
    assert _contribution_dates(pd.DatetimeIndex([])) == []


def test_contribution_dates_idempotent():
    cal = _us_calendar("2024-01-01", "2024-12-31")
    a = _contribution_dates(cal)
    b = _contribution_dates(cal)
    assert a == b


def test_contribution_dates_unsupported_schedule_raises():
    cal = _us_calendar("2024-01-01", "2024-12-31")
    with pytest.raises(ValueError):
        _contribution_dates(cal, schedule="first_monday")


def test_scaled_risk_params_grows_caps_proportionally():
    """After contributions equal to starting capital, caps double."""
    class _StubBT:
        starting_capital_gbp = 30000.0
        contributions_cfg = config.ContributionsConfig(
            enabled=True, scale_absolute_caps_with_contributions=True
        )
        _base_risk_params = config.RISK_PARAMS

    base = config.RISK_PARAMS
    scaled = Backtester._scaled_risk_params(_StubBT(), 30000.0)
    assert scaled.max_position_gbp_absolute == pytest.approx(base.max_position_gbp_absolute * 2)
    assert scaled.risk_per_trade_gbp_absolute == pytest.approx(base.risk_per_trade_gbp_absolute * 2)
    assert scaled.max_total_invested_gbp_absolute == pytest.approx(base.max_total_invested_gbp_absolute * 2)


def test_scaled_risk_params_zero_contributions_returns_base():
    class _StubBT:
        starting_capital_gbp = 30000.0
        contributions_cfg = config.ContributionsConfig(
            enabled=True, scale_absolute_caps_with_contributions=True
        )
        _base_risk_params = config.RISK_PARAMS

    out = Backtester._scaled_risk_params(_StubBT(), 0.0)
    assert out is config.RISK_PARAMS


def test_scaled_risk_params_disabled_flag_returns_base():
    class _StubBT:
        starting_capital_gbp = 30000.0
        contributions_cfg = config.ContributionsConfig(
            enabled=True, scale_absolute_caps_with_contributions=False
        )
        _base_risk_params = config.RISK_PARAMS

    out = Backtester._scaled_risk_params(_StubBT(), 9000.0)
    assert out is config.RISK_PARAMS


def test_db_contribution_idempotent(tmp_path, monkeypatch):
    """Inserting the same (date, source) twice is a no-op the second time."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    from ics import db
    db.init_db()
    assert db.insert_contribution("2025-01-31", 750.0, source="paper") is True
    assert db.insert_contribution("2025-01-31", 750.0, source="paper") is False
    db.insert_contribution("2025-02-28", 750.0, source="paper")
    assert db.total_contributions(source="paper") == pytest.approx(1500.0)
    rows = db.get_contributions(source="paper")
    assert [r["contribution_date"] for r in rows] == ["2025-01-31", "2025-02-28"]
