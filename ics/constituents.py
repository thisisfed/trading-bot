"""
constituents.py
---------------
Point-in-time NASDAQ-100 membership lookup.

Uses the `nasdaq_100_ticker_history` library (jmccarrell/n100tickers on
GitHub).  Install: pip install git+https://github.com/jmccarrell/n100tickers.git

S&P 500 membership lives in `sp500_constituents.py` (different upstream
data source, different time coverage).  Keep them separate; this module
deliberately doesn't try to handle both.

A query for any date returns membership as known on that date.
Point-in-time means we don't peek at the future.
"""
from __future__ import annotations

from typing import List

from . import config
from .logging_utils import get_logger

log = get_logger("ics.constituents")


def check_library() -> bool:
    """Return True iff the n100tickers library is available."""
    try:
        import nasdaq_100_ticker_history  # noqa: F401
        return True
    except ImportError:
        return False


def get_universe_at(date_str: str,
                    fallback_to_base_universe: bool = True) -> List[str]:
    """
    Return NDX membership as of `date_str` (YYYY-MM-DD).

    If `fallback_to_base_universe` is True and the library isn't
    installed, returns config.BASE_UNIVERSE with a warning.  When False,
    raises ImportError so the caller can decide how to handle it.
    """
    if not check_library():
        if fallback_to_base_universe:
            log.warning(
                "n100tickers library not installed; falling back to BASE_UNIVERSE. "
                "Install with: pip install git+https://github.com/jmccarrell/n100tickers.git"
            )
            return list(config.BASE_UNIVERSE)
        raise ImportError(
            "n100tickers library not installed. "
            "pip install git+https://github.com/jmccarrell/n100tickers.git"
        )
    from nasdaq_100_ticker_history import tickers_as_of
    import pandas as pd
    d = pd.to_datetime(date_str).date()
    tickers = tickers_as_of(d.year, d.month, d.day)
    # tickers_as_of returns a set; sort for stability
    return sorted(str(t).strip().upper() for t in tickers if t)
