# ICS — Swing Trading Bot on PIT-NASDAQ-100

A long-only swing trading bot for the point-in-time NASDAQ-100 universe. Generates entry signals on a daily-bar scan, sizes positions by ATR, manages exits via tiered R:R targets, and notifies via Telegram. Designed to be run in paper mode for validation before any live deployment.

**Status:** Paper trading. ICS v2.0 (May 2026).

---

## What the strategy does

Each daily scan, the bot:

1. Refreshes the watchlist from current PIT-NASDAQ-100 membership (via the [n100tickers](https://github.com/jmccarrell/n100tickers) library), filtered for liquidity, market structure, and 200-SMA trend.
2. For each watchlist name, computes HMA bias, VWAP positioning, Bollinger Band location, and an RSI threshold check. Names with a valid bullish setup generate signals, scored by tier.
3. Sized by ATR so per-trade risk is constant across the portfolio. Stops are set 1.75 ATR below entry.
4. Exits via tiered R:R targets (partial profits at 1.5R, 2.5R, with a runner up to 4R+) or stop-loss.
5. Sends entry/exit/status to Telegram and records all activity to a local SQLite DB.

The signal layer (HMA + VWAP + Bollinger + RSI) by itself produces near-zero per-trade edge. The edge lives in the *wrapper* — ATR sizing, asymmetric R:R, compounding, and the implicit regime filtering provided by the trend-following entry logic.

---

## v2.0 changes

The v2.0 release is the result of a methodology audit that stripped out overfitting and validated what's actually generating returns:

| Change | Reason |
|---|---|
| **All three regime filters disabled** (SPY 200-SMA, VIX, SPY-drawdown) | Walk-forward testing showed the explicit regime filters are redundant with the trend-following entry logic. Disabling them improved monthly haircut Sharpe from +1.03 to +1.74. |
| **52-week-high watchlist filter relaxed to no-op** | The WFO universe (raw PIT-NDX membership) didn't apply this filter; live did. Aligning live to validated universe. |
| **WFO grid simplified from 72 → 18 combos** | Smaller grid means smaller Bonferroni haircut and less overfitting surface. |
| **marketCap fetcher fixed** | yfinance attribute name changed; lookup had been silently returning `None`. Cosmetic — the dependent filter is redundant on NDX anyway. |

---

## Performance (v3 WFO OOS, strategy-only, monthly resampled)

5.5 years of out-of-sample data (May 2020 – Nov 2025), £30k starting capital, £750/month contributions subtracted from equity to isolate strategy P&L.

| Metric | ICS v2.0 | Equal-weight BH-NDX | Alpha |
|---|---|---|---|
| CAGR | +29.7% | +25.1% | **+4.6%** |
| Max drawdown | **-11.2%** | -21.9% | -10.7 pts |
| Monthly Sharpe (Bonferroni haircut @ 18 tests) | **+1.74** | +1.20¹ | **+0.54** |
| Calmar | 4.20 | 1.16 | +3.04 |
| Sortino | 4.29 | 1.05 | +3.24 |
| 2022 return (NDX bear) | **+37.3%** | -12.7% | +50 pts |
| 2025 return (NDX rally) | +32.3% | +85.5% | -53 pts |

¹ Benchmark has no parameter search, so raw is the right comparison.

**Six independent biases removed during validation:** survivorship (PIT-NDX universe), capital contributions, returns autocorrelation, parameter multi-testing (Bonferroni 18-combo grid), three explicit regime filters, one watchlist filter. Plus benchmarked against equal-weight buy-and-hold of the same universe.

---

## What this strategy is — and isn't

**It is:** a long-only swing strategy that earns ~+0.5 risk-adjusted alpha and ~+5% CAGR over passive buy-and-hold of the same universe, primarily through bear-market defensiveness and drawdown limitation. Strategy delivered +37% during 2022 (NDX -33%) and survived with -11% max DD vs benchmark -22%.

**It isn't:** a strategy that beats passive indexing every year. In strong bull years (2025: BH +85%, strategy +32%), the strategy gives back significant upside in exchange for defensive smoothness. The 2025 underperformance is the cost of the 2022 outperformance — a real trade-off, not a flaw.

**It depends on:** the HMA trend-following entry logic continuing to act as an implicit regime filter. The strategy has no explicit VIX or market-state gate; bear-market protection comes from HMA not firing bullish on falling names. If a future regime breaks this implicit filtering (e.g. choppy markets with frequent false HMA flips), the alpha mechanism could weaken.

---

## Project layout

```
ics/
├── cli.py              entry point: backtest, wfo, live, scan, refresh-watchlist
├── config.py           strategy, regime filter, contribution, and live params
├── backtest.py         single-spec backtest engine
├── wfo.py              walk-forward optimisation (18-combo grid)
├── signals.py          HMA/VWAP/Bollinger/RSI signal generation
├── indicators.py       technical indicator implementations
├── data.py             yfinance loaders, FX, market caps
├── watchlist.py        liquidity/structure filters; produces watchlist.csv
├── constituents.py     PIT-NDX membership accessor (via n100tickers)
├── sp500_constituents.py  PIT-S&P 500 (alternate universe, untested in v2.0)
├── regime.py           SPY-SMA / VIX / SPY-DD filter checks (all disabled in v2.0)
├── live.py             intraday/daily scan loop + Telegram handlers
├── paper_trader.py     paper-mode position management
├── notifier.py         Telegram integration
├── reporter.py         writes equity_gbp.csv / trades.csv / plots
├── montecarlo.py       MC bootstrap on trade list
└── db.py               SQLite layer
src/
├── 25_sharpe_analysis.py    per-trade Sharpe + Harvey-Liu haircut
├── 26_drawdown_analysis.py  drawdown metrics from equity_gbp.csv
├── 27_equity_sharpe_analysis.py  equity-level Sharpe with autocorr resample
└── 28_buyhold_benchmark.py  equal-weight PIT-NDX buy-and-hold comparison
tests/                  pytest suite covering core engine, regime, paper trader, slippage
ics-bot.service         systemd unit for Pi deployment
DEPLOYMENT.md           Pi 5 / Docker deployment runbook
```

---

## Setup

Requirements: Python 3.11+, a Telegram bot token, a yfinance-accessible network.

```bash
git clone https://github.com/<you>/ics.git
cd ics
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install git+https://github.com/jmccarrell/n100tickers.git   # PIT-NDX
cp .env.example .env
# Edit .env: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, TRADING_MODE=paper, TZ
```

---

## Usage

All CLI commands run from the project root:

```bash
# Refresh the watchlist (PIT-NDX → liquidity-filtered universe)
python -m ics.cli refresh-watchlist

# One-shot backtest with default params on the current watchlist
python -m ics.cli backtest --from-watchlist --start 2019-01-01 --name my_test

# Walk-forward OOS validation (5-10 min for the 18-combo grid)
python -m ics.cli wfo --start 2019-01-01 \
    --is-days 504 --oos-days 252 --step-days 252 \
    --objective sharpe --mc --name my_wfo

# Run a single live scan (one-shot, no loop)
python -m ics.cli scan

# Start the live engine (typically run under systemd; honours TRADING_MODE)
python -m ics.cli live
```

Reports land in `data/reports/<name>/` with `equity_gbp.csv`, `trades.csv`, `summary.txt`, and equity/drawdown plots.

---

## Analysis tooling

Four standalone scripts under `src/` evaluate the strategy against academic thresholds:

```bash
# Per-trade Sharpe with Harvey-Liu Bonferroni haircut
python src/25_sharpe_analysis.py data/reports/my_wfo/trades.csv --tests 18

# Drawdown and risk-adjusted metrics from the equity curve
python src/26_drawdown_analysis.py data/reports/my_wfo/equity_gbp.csv \
    --contributions data/reports/my_wfo/contributions_gbp.csv

# Equity-level Sharpe with optional monthly resample to dampen autocorrelation
python src/27_equity_sharpe_analysis.py data/reports/my_wfo/equity_gbp.csv \
    --contributions data/reports/my_wfo/contributions_gbp.csv \
    --tests 18 --resample M

# Equal-weight buy-and-hold benchmark on the same universe / dates / contributions
python src/28_buyhold_benchmark.py
python src/27_equity_sharpe_analysis.py data/reports/buyhold_ndx/equity_gbp.csv \
    --contributions data/reports/buyhold_ndx/contributions_gbp.csv \
    --tests 1 --resample M
```

The `--tests` flag applies the Harvey-Liu (2015) Bonferroni-style haircut for parameter-search multi-testing. The current WFO grid has 18 combinations; for a single pre-specified backtest use `--tests 1`.

---

## Telegram commands

The live bot accepts these commands from the configured chat:

| Command | Action |
|---|---|
| `/status` | Bot health, last scan time, next scan, scan mode |
| `/paper` | Current paper portfolio: equity, P&L, open positions, win rate |
| `/scan` | Trigger a manual scan immediately |
| `/refresh` | Force a watchlist refresh |
| `/regime` | Current market-regime view (informational; filters disabled in v2.0) |
| `/equity` | Equity curve snapshot |
| `/help` | List commands |

`/paper` is read-only — it does not open trades. The bot opens paper positions automatically on its scan schedule (see `LIVE_PARAMS.scan_mode`).

---

## Deployment workflow

The honest deployment path:

1. **Run paper mode for 3-6 months.** `TRADING_MODE=paper` in `.env`. The bot scans on schedule and records virtual trades to the DB.
2. **Track live equity Sharpe weekly** via `/paper` and the `26_/27_` analysis scripts on the live `equity_gbp.csv`.
3. **First gate (3 months):** if live equity Sharpe ≥ 0.5, continue. If < 0, pause and investigate. The expected paper-to-live haircut is 30-50% — a 1.74 backtest Sharpe might land 0.8-1.2 live.
4. **Second gate (6 months):** if live Sharpe ≥ 0.8 *and* max DD ≤ 10%, consider switching to `TRADING_MODE=live` at 25-50% target position size.
5. **Full size (12 months):** if both metrics hold at small size, scale to full size.

The systemd unit at `ics-bot.service` runs the bot under a non-root user; logs via `journalctl -u ics-bot.service`. See `DEPLOYMENT.md` for the Pi 5 setup details.

---

## Caveats and known limitations

- **Long-only.** No short component, no portfolio hedging. In a sustained bear market lasting >12 months (e.g. 2000-2002), the strategy has not been forward-tested.
- **Universe-specific.** Validated on NASDAQ-100. Performance on S&P 500 or other indices is untested in v2.0 — different universe likely produces different results.
- **Concurrent position correlation.** The bot can hold multiple correlated tech names simultaneously. A sector cap or vol-target would reduce this; both are on the v3.0 candidate list.
- **No fee/slippage modelling in the backtest.** Real-world execution will incur ~10-20 bps per round-trip in fees and another 5-15 bps in slippage. Already mostly factored into the live Sharpe expectation.
- **2025 underperformance is real.** Equal-weight buy-and-hold returned +85% in 2025; the strategy returned +32%. This will repeat in any strong narrow bull market. The strategy earns its alpha in bear and choppy regimes — be prepared for stretches of relative underperformance.

---

## Roadmap (post-paper-validation)

If 6 months of paper trading confirms the live Sharpe holds ≥ 0.8, the v3.0 candidate list is:

1. **Volatility targeting** — scale total portfolio exposure to target 15% annualised vol. Highest-EV legitimate Sharpe improvement.
2. **Sector concentration limits** — cap N positions per GICS sector to reduce concurrent correlation.
3. **Earnings filter** — exclude entries within 5 trading days of scheduled earnings.
4. **Mean-reversion overlay** — a second, low-correlation strategy on the same universe for combined-Sharpe diversification.

None of these are committed; each requires the same validation rigour as v2.0 before adoption.

---

## License

[Your license here — MIT, BSD, etc.]

---

## Acknowledgements

PIT-NDX membership via [n100tickers](https://github.com/jmccarrell/n100tickers) by Jason McCarrell. yfinance for price data. Methodology drawing on Harvey & Liu (2015) "Backtesting" for the Bonferroni haircut framework.
