# ICS — Internal Convergence Scanner

[![tests](https://github.com/thisisfed/trading-bot/actions/workflows/tests.yml/badge.svg)](https://github.com/thisisfed/trading-bot/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![python](https://img.shields.io/badge/python-3.11-blue.svg)

Swing-trading bot for US tech / AI / momentum stocks, sized in **GBP** for a
**Robinhood UK ISA** (£30,000 starting capital). Built for a Mac dev box and
Raspberry Pi 5 production with auto-restart, scheduled scans and Telegram
alerts.

**This is a personal learning project.** It does not place orders automatically;
it sends alerts. See the [Disclaimer](#disclaimer) at the end.

---

## Highlights

- **Event-driven backtester** with deterministic fills and an explicit cost
  model for the Robinhood UK ISA (SEC fee + TAF + FX markup + slippage).
- **Real walk-forward optimisation** — grid search on in-sample windows,
  evaluated on untouched out-of-sample windows, OOS equity curves stitched
  end-to-end.
- **Monte Carlo validation** in both parametric and trade-shuffle variants
  to estimate the luck component of any single backtest path.
- **Regime filter** so signals only fire when broader market structure
  agrees.
- **Live engine** with paper-trading mode, market-hours scheduler, clean
  SIGTERM handling, and a Telegram command listener.
- **Dockerised** and shipped with a hardened systemd unit for Pi deployment.
- **18 test files** covering signals, sizing, slippage, regime, paper
  trader and assorted edge cases.

---

## Security

`.env` is never committed. It holds the Telegram bot token, which functions as
a password. The repo ships `.env.example` as a template — copy it to `.env`
locally and fill in real values. `.gitignore` excludes `.env` and all generated
artefacts (databases, logs, caches).

All credentials are read via `os.getenv(...)`; no secrets live in source.

---

## Project layout

```
trading-bot/
├── ics/
│   ├── config.py              # capital, fees, parameters, env loading
│   ├── data.py                # yfinance + parquet cache
│   ├── indicators.py          # HMA, RSI, ATR, RS, bull-flag detector
│   ├── signals.py             # 6-condition convergence + market filter
│   ├── sizing.py              # risk-based sizing, caps, cooldowns
│   ├── slippage.py            # microstructure cost model
│   ├── regime.py              # broad-market regime filter
│   ├── backtest.py            # event-driven backtest engine
│   ├── wfo.py                 # walk-forward optimiser
│   ├── multi_wfo.py           # WFO across multiple objectives
│   ├── montecarlo.py          # parametric + shuffle MC
│   ├── stability.py           # parameter-stability analysis
│   ├── revalidation.py        # OOS revalidation harness
│   ├── compare.py             # strategy comparison tooling
│   ├── compare_variants.py    # variant sweep reports
│   ├── performance.py         # CAGR, Sharpe, Sortino, MDD, Calmar
│   ├── live.py                # live engine + scheduler
│   ├── paper_trader.py        # paper-trading layer
│   ├── paper_status.py        # paper P&L reporting
│   ├── preflight.py           # pre-launch sanity checks
│   ├── notifier.py            # Telegram alerts + command listener
│   ├── reporter.py            # PNG charts + summary.txt + trades.csv
│   ├── watchlist.py           # universe filter
│   ├── constituents.py        # index constituents loader
│   ├── sp500_constituents.py  # point-in-time S&P 500 history
│   ├── earnings.py            # earnings calendar blackouts
│   ├── db.py                  # SQLite persistence
│   ├── logging_utils.py
│   └── cli.py                 # `python -m ics.cli ...`
├── tests/                     # 18 test files, no network required
├── .github/workflows/         # CI: pytest on push and PR
├── .env.example               # copy to .env and fill in
├── .gitignore
├── requirements.txt
├── Dockerfile
├── ics-bot.service            # systemd unit for Pi deployment
├── DEPLOYMENT.md              # runbook
└── LICENSE
```

`data/`, `logs/` and `wfo_results/` are created at runtime and are gitignored.

---

## Setup

### macOS / Linux dev

```bash
git clone https://github.com/thisisfed/trading-bot.git
cd trading-bot
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in TELEGRAM_TOKEN / TELEGRAM_CHAT_ID
pytest -q tests
```

### Raspberry Pi 5 (production)

```bash
sudo apt update && sudo apt install -y python3.11 python3.11-venv git
git clone https://github.com/thisisfed/trading-bot.git ~/trading-bot
cd ~/trading-bot
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && nano .env

sudo cp ics-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable ics-bot
sudo systemctl start ics-bot
journalctl -u ics-bot -f
```

The systemd unit uses `Restart=on-failure` with a 15s back-off, so a crash
loop won't hammer the network. SIGTERM is handled cleanly.

### Docker (alternative)

```bash
docker build -t ics-bot .
docker run -d --restart unless-stopped \
    --env-file .env \
    -v $(pwd)/data:/app/data \
    -v $(pwd)/logs:/app/logs \
    ics-bot
```

---

## Usage

### Refresh the watchlist
```bash
python -m ics.cli refresh-watchlist
```

### One-shot full test (refresh + backtest + WFO + MC + reports)
```bash
python -m ics.cli fulltest --start 2019-01-01 --name v2_full
```

### Targeted backtest
```bash
python -m ics.cli backtest --from-watchlist --start 2019-01-01 --mc --name v2_bt
```

### Walk-forward optimisation
```bash
python -m ics.cli wfo --from-watchlist --start 2019-01-01 \
    --is-days 504 --oos-days 252 --step-days 252 \
    --objective sharpe --mc --name v2_wfo
```

Real WFO: grid-search the parameter space on each in-sample window, pick the
best combination by the chosen objective, then evaluate only that combination
on the untouched OOS window. The OOS equity curves are stitched into a single
continuous series. If the strategy has a real edge, this curve grows.
If it's curve-fitted noise, it goes nowhere.

### Run the live engine
```bash
python -m ics.cli live
```

### Trigger a one-off scan
```bash
python -m ics.cli scan
```

### Run the test suite (no network)
```bash
pytest -q tests
```

---

## Telegram commands

Once the live engine is running, message the bot:

| Command   | Effect                                                          |
|-----------|-----------------------------------------------------------------|
| `/status` | Uptime, last scan / refresh / summary timestamps, equity        |
| `/ping`   | Liveness check                                                  |
| `/scan`   | Trigger a manual scan now                                       |
| `/refresh`| Refresh the watchlist now                                       |
| `/equity` | Show last-known equity                                          |
| `/help`   | List all commands                                               |

The listener whitelists messages from `TELEGRAM_CHAT_ID`. Anyone else is
ignored and logged.

---

## Strategy summary

Convergence of six binary conditions on daily bars:

1. Close > HMA(55) **and** HMA(55) sloping up
2. Volume > 1.5× 20-day-avg volume
3. RSI(14) ∈ (55, 75)
4. RS vs SPY > 0 (21-day relative-strength outperformance)
5. Bull-flag active **or** breakout confirmed
6. Close > HMA(20) **and** HMA(20) sloping up

Plus a broad-market filter: SPY > HMA(SPY, 55) **and** SPY HMA sloping up.

- **Tier 1** (4+ conditions met AND breakout/flag present): pyramid-eligible.
- **Tier 2** (3+ conditions met): single tranche, no pyramid.

### Position sizing
- 0.75% initial risk per trade (configurable in `config.RISK_PARAMS`)
- 0.5% extra on Tier 1 add at +6% from entry; total ≤ 1.5%
- 20% position-size cap of equity
- Absolute share cap (sanity)
- 5-day cooldown per ticker after any exit

### Exits
- Stop (initial or trailing) — priority
- Target (measured-move from flag, else 3R fallback)

### Costs (Robinhood UK ISA)
- Commission: £0.00
- SEC fee on sells: 0.0027% of USD notional
- TAF on sells: $0.000166 per share, capped $8.30
- FX markup: 0.03% per side
- Slippage: 10 bps per side

---

## A note on backtest results

The in-sample backtest (2019–present, watchlist universe, £30,000 start) can
show something like **+800% / 35% CAGR / 1.28 Sharpe**. **Do not believe this
number.**

A proper walk-forward run on the same period typically shows **flat to
positive** OOS CAGR with a markedly lower profit factor and most of the
in-sample alpha gone. That is because:

1. `BASE_UNIVERSE` is a hand-picked 2024–25 momentum list applied back to
   2019 — classic survivorship bias. Stocks that blew up between 2019 and
   2024 aren't in the list.
2. P&L is concentrated in a handful of names (one ticker = ~45% of total
   profit in a sample run). That is a luck signature, not a robust edge.

**Use the WFO output, not the backtest output, when deciding whether to
deploy this with real money.** And consider: even the WFO numbers are still
contaminated by survivorship bias unless `BASE_UNIVERSE` is rebuilt from a
point-in-time index-constituents source (the codebase supports a NASDAQ-100
point-in-time mode via `sp500_constituents.py` and `constituents.py`).

---

## What I learned building this

- Beautiful backtests are almost always wrong. Walk-forward optimisation
  and Monte Carlo aren't optional; they're the bare minimum.
- Cost modelling matters. Ignoring SEC fees, TAF, FX markup and slippage
  inflates returns into nonsense.
- Survivorship bias is the silent killer. The hardest part of building
  this wasn't the strategy — it was hunting down a point-in-time universe.
- Production-grade reliability is its own discipline. Most of the
  late-stage work was systemd hardening, signal handling, restart logic,
  and watchdogs — not strategy code.

---

## License

MIT — see [LICENSE](LICENSE).

## Disclaimer

This is a personal learning project. **Not financial advice. Not for
production use with real money.** Backtest results are not indicative of
future performance. Trade only what you can afford to lose.
