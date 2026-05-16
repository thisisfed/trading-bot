# ICS Bot — Deployment Runbook

## What you have

A walk-forward-validated swing trading strategy with:
- **OOS Sharpe ~1.0** across three independent objective functions
- **OOS CAGR ~21%** stitched across rolling out-of-sample windows
- **Survivorship-bias-free** universe (point-in-time NASDAQ-100 constituents)
- **Stable parameters** (`atr_stop_mult=1.75` and `vix_max=25` at 100% win rate across windows)

You also have a fully functioning paper-trading layer.

## What you do NOT yet have

- **Confirmation the strategy works on real-time, post-2025 data.** WFO is OOS in a statistical sense but it's still on historical bars. Real fills, real slippage, real FX — those are different.
- **Order routing.** Even in `live` mode, the bot does NOT place orders. It sends Telegram alerts; you click buttons in Robinhood UK manually.
- **Real-money confidence.** This is what paper trading is for.

## The deployment plan

### Phase 1 — Paper trading (60-90 days)

Goal: confirm the live strategy produces results within reasonable distance of WFO expectations.

```bash
# 1. Make sure TRADING_MODE=paper in .env (it's the default)
grep TRADING_MODE .env

# 2. Run the pre-flight checks
python -m ics.cli preflight

# 3. Start the bot under systemd
sudo systemctl restart ics-bot
journalctl -u ics-bot -f
```

The bot will:
- Scan the live NDX universe daily after US close
- Apply the WFO-validated strategy (regime filter + 6-condition score + sizing)
- Open virtual positions on signals at next-day open with realistic fills
- Track stop / target / trailing exits using daily OHLC
- Persist everything to SQLite under `source='paper'`
- Send a Telegram alert per scan with both signals AND paper actions

#### Daily/weekly things to check

Send `/paper` to the bot at any time:

```
📓 PAPER TRADING STATUS

Starting capital:  £30,000.00
Current equity:    £31,247.83
🟢 P&L:           £+1,247.83 (+4.2%)

Cash:              £18,234.55
Open positions:    3

Closed trades:     12
Win rate:          41.7%
Avg trade:         £+103.99
Best:              £+587.20
Worst:             £-234.10
```

Compare against WFO expectations after 60 days:

| Metric | WFO expectation | Paper acceptable | Paper concerning |
|---|---|---|---|
| Win rate | ~37% | 30-45% | <25% or >55% |
| Avg trade | ~£55 | £30-£90 | <£15 or >£150 |
| 60-day P&L | ~+£1,000 | +£0 to +£3,000 | >+£5,000 (suspicious) or <-£2,000 |
| Sharpe (annualised) | ~1.0 | 0.5-1.5 | <0 |

**Why a positive paper P&L could be "concerning":** if it's wildly above the WFO, you've found a bug or the regime is unusually favourable — either way, treat it as suspicious until you understand why.

### Phase 2 — Live (only after paper passes)

Goal: real money, but with the bot still in advisory mode.

```bash
# 1. Switch mode
sed -i 's/TRADING_MODE=paper/TRADING_MODE=live/' .env

# 2. Re-run preflight to confirm everything still works
python -m ics.cli preflight

# 3. Restart
sudo systemctl restart ics-bot
```

In live mode:
- Bot still scans, alerts via Telegram with `🛒 Buy on Amazon`-style buttons
- **YOU place orders in Robinhood UK manually**
- Track each trade in the DB by sending `/log_trade <ticker> <fill_price>` (TODO — not yet implemented)

### Phase 3 — Full automation (out of scope for now)

Robinhood UK does not have a public retail API. To automate execution you'd need a different broker (IBKR, Alpaca, Trading 212 with API access), at which point you should re-evaluate the whole stack.

## Operational

### Logs

```bash
journalctl -u ics-bot -f          # live tail
journalctl -u ics-bot --since "1 hour ago"
journalctl -u ics-bot -p err      # errors only
```

### Telegram commands

| Command | Effect |
|---|---|
| `/status` | Uptime, last scan timestamp, current equity |
| `/paper` | Detailed paper-trading P&L and open positions |
| `/scan` | Trigger a manual scan |
| `/refresh` | Refresh the watchlist (rare; we now use NDX-PIT by default) |
| `/equity` | Quick equity check |
| `/help` | List all commands |

### Stopping the bot

```bash
sudo systemctl stop ics-bot       # clean shutdown
sudo systemctl disable ics-bot    # don't start on boot
```

The bot handles SIGTERM cleanly — open paper positions are saved to DB, scheduler is interrupted between sleeps. Restarts pick up exactly where you left off.

### Resetting paper trading

If you want to wipe paper trading state and start fresh:

```bash
sudo systemctl stop ics-bot
sqlite3 data/ics.db "DELETE FROM trades WHERE source='paper'; \
                     DELETE FROM equity WHERE source='paper'; \
                     DELETE FROM watchlist WHERE snapshot_ts LIKE 'paper_state:%';"
sudo systemctl start ics-bot
```

This will NOT touch live data (when you eventually have any).

## What to monitor closely in the first week of paper trading

1. **Does the bot send a scan alert every weekday after US close?** If not, check `journalctl` for errors. The most likely failures are yfinance rate-limiting or a Telegram outage.

2. **Do the paper open/close prices look sane?** Spot-check 2-3 trades against the actual market prices from that day. Significant divergence means a bug.

3. **Is the cash balance staying positive?** If `/paper` shows £-1,000 cash, sizing is broken.

4. **Are stops and targets firing at the right prices?** If a trade closed at "stop" but the day's low was nowhere near the stop level, something's wrong with the mark-to-market logic.

5. **Does the equity curve look like the WFO equity curve?** Roughly. Deviation of 30% over 60 days is normal; deviation of 200% means investigate.

## When to abandon

- **Paper Sharpe is negative for 90 days.** Something is structurally different between WFO and live conditions. Don't go live; investigate why.
- **Win rate is below 25% or above 55% for 60 days.** Either the entry logic is broken or the universe has shifted. Investigate.
- **More than three "look-ahead" or sizing bugs surface.** The codebase needs another pass before being trusted with real money.

## Final note

You've taken this from "bot with phantom edge" to "validated strategy with paper-trading harness" in a week. That's good work. The paper-trading phase is where the rubber meets the road — and where you'll either get conviction in the strategy, or learn (cheaply) that something doesn't generalise to real-time data.

Don't skip it.
