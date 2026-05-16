"""
sp500_constituents.py
---------------------
Point-in-time S&P 500 membership for survivorship-bias-free backtesting.

Data source: github.com/fja05680/sp500
A single CSV with daily/weekly snapshots of SPX membership from 1996 onwards.
Dataset originally compiled by Andreas Clenow for "Trading Evolved" and is
maintained as open data.

Why a separate module from constituents.py
-------------------------------------------
constituents.py uses the `nasdaq_100_ticker_history` package (PIT NDX, only
back to 2015).  This module gives you SPX, back to 1996 — covering the
2000 dot-com unwind and the 2008 GFC, neither of which the NDX module sees.

Format of the source CSV
------------------------
    date,tickers
    1996-01-02,"AAPL,MSFT,IBM,..."   # comma-separated ticker list
    1996-01-09,"AAPL,MSFT,IBM,..."
    ...

Snapshots aren't strictly daily — they're weekly-ish in early years and
denser later.  `get_universe_at(date)` finds the most-recent snapshot at
or before `date`, which is point-in-time correct.

Caveats
-------
* Many SPX components from 2000-2010 are delisted / renamed / acquired.
  yfinance will fail to fetch some of them.  The Backtester already
  handles missing tickers gracefully (logs and skips), so this is safe.
* yfinance's own coverage of pre-2010 prices is uneven.  Some bars may
  have NaN volume or split-adjustment artefacts.  Take results with a
  grain of salt.
* The dataset is community-maintained.  Spot-check critical findings
  against multiple sources before drawing strong conclusions.
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import List, Optional

import pandas as pd

from . import config

log = logging.getLogger("ics.sp500")


# ---------------------------------------------------------------------------
# Data location and source
# ---------------------------------------------------------------------------
SOURCE_URL = (
    "https://github.com/fja05680/sp500/raw/refs/heads/master/"
    "S%26P%20500%20Historical%20Components%20%26%20Changes.csv"
)
CACHE_PATH = config.DATA_DIR / "sp500_constituents.csv" \
    if hasattr(config, "DATA_DIR") else Path("data/sp500_constituents.csv")


# In-memory cache so we don't re-parse on every call
_cache: Optional[pd.DataFrame] = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def get_universe_at(
    as_of: str | date | pd.Timestamp,
    *,
    cap: Optional[int] = None,
) -> List[str]:
    """
    Return the list of S&P 500 tickers that were members on `as_of`.

    Uses the most recent snapshot at or before `as_of` (point-in-time correct).

    Parameters
    ----------
    as_of : str | date | pd.Timestamp
        The date for which to retrieve membership.
    cap : int | None
        If given, return only the first N tickers (alphabetical).  Useful
        to keep WFO compute manageable; the SPX has ~500 tickers vs NDX's
        ~100, so without a cap a single window can take 5x longer.

    Returns
    -------
    List[str]  — sorted list of ticker strings
    """
    df = _load_or_download()
    ts = _parse_date(as_of)

    # Find the most recent snapshot at or before `ts`
    valid = df[df.index <= ts]
    if valid.empty:
        # Before earliest snapshot — return empty rather than fail open,
        # because using an empty universe is honest about not having data.
        log.warning("No SPX snapshot available before %s (earliest is %s)",
                    ts.date(), df.index.min().date())
        return []

    snapshot = valid.iloc[-1]
    raw_tickers = str(snapshot["tickers"])
    tickers = sorted(t.strip() for t in raw_tickers.split(",") if t.strip())

    if cap is not None and cap > 0:
        tickers = tickers[:cap]

    log.debug("SPX as of %s: %d tickers (snapshot %s)",
              ts.date(), len(tickers), valid.index[-1].date())
    return tickers


def available_from() -> Optional[date]:
    """Earliest date the dataset covers, or None if data not yet loaded."""
    try:
        df = _load_or_download()
        return df.index.min().date()
    except Exception:
        return None


def check_library() -> bool:
    """Return True if the dataset is loadable."""
    try:
        df = _load_or_download()
        return not df.empty
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------
def _load_or_download() -> pd.DataFrame:
    """
    Load the constituents CSV from the local cache, or download once.

    On first call:
      - Looks for `data/sp500_constituents.csv`.
      - If absent, downloads from SOURCE_URL and caches it.
      - If both fail, raises FileNotFoundError with a helpful hint.

    Subsequent calls are served from the in-memory cache.
    """
    global _cache
    if _cache is not None:
        return _cache

    cache_path = CACHE_PATH
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    if not cache_path.exists():
        log.info("SPX constituents cache not found, downloading from %s ...",
                 SOURCE_URL)
        try:
            import requests
            r = requests.get(SOURCE_URL, timeout=60)
            r.raise_for_status()
            cache_path.write_bytes(r.content)
            log.info("Downloaded SPX constituents (%d bytes) → %s",
                     len(r.content), cache_path)
        except Exception as e:
            raise FileNotFoundError(
                f"Could not load SPX constituents.\n"
                f"Tried: {cache_path} (not present) and download from "
                f"{SOURCE_URL} (failed: {e}).\n\n"
                f"Manual fix:\n"
                f"  1. Download the CSV from the URL above\n"
                f"  2. Save it to {cache_path}\n"
                f"  3. Retry."
            ) from e

    try:
        df = pd.read_csv(cache_path)
    except Exception as e:
        raise ValueError(f"Could not parse {cache_path}: {e}") from e

    # The dataset uses 'date' or 'Date' depending on version
    date_col = "date" if "date" in df.columns else "Date"
    ticker_col = "tickers" if "tickers" in df.columns else "Tickers"
    if date_col not in df.columns or ticker_col not in df.columns:
        raise ValueError(
            f"Unexpected CSV format in {cache_path}; "
            f"expected columns 'date' and 'tickers', got {list(df.columns)}"
        )

    df = df.rename(columns={date_col: "date", ticker_col: "tickers"})
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    _cache = df
    return df


def _parse_date(d: str | date | pd.Timestamp) -> pd.Timestamp:
    if isinstance(d, pd.Timestamp):
        return d
    if isinstance(d, date):
        return pd.Timestamp(d)
    return pd.Timestamp(d)


def reset_cache_for_tests() -> None:
    """Clear the module-level cache (used in tests)."""
    global _cache
    _cache = None
