# ICS — Internal Convergence Scanner v2.1

Swing-trading bot for US tech / AI / momentum stocks, sized in **GBP** for a
**Robinhood UK ISA** (£30,000 starting capital). Built for a Mac dev box and
Raspberry Pi 5 production with auto-restart, scheduled scans and Telegram
alerts.

This is **v2.1**, a follow-up to the v2.0 hardening that fixed the
"unrealistic-positive-numbers" backtest bug. v2.1 fixes live-engine breakage,
implements a real walk-forward optimiser, adds Telegram command handlers, and
removes a leaked secret.

---

## ⚠️ Security note

`.env` is **never** committed. It contains your Telegram bot token, which is
equivalent to a password. The repo ships `.env.example`; copy it to `.env`
locally and fill in real values. `.gitignore` already excludes `.env`.

If you ever accidentally commit a token, immediately revoke it via
`@BotFather` → `/revoke`, then issue a new one.

---

## What changed in v2.1

| #  | Issue (v2.0)                                                                | Fix (v2.1)                                                                  |
|---:|-----------------------------------------------------------------------------|-----------------------------------------------------------------------------|
| 1  | `.env` containing a live Telegram bot token shipped in the repo.            | Replaced with `.env.example` placeholder. `.gitignore` excludes `.env`.     |
| 2  | `live.py` called `notifier.register_action(...)` which didn't exist.        | Implemented `register_action` in `notifier.py` and full action dispatcher. |
| 3  | `cli.py scan` called `live_mod.run_scan_once()` which didn't exist.         | Implemented `run_scan_once()` in `live.py`.                                |
| 4  | Live engine had no actual scheduler — just `time.sleep(60)` forever.        | Real intraday/daily loop honouring `LIVE_PARAMS` market hours.              |
| 5  | `wfo.py` was rolling-window evaluation labelled as walk-forward optimisation. | Real WFO: grid search on IS, evaluate best on OOS, stitch OOS equity.    |
| 6  | `walkforward.py` referenced `config.data.get_history` (non-existent).       | Deleted (was unused dead code).                                            |
| 7  | Two Dockerfiles (`Dockerfile` + `dockerfile.py`) with different commands.   | One `Dockerfile`, multi-arch (works on x86_64 dev + arm64 Pi).             |
| 8  | `BASE_UNIVERSE` had 4 duplicates (NVDA / TSLA / PLTR / SOUN).               | Deduplicated; defensive `dict.fromkeys` pass at the end.                   |
| 9  | `trailing_stop_atr_mult` was named for ATR but used initial-risk-per-share. | Renamed `trailing_stop_risk_mult` to match the formula.                    |
| 10 | Scoring logic duplicated between `signals.py` and `backtest._signal_at`.    | `backtest._signal_at` now calls the shared `_evaluate_bar`.                |
| 11 | `time.sleep(60)` in live loop blocked SIGTERM for up to 60 s.               | `threading.Event` based sleep; clean SIGTERM/SIGINT handling.              |
| 12 | systemd unit had no hardening, logged to files (mishandled by `journalctl`).| sd-hardened unit, logs to journald.                                        |

---

## Project layout

```
ics-bot/
├── ics/
│   ├── __init__.py
│   ├── config.py             # capital, fees, parameters
│   ├── logging_utils.py
│   ├── data.py               # yfinance + parquet cache, tz-naive
│   ├── indicators.py         # HMA, RSI, ATR, RS, bull-flag detector
│   ├── sizing.py             # risk-based sizing + caps
│   ├── signals.py            # 6-condition convergence + broad market filter
│   ├── backtest.py           # event-driven backtest
│   ├── performance.py        # CAGR, Sharpe, Sortino, MDD, Calmar
│   ├── db.py                 # SQLite persistence
│   ├── montecarlo.py         # parametric & shuffle MC
│   ├── reporter.py           # PNG charts + summary.txt + trades.csv
│   ├── notifier.py           # Telegram (incl. command listener)
│   ├── watchlist.py          # universe filter
│   ├── live.py               # live engine: schedulers + Telegram actions
│   ├── wfo.py                # real walk-forward OPTIMISATION
│   └── cli.py                # `python -m ics.cli ...`
├── tests/
│   └── test_smoke.py         # synthetic-data sanity tests, no network
├── data/                     # auto-created at runtime
├── logs/                     # auto-created at runtime
├── .env.example              # copy to .env and fill in
├── .gitignore
├── requirements.txt
├── Dockerfile
└── ics-bot.service
```

---

## Setup

### macOS / Linux dev

```bash
cd ics-bot
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in TELEGRAM_TOKEN / TELEGRAM_CHAT_ID
pytest -q tests             # smoke tests, no network
```

### Raspberry Pi 5 (production)

```bash
sudo apt update && sudo apt install -y python3.11 python3.11-venv git
git clone <your-repo> ~/ics-bot && cd ~/ics-bot
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

> **Important — clear stale caches/reports** if you're upgrading from v1:
>
> ```bash
> rm -rf data/price_cache data/reports data/ics.db
> ```

### Refresh the watchlist
```bash
python -m ics.cli refresh-watchlist
```

### One-shot full test (refresh + backtest + WFO + MC, with reports)
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

This now does a real grid search on each IS window, picks the best parameter
combo, then runs only that combo on the OOS window. The OOS equity from each
window is stitched into a single continuous curve. If the strategy has a real
edge, this curve grows. If it's curve-fitted noise, this curve goes nowhere.

### Run the live engine (locally)
```bash
python -m ics.cli live
```

### Trigger a one-off scan
```bash
python -m ics.cli scan
```

### Run the smoke tests (no network)
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

- **Tier 1 (4+ conditions met AND breakout/flag present):** pyramid-eligible.
- **Tier 2 (3+ conditions met):** single tranche, no pyramid.

### Position sizing
- 0.75% initial risk per trade (configurable in `config.RISK_PARAMS`)
- 0.5% extra on Tier 1 add at +6% from entry; total ≤ 1.5%
- 20% position size cap of equity
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

The in-sample backtest (2019–today, watchlist universe, £30,000 start) shows
something like **+800% / 35% CAGR / 1.28 Sharpe**. **Do not believe this number.**

The walk-forward run on the same period typically shows **flat to slightly
positive** OOS CAGR with a markedly lower profit factor and most of the
in-sample alpha gone. That's because:

1. `BASE_UNIVERSE` is a hand-picked 2024–25 momentum list, applied back to
   2019 — classic survivorship bias. Stocks that blew up between 2019 and
   2024 aren't in the list.
2. P&L is concentrated in a handful of names (one ticker = ~45% of total
   profit in a sample run). That's a luck signature, not a robust edge.

**Use the WFO output, not the backtest output, when deciding whether to
deploy this with real money.** And consider: even the WFO numbers are still
contaminated by survivorship bias unless you rebuild `BASE_UNIVERSE` from a
point-in-time index constituents file (Russell 1000 or NASDAQ-100 as of each
year), which this codebase does not.

---

## License

Personal use. No warranty. Trade what you can afford to lose.
