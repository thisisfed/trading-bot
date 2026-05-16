"""
earnings.py
-----------
Earnings-date awareness for the strategy.  Two purposes:

1. Skip new entries within N trading days of a ticker's earnings call.
   Earnings gaps are a measurable source of stop-out losses on momentum
   names and the existing signal logic doesn't model event risk.

2. Surface "earnings today" warnings in the daily summary / pre-market
   readiness message so the human in the loop knows which open positions
   carry event risk.

Design choices
--------------
- yfinance is the only data source.  No paid API.  Earnings dates are
  scraped from Yahoo's calendar via `Ticker.calendar`.  This means:
  - The data is best-effort.  Yahoo occasionally has the wrong date.
  - For the BACKTESTER, we have NO point-in-time earnings history.
    Yahoo only gives the *next* upcoming earnings, not past dates as they
    were known at the time.  So this blackout is LIVE-ONLY by default.
    In backtests it's disabled, which is honest: we shouldn't pretend to
    have used data we didn't have at the time.
- The cache is keyed by ticker only, not (ticker, fetched_at), because
  we only ever need the *next* earnings date.  Cache TTL is 24h.
- All errors fail open (return None / not in blackout), never closed.
  An earnings-data outage must not block the strategy from trading.

Blackout window
---------------
Default: skip entries within `earnings_blackout_days` trading days BEFORE
earnings (default 5).  No symmetric post-earnings blackout because:
  - The strategy's stop is already at ~2.5% of risk.
  - Post-earnings momentum gaps in our favour are part of the edge.
  - Symmetric blackouts halve the trade count for marginal protection.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

from . import config
from .logging_utils import get_logger

log = get_logger("ics.earnings")

# Cache freshness — Yahoo updates earnings calendars infrequently, daily
# refresh is more than enough for our purposes.
CACHE_TTL_HOURS = 24


# ---------------------------------------------------------------------------
# Data fetcher
# ---------------------------------------------------------------------------
def _fetch_next_earnings_from_yfinance(ticker: str) -> Optional[pd.Timestamp]:
    """
    Hit yfinance for the next earnings date.  Returns None if not found,
    on any error, or if the data looks malformed.

    Yahoo's calendar format has changed several times.  We try the modern
    pandas DataFrame shape AND the older dict shape, then fall back to
    `info.earningsTimestamp` as a third path.  All in a try/except — the
    function MUST NOT raise.
    """
    try:
        import yfinance as yf
    except ImportError:
        log.warning("yfinance not installed; earnings blackout disabled")
        return None

    try:
        tk = yf.Ticker(ticker)
        cal = getattr(tk, "calendar", None)
    except Exception as e:
        log.debug("yfinance error fetching %s calendar: %s", ticker, e)
        return None

    # Modern shape: dict with 'Earnings Date' key holding a list of datetimes
    try:
        if isinstance(cal, dict):
            dates = cal.get("Earnings Date") or cal.get("earningsDate")
            if dates:
                if isinstance(dates, list) and dates:
                    return pd.Timestamp(dates[0]).normalize()
                return pd.Timestamp(dates).normalize()
    except Exception as e:
        log.debug("dict-shape parse failed for %s: %s", ticker, e)

    # Legacy DataFrame shape: index includes 'Earnings Date'
    try:
        if isinstance(cal, pd.DataFrame) and not cal.empty:
            if "Earnings Date" in cal.index:
                val = cal.loc["Earnings Date"].iloc[0]
                return pd.Timestamp(val).normalize()
    except Exception as e:
        log.debug("frame-shape parse failed for %s: %s", ticker, e)

    # Fallback: info.earningsTimestamp (epoch seconds)
    try:
        info = tk.info  # noqa: PLW1505 - we know this can be slow
        ts = info.get("earningsTimestamp") if info else None
        if ts:
            return pd.Timestamp.fromtimestamp(int(ts)).normalize()
    except Exception:
        pass

    return None


# ---------------------------------------------------------------------------
# Cache layer
# ---------------------------------------------------------------------------
def _read_cache(ticker: str) -> Optional[dict]:
    """Return cache row or None if missing.  Schema: ticker, fetched_at, next_earnings_date."""
    try:
        with sqlite3.connect(config.DB_PATH) as c:
            c.row_factory = sqlite3.Row
            row = c.execute(
                "SELECT ticker, fetched_at, next_earnings_date FROM earnings_cache "
                "WHERE ticker = ?", (ticker,)
            ).fetchone()
            return dict(row) if row else None
    except sqlite3.OperationalError:
        return None


def _write_cache(ticker: str, next_earnings: Optional[pd.Timestamp]) -> None:
    """Upsert cache.  Never raises."""
    try:
        with sqlite3.connect(config.DB_PATH) as c:
            c.execute(
                "INSERT INTO earnings_cache (ticker, fetched_at, next_earnings_date) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(ticker) DO UPDATE SET "
                "  fetched_at = excluded.fetched_at, "
                "  next_earnings_date = excluded.next_earnings_date",
                (
                    ticker,
                    datetime.utcnow().isoformat(timespec="seconds"),
                    next_earnings.strftime("%Y-%m-%d") if next_earnings is not None else None,
                ),
            )
    except sqlite3.OperationalError as e:
        log.debug("Could not write earnings cache for %s: %s", ticker, e)


def _cache_is_fresh(row: dict) -> bool:
    """True if the cache row is younger than CACHE_TTL_HOURS."""
    try:
        fetched = datetime.fromisoformat(row["fetched_at"])
    except (KeyError, ValueError, TypeError):
        return False
    return datetime.utcnow() - fetched < timedelta(hours=CACHE_TTL_HOURS)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def get_next_earnings(
    ticker: str,
    *,
    use_cache: bool = True,
    force_refresh: bool = False,
) -> Optional[pd.Timestamp]:
    """
    Return the next earnings date for `ticker` as a normalized Timestamp,
    or None if unknown/error/no upcoming earnings.

    Caching:
      - If the cache row is fresh (< 24h old) and `force_refresh=False`,
        return the cached value (which may be None — a cached "we asked
        Yahoo and got nothing back" is still a useful negative).
      - Otherwise call yfinance and refresh the cache.

    This function NEVER raises.  All failures degrade to None.
    """
    if not ticker:
        return None

    if use_cache and not force_refresh:
        row = _read_cache(ticker)
        if row is not None and _cache_is_fresh(row):
            d = row.get("next_earnings_date")
            return pd.Timestamp(d).normalize() if d else None

    next_date = _fetch_next_earnings_from_yfinance(ticker)
    if use_cache:
        _write_cache(ticker, next_date)
    return next_date


def is_in_earnings_blackout(
    ticker: str,
    now: pd.Timestamp,
    blackout_days: int = 5,
    *,
    use_cache: bool = True,
) -> bool:
    """
    Return True if `ticker` is within `blackout_days` trading days BEFORE
    its next earnings call as of `now`.

    Conservative semantics:
      - Unknown earnings date → return False (fail open).  We don't block
        trades on absence of data.
      - Earnings date in the past → return False (no upcoming event).
      - blackout_days <= 0 → return False (feature disabled per-call).

    Trading-day approximation: we count calendar days, not trading days,
    because we don't carry a market-calendar dependency at this level.
    For a 5-trading-day blackout, use `blackout_days=7` (5 + weekend).
    """
    if blackout_days <= 0:
        return False
    earnings = get_next_earnings(ticker, use_cache=use_cache)
    if earnings is None:
        return False
    now_norm = pd.Timestamp(now).normalize()
    if earnings < now_norm:
        return False
    delta_days = (earnings - now_norm).days
    return delta_days <= blackout_days


def get_blackout_status(tickers: list[str], now: pd.Timestamp,
                        blackout_days: int = 5) -> dict:
    """
    Bulk lookup for the daily summary.  Returns a dict of
    {ticker: days_until_earnings or None}.  Skipped tickers (no data)
    map to None.  Used by the pre-market readiness message to flag
    "earnings today / this week" without re-querying per signal.
    """
    out = {}
    for t in tickers:
        d = get_next_earnings(t)
        if d is None:
            out[t] = None
        else:
            delta = (d - pd.Timestamp(now).normalize()).days
            out[t] = delta if delta >= 0 else None
    return out
