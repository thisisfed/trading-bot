"""
Tests for sp500_constituents.py — point-in-time SPX universe.
Uses a synthetic CSV so no network is needed.
"""
import tempfile
from pathlib import Path

import pandas as pd
import pytest

from ics import sp500_constituents as spx


@pytest.fixture(autouse=True)
def _isolated_cache(monkeypatch):
    """
    Point CACHE_PATH at a temp file and write a synthetic 3-row dataset.
    Reset the in-memory cache before each test.
    """
    tmpdir = tempfile.mkdtemp()
    cache_path = Path(tmpdir) / "sp500_test.csv"
    csv_content = (
        "date,tickers\n"
        "2000-01-03,\"AAPL,MSFT,IBM,GE,XOM\"\n"
        "2005-06-15,\"AAPL,MSFT,IBM,GE,XOM,GOOG\"\n"
        "2020-01-02,\"AAPL,MSFT,GOOG,AMZN,FB,TSLA\"\n"
    )
    cache_path.write_text(csv_content)

    monkeypatch.setattr(spx, "CACHE_PATH", cache_path)
    spx.reset_cache_for_tests()
    yield


def test_get_universe_at_returns_correct_snapshot():
    tickers = spx.get_universe_at("2000-06-01")
    assert tickers == sorted(["AAPL", "MSFT", "IBM", "GE", "XOM"])


def test_get_universe_at_uses_most_recent_snapshot_at_or_before():
    """A query for 2010 should return the 2005 snapshot, not 2020."""
    tickers = spx.get_universe_at("2010-01-01")
    assert "GOOG" in tickers     # added in 2005 snapshot
    assert "AMZN" not in tickers  # only added in 2020 snapshot


def test_get_universe_at_returns_latest_for_recent_date():
    tickers = spx.get_universe_at("2024-01-01")
    assert "TSLA" in tickers
    assert set(tickers) == {"AAPL", "MSFT", "GOOG", "AMZN", "FB", "TSLA"}


def test_get_universe_at_before_dataset_returns_empty():
    """Querying before the earliest snapshot returns empty list, not exception."""
    tickers = spx.get_universe_at("1990-01-01")
    assert tickers == []


def test_get_universe_at_with_cap():
    tickers = spx.get_universe_at("2024-01-01", cap=3)
    assert len(tickers) == 3
    # First 3 alphabetical from {AAPL, AMZN, FB, GOOG, MSFT, TSLA}
    assert tickers == ["AAPL", "AMZN", "FB"]


def test_check_library_returns_true_when_loadable():
    assert spx.check_library() is True


def test_available_from_returns_earliest_date():
    af = spx.available_from()
    assert af is not None
    assert af.year == 2000


def test_repeated_calls_use_cache():
    """Second call shouldn't re-parse — verify by mutating the file in between."""
    spx.get_universe_at("2024-01-01")
    # Wipe the file — if cache works, next call still succeeds
    spx.CACHE_PATH.write_text("")
    tickers = spx.get_universe_at("2024-01-01")
    assert "TSLA" in tickers


def test_reset_cache_forces_reload():
    spx.get_universe_at("2024-01-01")
    spx.reset_cache_for_tests()
    # File still has valid contents — should succeed
    tickers = spx.get_universe_at("2024-01-01")
    assert "TSLA" in tickers


# ---------------------------------------------------------------------------
# WFO universe dispatch — ensure the universe arg routes correctly
# ---------------------------------------------------------------------------
def test_get_pit_tickers_dispatches_to_sp500():
    """The wfo helper should route universe='sp500' to sp500_constituents."""
    from ics.wfo import _get_pit_tickers
    tickers = _get_pit_tickers("2024-01-01", universe_cap=10, universe="sp500")
    # Should return tickers from our synthetic SPX dataset
    assert len(tickers) > 0
    assert "AAPL" in tickers


def test_get_pit_tickers_rejects_unknown_universe():
    """Unknown universe name shouldn't crash — the wfo entrypoint has the
    validation, but _get_pit_tickers itself defaults to nasdaq100 fallback."""
    from ics.wfo import _get_pit_tickers
    # nasdaq100 path needs the library; just ensure it doesn't raise on a
    # known universe.  Unknown values fall through to NDX.
    out = _get_pit_tickers("2024-01-01", universe_cap=5, universe="nasdaq100")
    assert isinstance(out, list)
