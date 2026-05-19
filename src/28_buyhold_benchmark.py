"""
28_buyhold_benchmark.py

Equal-weight buy-and-hold of point-in-time NASDAQ-100 over the same date
range and contribution schedule as the v3 WFO. The honest answer to:
"is the strategy's edge real, or just leveraged long-NDX beta?"

== Methodology ==
1. Use PIT-NDX membership at 2020-05-19 (start of v3 WFO OOS).
2. Equal-weight initial £30,000 capital across those names at t=0.
3. Each contribution date (last Friday of each month, £750), buy more of
   the same equal-weight basket at then-current prices.
4. No rebalancing. No membership updates. True buy-and-hold.
5. FX (GBP per USD) from yfinance via ics.data — same source the bot uses.
6. Output equity_gbp.csv and contributions_gbp.csv in the same format as
   the bot's report directories, so 26_drawdown_analysis.py and
   27_equity_sharpe_analysis.py can be run on them directly.

== Caveats ==
- This is buy-and-hold of the names that WERE in NDX on 2020-05-19,
  not "the NDX index" itself (which periodically adds/removes names).
  For our purposes — establishing whether the active strategy beats a
  naive long-only allocation to the same universe — this is the fair
  benchmark.
- Tickers with insufficient yfinance history get dropped. Survivorship
  effect is small (~3-5 names typically) and works AGAINST the benchmark
  if anything (removed names tend to be losers).
- FX conversion is daily mark-to-market. The strategy uses the same FX
  series so currency impact is identical.

== Usage ==
    cd ~/Desktop/trading-bot-main           # project root with ics/ inside
    source .venv/bin/activate
    python src/28_buyhold_benchmark.py
    deactivate

After it finishes, run the standard analyses to compare to the strategy:

    python src/26_drawdown_analysis.py data/reports/buyhold_ndx/equity_gbp.csv \\
        --contributions data/reports/buyhold_ndx/contributions_gbp.csv \\
        --label "Buy-and-hold PIT-NDX (strategy only)"

    python src/27_equity_sharpe_analysis.py data/reports/buyhold_ndx/equity_gbp.csv \\
        --contributions data/reports/buyhold_ndx/contributions_gbp.csv \\
        --tests 1 --resample M --label "Buy-and-hold NDX (monthly)"

Note: --tests 1 for the benchmark because buy-and-hold has no parameter
search — there's nothing to multi-test-correct for.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the bot's `ics` package importable regardless of where this script
# is invoked from (e.g. `python src/28_buyhold_benchmark.py` from project root).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

# Use the bot's own modules so the comparison is exactly apples-to-apples.
from ics import data
from ics.constituents import get_universe_at


# ===========================================================================
# Configuration — must match the v3 WFO and the strategy run exactly
# ===========================================================================

START_DATE = "2020-05-19"        # v3 WFO first OOS start
END_DATE = "2025-11-25"          # v3 WFO last OOS end
START_CAPITAL_GBP = 30_000.0     # matches the strategy's starting equity
MONTHLY_CONTRIB_GBP = 750.0      # matches the strategy's contribution schedule
OUTPUT_DIR = Path("data/reports/buyhold_ndx")
MIN_COVERAGE = 0.80              # drop tickers with < 80% price coverage


# ===========================================================================
# Helpers
# ===========================================================================

def _last_fridays_between(start: pd.Timestamp, end: pd.Timestamp) -> pd.DatetimeIndex:
    """Last Friday of every month in [start, end]. Same logic as the
    reconstruction we've used all day for the strategy."""
    months = pd.period_range(start, end, freq="M")
    out = []
    for m in months:
        d = m.to_timestamp(how="end").normalize()
        while d.weekday() != 4:
            d -= pd.Timedelta(days=1)
        if start <= d <= end:
            out.append(d)
    return pd.DatetimeIndex(out)


def _load_prices(tickers: list[str], start: str, end: str) -> pd.DataFrame:
    """Download adjusted close prices for the basket via the bot's
    get_history helper. Returns wide DataFrame indexed by date, columns
    are tickers. Drops tickers with insufficient coverage."""
    rows = {}
    for t in tickers:
        try:
            df = data.get_history(t, start=start, end=end, interval="1d")
            if df is None or df.empty:
                continue
            rows[t] = df["Close"]
        except Exception as e:
            print(f"  skipping {t}: {e}")

    px = pd.DataFrame(rows).sort_index()
    if px.empty:
        raise RuntimeError("No price data loaded — check yfinance / network.")

    # Use the union of all dates, then forward-fill within each series for
    # mark-to-market continuity. Tickers with too little coverage get dropped.
    all_dates = pd.bdate_range(px.index.min(), px.index.max())
    px = px.reindex(all_dates).ffill()
    coverage = px.notna().mean()
    kept = coverage[coverage >= MIN_COVERAGE].index.tolist()
    dropped = sorted(set(px.columns) - set(kept))
    if dropped:
        print(f"  dropped {len(dropped)} ticker(s) with <{MIN_COVERAGE:.0%} "
              f"coverage: {dropped}")
    return px[kept]


def _on_or_before(target: pd.Timestamp, idx: pd.DatetimeIndex) -> pd.Timestamp | None:
    """Return the latest date in idx that is <= target. None if no such date."""
    valid = idx[idx <= target]
    return valid[-1] if len(valid) else None


# ===========================================================================
# Main
# ===========================================================================

