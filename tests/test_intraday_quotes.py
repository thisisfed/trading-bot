"""Tests for the intraday-quote upgrade in v2.5.2."""
from unittest.mock import MagicMock, patch


def _reset_cache():
    from ics import data
    data._quote_cache.clear()


def test_get_quote_returns_last_price():
    _reset_cache()
    from ics import data
    fake_info = {"last_price": 178.42}
    with patch("ics.data.yf.Ticker") as fake_ticker:
        fake_ticker.return_value.fast_info = fake_info
        out = data.get_quote("AAPL")
    assert out == 178.42


def test_get_quote_returns_none_on_exception():
    _reset_cache()
    from ics import data
    with patch("ics.data.yf.Ticker", side_effect=RuntimeError("net down")):
        out = data.get_quote("AAPL")
    assert out is None


def test_get_quote_returns_none_when_all_keys_missing():
    _reset_cache()
    from ics import data
    fake_info = {"market_cap": 1e12}
    with patch("ics.data.yf.Ticker") as fake_ticker:
        fake_ticker.return_value.fast_info = fake_info
        assert data.get_quote("AAPL") is None


def test_get_quote_caches_within_ttl():
    _reset_cache()
    from ics import data
    fake_info = {"last_price": 178.42}
    with patch("ics.data.yf.Ticker") as fake_ticker:
        fake_ticker.return_value.fast_info = fake_info
        a = data.get_quote("AAPL")
        b = data.get_quote("AAPL")
        assert fake_ticker.call_count == 1
        assert a == b == 178.42


def test_get_quote_force_refresh_bypasses_cache():
    _reset_cache()
    from ics import data
    fake_info = {"last_price": 178.42}
    with patch("ics.data.yf.Ticker") as fake_ticker:
        fake_ticker.return_value.fast_info = fake_info
        data.get_quote("AAPL")
        data.get_quote("AAPL", max_age_seconds=0)
        assert fake_ticker.call_count == 2


def test_get_quotes_bulk_skips_failures():
    _reset_cache()
    from ics import data
    def fake_factory(t):
        m = MagicMock()
        if t == "BAD":
            raise RuntimeError("delisted")
        m.fast_info = {"last_price": 100.0 if t == "AAPL" else 200.0}
        return m
    with patch("ics.data.yf.Ticker", side_effect=fake_factory):
        out = data.get_quotes(["AAPL", "BAD", "MSFT"])
    assert "AAPL" in out and out["AAPL"] == 100.0
    assert "MSFT" in out and out["MSFT"] == 200.0
    assert "BAD" not in out
