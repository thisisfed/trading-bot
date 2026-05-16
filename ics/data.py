"""
data.py
-------
Thin wrapper around yfinance with on-disk parquet caching.

v2 fixes:
- Hardened tz-naive coercion everywhere (avoids WFO crash when comparing
  cached vs requested ranges)
- Returns tz-naive Series/DataFrames consistently
- Falls back to cached data if yfinance fails
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, Optional

import pandas as pd
import yfinance as yf

from . import config
from .logging_utils import get_logger

log = get_logger("ics.data")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _cache_path(ticker: str, interval: str) -> Path:
    safe = ticker.replace("/", "_").replace("=", "_").replace("^", "_")
    return config.PRICE_CACHE_DIR / f"{safe}__{interval}.parquet"


def _to_naive_index(df: pd.DataFrame) -> pd.DataFrame:
    """Force tz-naive index — single point of truth."""
    if df is None or df.empty:
        return df
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    return df


def _normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    """Standard OHLCV columns, tz-naive index, sorted, no duplicates."""
    if df is None or df.empty:
        return pd.DataFrame()

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    cols = {str(c).lower(): c for c in df.columns}
    rename = {}
    for want in ("open", "high", "low", "close", "adj close", "volume"):
        if want in cols:
            rename[cols[want]] = want.title() if want != "adj close" else "Adj Close"
    df = df.rename(columns=rename)

    keep = [c for c in ("Open", "High", "Low", "Close", "Adj Close", "Volume") if c in df.columns]
    df = df[keep].copy()
    df = _to_naive_index(df)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


def _load_cache(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        df = pd.read_parquet(path)
        return _to_naive_index(df)
    except Exception as e:
        log.warning("Failed reading cache %s: %s", path, e)
        return pd.DataFrame()


def _save_cache(df: pd.DataFrame, path: Path) -> None:
    try:
        df.to_parquet(path)
    except Exception as e:
        log.warning("Failed writing cache %s: %s", path, e)


def _to_naive_ts(ts) -> pd.Timestamp:
    t = pd.Timestamp(ts) if not isinstance(ts, pd.Timestamp) else ts
    if t.tz is not None:
        t = t.tz_localize(None)
    return t


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def get_history(
    ticker: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    interval: str = "1d",
    force_refresh: bool = False,
) -> pd.DataFrame:
    """Get OHLCV for a single ticker. Always tz-naive output."""
    cache = _cache_path(ticker, interval)
    cached = pd.DataFrame() if force_refresh else _load_cache(cache)

    end_dt = _to_naive_ts(end) if end else _to_naive_ts(pd.Timestamp.utcnow().normalize())
    start_dt = _to_naive_ts(start) if start else end_dt - pd.Timedelta(days=365 * 5)

    need_download = (
        cached.empty
        or cached.index.min() > start_dt + pd.Timedelta(days=2)
        or cached.index.max() < end_dt - pd.Timedelta(days=2)
    )

    if need_download:
        log.debug("Downloading %s [%s..%s] interval=%s",
                  ticker, start_dt.date(), end_dt.date(), interval)
        try:
            raw = yf.download(
                ticker,
                start=start_dt - pd.Timedelta(days=5),
                end=end_dt + pd.Timedelta(days=1),
                interval=interval,
                auto_adjust=False,
                progress=False,
                threads=False,
            )
        except Exception as e:
            log.error("yfinance download failed for %s: %s", ticker, e)
            return cached
        raw = _normalize_df(raw)
        if not raw.empty:
            merged = pd.concat([cached, raw]) if not cached.empty else raw
            merged = merged[~merged.index.duplicated(keep="last")].sort_index()
            _save_cache(merged, cache)
            cached = merged

    if cached.empty:
        return cached
    cached = _to_naive_index(cached)
    return cached.loc[(cached.index >= start_dt) & (cached.index <= end_dt)].copy()


def download(
    tickers: Iterable[str],
    start: Optional[str] = None,
    end: Optional[str] = None,
    interval: str = "1d",
    force_refresh: bool = False,
    pause: float = 0.0,
) -> Dict[str, pd.DataFrame]:
    """Bulk download. Sequential — Pi-friendly. Returns dict ticker -> df."""
    out: Dict[str, pd.DataFrame] = {}
    tickers = list(tickers)
    for i, t in enumerate(tickers, 1):
        df = get_history(t, start=start, end=end, interval=interval,
                         force_refresh=force_refresh)
        if not df.empty:
            out[t] = df
        if pause > 0 and i < len(tickers):
            time.sleep(pause)
    log.info("Downloaded %d/%d tickers (interval=%s)", len(out), len(tickers), interval)
    return out


def get_fx_series(start: Optional[str] = None, end: Optional[str] = None) -> pd.Series:
    """
    Series of GBP per USD. yfinance GBPUSD=X gives USD-per-GBP, so we invert.
    Result: USD * series -> GBP.
    """
    df = get_history(config.FX_TICKER, start=start, end=end, interval="1d")
    if df.empty:
        log.warning("FX series empty; falling back to 0.79 GBP/USD constant")
        idx = pd.bdate_range(start or "2018-01-01",
                             end or pd.Timestamp.utcnow().normalize())
        return pd.Series(0.79, index=idx, name="gbp_per_usd")
    usd_per_gbp = df["Close"]
    return (1.0 / usd_per_gbp).rename("gbp_per_usd")


def get_market_caps(tickers: Iterable[str]) -> Dict[str, float]:
    """Best-effort market cap lookup via yfinance .fast_info."""
    caps: Dict[str, float] = {}
    for t in tickers:
        try:
            info = yf.Ticker(t).fast_info
            mc = info.get("market_cap") if hasattr(info, "get") else getattr(info, "market_cap", None)
            if mc and mc > 0:
                caps[t] = float(mc)
        except Exception as e:
            log.debug("market_cap lookup failed for %s: %s", t, e)
    return caps


# ---------------------------------------------------------------------------
# Intraday quotes
#
# `fast_info` returns the most recent traded price for a ticker.  For
# free yfinance users this is delayed by ~15 minutes — fine for a
# daily-bar strategy with manual execution, and a big upgrade over
# reading the previous day's Close while you're sitting in front of a
# Telegram on Tuesday afternoon.
#
# Cache TTL is 60 seconds so repeated /equity calls don't hammer the API.
# Cache lives in process memory; restarts clear it.  Fail-soft: any
# error returns None so the caller can fall back to the daily Close.
# ---------------------------------------------------------------------------
_quote_cache: Dict[str, tuple[float, datetime]] = {}
_QUOTE_TTL_SECONDS = 60.0


def get_quote(ticker: str, max_age_seconds: Optional[float] = None) -> Optional[float]:
    """Return the most recent traded price for `ticker`, or None on failure.

    Cached in-process for `max_age_seconds` (default 60s) to avoid
    hammering yfinance when /equity is called repeatedly.  Set
    max_age_seconds=0 to force a fresh fetch.
    """
    ttl = _QUOTE_TTL_SECONDS if max_age_seconds is None else float(max_age_seconds)
    now = datetime.utcnow()
    cached = _quote_cache.get(ticker)
    if cached is not None and ttl > 0:
        price, fetched_at = cached
        if (now - fetched_at).total_seconds() < ttl:
            return price
    try:
        info = yf.Ticker(ticker).fast_info
        # fast_info exposes last_price across versions; some older builds
        # used .last_trade_price.  Try a few keys before giving up.
        price = None
        for key in ("last_price", "lastPrice", "last_trade_price",
                    "regular_market_price", "regularMarketPrice"):
            try:
                v = info[key] if hasattr(info, "__getitem__") else getattr(info, key, None)
            except Exception:
                v = None
            if v is not None and float(v) > 0:
                price = float(v)
                break
        if price is None:
            return None
        _quote_cache[ticker] = (price, now)
        return price
    except Exception as e:
        log.debug("get_quote failed for %s: %s", ticker, e)
        return None


def get_quotes(tickers: Iterable[str], max_age_seconds: Optional[float] = None
               ) -> Dict[str, float]:
    """Bulk quote lookup. Returns a dict mapping ticker -> price (only
    successful lookups are included)."""
    out: Dict[str, float] = {}
    for t in tickers:
        q = get_quote(t, max_age_seconds=max_age_seconds)
        if q is not None:
            out[t] = q
    return out
