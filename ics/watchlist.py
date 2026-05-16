"""
watchlist.py
------------
Builds and refreshes the dynamic universe.

v2.5: Universe candidates default to point-in-time NDX membership rather
than the hand-curated BASE_UNIVERSE (which was 2024-2025 winners and
introduced forward survivorship bias into live trading).  See
config.UNIVERSE_SOURCE.

Pipeline:
    universe source (PIT-NDX, PIT-S&P 500, or BASE_UNIVERSE)
      -> per-ticker filters: price / volume / market cap / 200-SMA /
                             RS / vol-ratio / proximity to 52w high /
                             dollar-volume floor
      -> watchlist.csv
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

import numpy as np
import pandas as pd

from . import config, data
from .indicators import sma
from .logging_utils import get_logger

log = get_logger("ics.watchlist")


def _resolve_base_universe() -> List[str]:
    """
    Resolve the candidate universe per `config.UNIVERSE_SOURCE.mode`.

    Modes:
      "ndx_pit"   — current-date NDX membership via the n100tickers library.
                    Falls back to BASE_UNIVERSE with a WARNING if unavailable.
      "sp500_pit" — current-date S&P 500 membership via sp500_constituents
                    (fja05680/sp500 CSV).  Falls back to BASE_UNIVERSE
                    with a WARNING if unavailable.
      "base"      — the legacy hand-curated BASE_UNIVERSE list.

    Unknown modes log an error and fall back to BASE_UNIVERSE so the
    watchlist refresh keeps running.
    """
    mode = config.UNIVERSE_SOURCE.mode

    if mode == "ndx_pit":
        try:
            from .constituents import get_universe_at, check_library
            if not check_library():
                log.warning(
                    "PIT-NDX library not available; falling back to BASE_UNIVERSE "
                    "for watchlist refresh.  Install with: "
                    "pip install git+https://github.com/jmccarrell/n100tickers.git"
                )
                return list(config.BASE_UNIVERSE)
            today = pd.Timestamp.utcnow().normalize().strftime("%Y-%m-%d")
            tickers = get_universe_at(today, fallback_to_base_universe=False)
            log.info("Using PIT-NDX universe (%d tickers) as of %s", len(tickers), today)
            return list(tickers)
        except Exception as e:
            log.error("PIT-NDX resolution failed (%s); falling back to BASE_UNIVERSE.", e)
            return list(config.BASE_UNIVERSE)

    elif mode == "sp500_pit":
        try:
            from .sp500_constituents import get_universe_at as get_spx
            from .sp500_constituents import check_library as check_spx
            if not check_spx():
                log.warning(
                    "PIT S&P 500 dataset not available; falling back to BASE_UNIVERSE "
                    "for watchlist refresh.  Expected CSV at "
                    "data/sp500_constituents.csv (downloads automatically on first "
                    "use if absent and network is available)."
                )
                return list(config.BASE_UNIVERSE)
            today = pd.Timestamp.utcnow().normalize().strftime("%Y-%m-%d")
            tickers = get_spx(today)
            if not tickers:
                log.warning("PIT S&P 500 returned 0 tickers for %s; "
                            "falling back to BASE_UNIVERSE.", today)
                return list(config.BASE_UNIVERSE)
            log.info("Using PIT S&P 500 universe (%d tickers) as of %s",
                     len(tickers), today)
            return list(tickers)
        except Exception as e:
            log.error("PIT S&P 500 resolution failed (%s); "
                      "falling back to BASE_UNIVERSE.", e)
            return list(config.BASE_UNIVERSE)

    elif mode == "base":
        log.info("Using BASE_UNIVERSE (%d tickers) per UNIVERSE_SOURCE.mode='base'.",
                 len(config.BASE_UNIVERSE))
        return list(config.BASE_UNIVERSE)

    else:
        log.error("Unknown UNIVERSE_SOURCE.mode=%r; falling back to BASE_UNIVERSE.", mode)
        return list(config.BASE_UNIVERSE)


def _filter_one(
    ticker: str,
    df: pd.DataFrame,
    ref_df: pd.DataFrame,
    market_cap: Optional[float],
    f: config.WatchlistFilters,
    min_dollar_volume_usd: float = 0.0,
) -> Optional[dict]:
    if df is None or len(df) < f.history_days_for_filters:
        return None

    df = df.tail(f.history_days_for_filters)
    last_close = float(df["Close"].iloc[-1])
    avg_vol = float(df["Volume"].tail(60).mean())
    high_52w = float(df["Close"].tail(252).max())

    if last_close < f.min_price_usd:
        return None
    if avg_vol < f.min_avg_daily_volume:
        return None
    if market_cap is not None and market_cap < f.min_market_cap_usd:
        return None

    # Dollar-volume floor — guards against thinly-traded names where £30k
    # positions would move the price.  Computed on the trailing 60 bars
    # as median(Close * Volume) to be resistant to single-day spikes.
    if min_dollar_volume_usd > 0:
        recent = df.tail(60)
        if len(recent) < 60:
            return None
        dv = (recent["Close"] * recent["Volume"]).median()
        if not np.isfinite(dv) or dv < min_dollar_volume_usd:
            return None

    sma200 = sma(df["Close"], f.sma_long_period).iloc[-1]
    if f.require_close_above_sma and (np.isnan(sma200) or last_close < sma200):
        return None

    v20 = df["Volume"].tail(f.vol_short_window).mean()
    v60 = df["Volume"].tail(f.vol_long_window).mean()
    if v60 <= 0 or (v20 / v60) < f.vol_short_long_ratio:
        return None

    if last_close < high_52w * (1.0 - f.pct_below_52w_high):
        return None

    n = f.rs_lookback_days
    if len(df) < n + 1 or len(ref_df) < n + 1:
        return None
    p_ret = df["Close"].iloc[-1] / df["Close"].iloc[-n - 1] - 1.0
    aligned_ref = ref_df["Close"].reindex(df.index).ffill()
    r_ret = aligned_ref.iloc[-1] / aligned_ref.iloc[-n - 1] - 1.0
    rs = p_ret - r_ret
    if rs <= 0:
        return None

    return {
        "ticker": ticker,
        "last_close": round(last_close, 4),
        "avg_vol_60d": int(avg_vol),
        "vol_ratio_20_60": round(float(v20 / v60), 3),
        "rs_1m": round(float(rs), 4),
        "pct_below_52w_high": round(float((high_52w - last_close) / high_52w), 4),
        "above_sma200": True,
        "market_cap_usd": int(market_cap) if market_cap else None,
        "refreshed_at": datetime.utcnow().isoformat(timespec="seconds"),
    }


def refresh_watchlist(
    base_universe: Optional[List[str]] = None,
    save_csv: bool = True,
    pause_between_dl: float = 0.0,
) -> pd.DataFrame:
    f = config.WATCHLIST_FILTERS
    universe = base_universe or _resolve_base_universe()
    log.info("Refreshing watchlist over %d candidates...", len(universe))

    end = pd.Timestamp.utcnow().normalize()
    start = end - pd.Timedelta(days=int(f.history_days_for_filters * 1.6))

    ref_ticker = f.rs_reference if f.rs_reference in ("SPY", "QQQ") else "SPY"
    ref_df = data.get_history(ref_ticker, start=str(start.date()), end=str(end.date()))
    if ref_df.empty:
        log.error("Could not load reference index %s — aborting refresh.", ref_ticker)
        return pd.DataFrame()

    price_data = data.download(
        universe, start=str(start.date()), end=str(end.date()), pause=pause_between_dl
    )
    log.info("Fetching market caps (best-effort)...")
    caps = data.get_market_caps(universe)

    dv_floor = config.UNIVERSE_SOURCE.min_dollar_volume_usd
    rows = []
    for t in universe:
        df = price_data.get(t)
        row = _filter_one(t, df, ref_df, caps.get(t), f, min_dollar_volume_usd=dv_floor)
        if row:
            rows.append(row)

    out = pd.DataFrame(rows).sort_values("rs_1m", ascending=False).reset_index(drop=True)
    log.info("Watchlist refreshed: %d / %d passed filters.", len(out), len(universe))

    if save_csv:
        out.to_csv(config.WATCHLIST_CSV, index=False)
        log.info("Saved watchlist -> %s", config.WATCHLIST_CSV)
    return out


def load_watchlist() -> pd.DataFrame:
    if not config.WATCHLIST_CSV.exists():
        log.info("No watchlist CSV found; running refresh.")
        return refresh_watchlist()
    return pd.read_csv(config.WATCHLIST_CSV)


def get_tickers() -> List[str]:
    df = load_watchlist()
    if df.empty:
        return []
    return df["ticker"].tolist()


if __name__ == "__main__":
    df = refresh_watchlist()
    if not df.empty:
        print(df.head(30).to_string(index=False))