def main() -> None:
    print(f"=== Buy-and-hold PIT-NDX benchmark ===")
    print(f"  Date range:     {START_DATE} → {END_DATE}")
    print(f"  Start capital:  £{START_CAPITAL_GBP:,.2f}")
    print(f"  Contribution:   £{MONTHLY_CONTRIB_GBP:.0f}/month (last Friday)")
    print()

    # 1. Get PIT-NDX membership at start date
    tickers = get_universe_at(START_DATE, fallback_to_base_universe=True)
    if not tickers:
        sys.exit("ERROR: no tickers returned from PIT-NDX lookup.")
    print(f"PIT-NDX at {START_DATE}: {len(tickers)} names")

    # 2. Download adjusted close prices for the basket
    print(f"Downloading prices for {len(tickers)} tickers...")
    prices = _load_prices(tickers, START_DATE, END_DATE)
    print(f"Usable price series: {prices.shape[1]} tickers × "
          f"{prices.shape[0]} business days")

    # 3. FX series (GBP per USD — multiply USD by fx to get GBP)
    print("Loading FX series (GBP per USD)...")
    fx = data.get_fx_series(start=START_DATE, end=END_DATE)
    fx = fx.reindex(prices.index, method="ffill").dropna()
    prices = prices.loc[fx.index]

    # 4. Build the contribution schedule (last Friday of each month)
    start_ts = pd.Timestamp(START_DATE)
    end_ts = pd.Timestamp(END_DATE)
    contrib_dates = _last_fridays_between(start_ts, end_ts)

    # Align contribution dates to nearest trading day on or before
    aligned_contribs = []
    for d in contrib_dates:
        td = _on_or_before(d, prices.index)
        if td is not None:
            aligned_contribs.append((td, MONTHLY_CONTRIB_GBP))
    print(f"Contribution events: {len(aligned_contribs)} "
          f"(total £{sum(c for _, c in aligned_contribs):,.0f})")

    # 5. Open the initial position: £30k buys equal-weight basket on day 0
    initial_date = prices.index[0]
    usd_per_gbp = 1.0 / fx.loc[initial_date]
    usd_capital = START_CAPITAL_GBP * usd_per_gbp
    n_tickers = prices.shape[1]
    usd_per_ticker = usd_capital / n_tickers
    shares = (usd_per_ticker / prices.loc[initial_date]).fillna(0.0)
    print(f"Initial buy on {initial_date.date()}: "
          f"£{START_CAPITAL_GBP:,.0f} = ${usd_capital:,.0f} "
          f"= ${usd_per_ticker:,.0f} per name × {n_tickers} names")

    # 6. Walk forward day by day, applying contributions when they hit
    equity_gbp_series = pd.Series(index=prices.index, dtype=float)
    contrib_series = pd.Series(index=prices.index, dtype=float)
    contrib_dict = dict(aligned_contribs)

    for date in prices.index:
        # Apply contribution if it's a contribution date
        if date in contrib_dict and date != initial_date:
            contrib_gbp = contrib_dict[date]
            usd_added = contrib_gbp / fx.loc[date]
            # Use prices that are valid on this date for equal-weight allocation
            valid_today = prices.loc[date].dropna()
            if len(valid_today) > 0:
                per_ticker_usd = usd_added / len(valid_today)
                added_shares = per_ticker_usd / valid_today
                shares.loc[added_shares.index] += added_shares
            contrib_series.loc[date] = contrib_gbp

        # Mark to market
        usd_value = float((shares * prices.loc[date]).fillna(0).sum())
        equity_gbp_series.loc[date] = usd_value * fx.loc[date]

    # 7. Write outputs in the same format as the bot's report directories
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    equity_gbp_series.name = "equity_gbp"
    equity_gbp_series.dropna().to_frame().to_csv(OUTPUT_DIR / "equity_gbp.csv")

    contribs_out = pd.Series(
        {d: amt for d, amt in aligned_contribs},
        name="contribution_gbp",
    )
    contribs_out.to_frame().to_csv(OUTPUT_DIR / "contributions_gbp.csv")

    # 8. Print a quick summary
    start_eq = float(equity_gbp_series.iloc[0])
    end_eq = float(equity_gbp_series.dropna().iloc[-1])
    span_years = (equity_gbp_series.dropna().index[-1]
                  - equity_gbp_series.index[0]).days / 365.25
    total_ret = (end_eq / start_eq - 1) * 100
    cagr = ((end_eq / start_eq) ** (1 / span_years) - 1) * 100
    print()
    print(f"=== Summary ===")
    print(f"  Start equity:    £{start_eq:,.2f}")
    print(f"  End equity:      £{end_eq:,.2f}")
    print(f"  Total return:    {total_ret:+.2f}%")
    print(f"  CAGR:            {cagr:+.2f}%")
    print(f"  Outputs:         {OUTPUT_DIR}/equity_gbp.csv")
    print(f"                   {OUTPUT_DIR}/contributions_gbp.csv")
    print()
    print("Next: run the analyses for the apples-to-apples comparison:")
    print()
    print(f"  python src/26_drawdown_analysis.py {OUTPUT_DIR}/equity_gbp.csv \\")
    print(f"      --contributions {OUTPUT_DIR}/contributions_gbp.csv \\")
    print(f"      --label \"Buy-and-hold PIT-NDX (strategy only)\"")
    print()
    print(f"  python src/27_equity_sharpe_analysis.py {OUTPUT_DIR}/equity_gbp.csv \\")
    print(f"      --contributions {OUTPUT_DIR}/contributions_gbp.csv \\")
    print(f"      --tests 1 --resample M --label \"BH NDX (monthly)\"")


if __name__ == "__main__":
    main()
