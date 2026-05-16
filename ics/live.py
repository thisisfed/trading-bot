"""
live.py
-------
Live scanning engine.

Two scan modes (set via config.LIVE_PARAMS.scan_mode):

  * "daily"     — one scan per day, after US market close (default).  Best on
                  a Pi with intermittent network.
  * "intraday"  — scan every `scan_interval_minutes` while US market is open.

Both modes also:
  - Refresh the watchlist on startup (and once per day if enabled in config)
  - Send a Telegram heartbeat with /status, /ping, /help, /scan, /refresh handlers
  - Persist signals + an equity stub to the SQLite DB
  - Handle SIGTERM / SIGINT cleanly (systemd-friendly)

`run_scan_once()` is a thin synchronous wrapper that the CLI uses for the
`ics scan` subcommand (one shot, no loop).
"""
from __future__ import annotations

import signal
import threading
import time
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import List, Optional

import pandas as pd

from . import config, db, notifier, signals
from .data import get_fx_series
from .logging_utils import get_logger
from .sizing import compute_position
from .watchlist import refresh_watchlist

log = get_logger("ics.live")

# ---------------------------------------------------------------------------
# Shutdown plumbing
# ---------------------------------------------------------------------------
_shutdown = threading.Event()
_last_scan_at: Optional[datetime] = None
_last_refresh_at: Optional[datetime] = None
_last_summary_at: Optional[datetime] = None
_last_premarket_at: Optional[datetime] = None
# Last seen regime state — used to detect transitions and send a one-time
# alert when the regime flips, rather than spamming you every scan.
_last_regime_state: Optional[bool] = None
_last_regime_reason: Optional[str] = None


def _install_signal_handlers() -> None:
    def _handler(signum, _frame):
        log.info("Received signal %s — shutting down...", signum)
        _shutdown.set()

    # Only install in the main thread (signal.signal() requires it).
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------
def main() -> None:
    """Long-running live engine. Blocks until SIGTERM/SIGINT."""
    log.info("=== ICS Live Engine started (mode=%s, trading=%s) ===",
             config.LIVE_PARAMS.scan_mode, config.TRADING_MODE)

    # Safety: scream if auto-apply revalidation is enabled.  This default
    # is False for good reason — auto-tuning parameters is how systems die
    # quietly.  If you see this warning, ask yourself whether you really
    # meant to enable it, and read ics/revalidation.py before continuing.
    if config.REVALIDATION.auto_apply:
        warning = (
            "⚠ ⚠ ⚠  config.REVALIDATION.auto_apply IS TRUE  ⚠ ⚠ ⚠\n"
            "    The revalidation system will (in a future build) auto-write\n"
            "    new parameters from WFO output to config.py without human\n"
            "    review.  This is STRONGLY DISCOURAGED.  Auto-tuned trading\n"
            "    systems drift away from their validation premise and quietly\n"
            "    destroy themselves.  Set auto_apply=False to restore safe\n"
            "    defaults.  In the current build, this flag does nothing\n"
            "    except print this warning — parameter writeback is\n"
            "    intentionally not implemented."
        )
        log.warning("\n%s", warning)
        try:
            notifier.send_plain(warning.replace("⚠", "[!]"))
        except Exception:
            pass

    db.init_db()
    notifier.mark_started()
    _install_signal_handlers()

    # Wire status + commands BEFORE the listener thread starts polling.
    notifier.set_status_provider(_status_dict)
    notifier.register_action("scan", _cmd_scan,
        description="trigger a manual scan now")
    notifier.register_action("refresh", _cmd_refresh,
        description="refresh the watchlist now (30-60s)")
    notifier.register_action("equity", _cmd_equity,
        description="show current paper equity")
    notifier.register_action("paper", _cmd_paper,
        description="full paper-trading status & P&L")
    notifier.register_action("regime", _cmd_regime,
        description="current broad-market regime")
    notifier.register_action("done", _cmd_done,
        description="record fill: /done <id|ticker> <price> [shares]")
    notifier.register_action("missed", _cmd_missed,
        description="mark alert as not executed: /missed <id|ticker> [notes]")
    notifier.register_action("pending", _cmd_pending,
        description="alerts awaiting your /done or /missed")
    notifier.register_action("slippage", _cmd_slippage,
        description="execution slippage report: /slippage [days]")
    notifier.register_action("premarket", _cmd_premarket,
        description="pre-market readiness snapshot on demand")
    notifier.start_command_listener()

    # Optional startup refresh
    if config.LIVE_PARAMS.refresh_watchlist_on_start:
        try:
            _refresh_watchlist_now()
        except Exception as e:
            log.error("Startup watchlist refresh failed: %s", e)
            notifier.send_plain(f"⚠️ Startup watchlist refresh failed: {e}")

    # First scan immediately so the user gets a signal of life
    try:
        run_scan_once(notify=True)
    except Exception as e:
        log.exception("Initial scan failed: %s", e)
        notifier.send_plain(f"❌ Initial scan failed: {str(e)[:200]}")

    mode_emoji = {"paper": "📓", "live": "💰", "off": "👁️"}.get(config.TRADING_MODE, "")
    notifier.send_plain(
        f"✅ ICS bot is up. {mode_emoji} Mode: {config.TRADING_MODE.upper()}\n"
        f"/status /scan /refresh /paper /regime /equity /help"
    )

    try:
        if config.LIVE_PARAMS.scan_mode == "intraday":
            _run_intraday_loop()
        else:
            _run_daily_loop()
    except KeyboardInterrupt:
        pass
    finally:
        log.info("Live engine stopped.")
        notifier.send_plain("👋 ICS bot shutting down.")


def run_scan_once(notify: bool = True) -> int:
    """One-shot scan. Returns the number of signals produced.

    Used by `ics scan` and by the live-engine schedulers.  Idempotent and
    safe to call from any thread that has its own event loop / signal setup.
    """
    global _last_scan_at

    # Always ensure the DB schema exists — init_db() is idempotent
    # (uses CREATE TABLE IF NOT EXISTS) so safe to call on every scan.
    # Protects against run_scan_once being called before main() has run
    # (e.g. a Telegram /scan arriving before the startup sequence finishes,
    # or a direct CLI call that bypasses main()).
    try:
        db.init_db()
    except Exception as e:
        log.error("DB init failed: %s", e)

    log.info("Running scan...")
    tickers = _resolve_tickers()
    if not tickers:
        log.warning("No tickers to scan (empty watchlist and empty universe).")
        if notify:
            notifier.send_plain("🔍 No tickers to scan.")
        return 0

    # Check broad-market regime ONCE per scan.  This drives both the transition
    # alert (sent only when the state flips) and the always-on banner in the
    # scan report.
    global _last_regime_state, _last_regime_reason
    regime = signals.current_regime_status()
    if notify and regime["enabled"]:
        # Detect transition: if state changed since last scan, send a one-time
        # heads-up.  This is the message you actually want — not every scan.
        if _last_regime_state is not None and regime["ok"] != _last_regime_state:
            if regime["ok"]:
                notifier.send_plain(
                    "🟢 REGIME ON — broad-market filter passes again.\n\n"
                    f"Reason: {regime['reason']}\n"
                    f"As of:  {regime['as_of']}\n\n"
                    "The bot will resume taking new entries on the next scan."
                )
            else:
                notifier.send_plain(
                    "🚨 REGIME OFF — broad-market filter has flipped.\n\n"
                    f"Reason: {regime['reason']}\n"
                    f"As of:  {regime['as_of']}\n\n"
                    "The bot will stop opening new positions. Existing "
                    "positions will continue to be managed normally — stops, "
                    "targets, and trailing exits all still fire.\n\n"
                    "Consider: reduce equity exposure, hold cash, or wait for "
                    "the regime to flip back ON. Don't fight the filter."
                )
        _last_regime_state = regime["ok"]
        _last_regime_reason = regime["reason"]

    try:
        sigs = signals.scan_universe(tickers, only_last_bar=True)
    except Exception as e:
        log.exception("scan_universe failed: %s", e)
        if notify:
            notifier.send_plain(f"❌ Scan error: {str(e)[:200]}")
        return 0

    _last_scan_at = datetime.now(timezone.utc)

    # Earnings blackout: drop signals on tickers within N calendar days of
    # earnings.  Live/paper only — backtests don't get this because we don't
    # have point-in-time earnings history.  Fail-open: if the earnings
    # lookup errors or returns None, the signal proceeds normally.
    blackout_dropped: list = []
    if config.SIGNAL_PARAMS.earnings_blackout_enabled_live and sigs:
        try:
            from . import earnings as earnings_mod
            now_ts = pd.Timestamp(_last_scan_at).tz_convert(None) if _last_scan_at.tzinfo else pd.Timestamp(_last_scan_at)
            kept: list = []
            for s in sigs:
                try:
                    in_blackout = earnings_mod.is_in_earnings_blackout(
                        s.ticker, now_ts,
                        blackout_days=config.SIGNAL_PARAMS.earnings_blackout_days,
                    )
                except Exception as e:
                    log.debug("Earnings check failed for %s, allowing signal: %s",
                              s.ticker, e)
                    in_blackout = False
                if in_blackout:
                    blackout_dropped.append(s.ticker)
                    log.info("Earnings blackout: dropping %s signal.", s.ticker)
                else:
                    kept.append(s)
            if blackout_dropped:
                log.info("Earnings blackout dropped %d signals: %s",
                         len(blackout_dropped), ",".join(blackout_dropped))
            sigs = kept
        except Exception as e:
            log.warning("Earnings blackout filter errored, allowing all signals: %s", e)

    # Persist signals — each guarded individually so one bad signal
    # doesn't abort the rest or surface a raw DB error to Telegram.
    for s in sigs:
        try:
            db.insert_signal(s.to_dict(), source="live")
        except Exception as e:
            log.debug("DB insert_signal failed for %s: %s", s.ticker, e)

    # Paper trading: hand each signal to the paper trader, then mark-to-market.
    # See config.TRADING_MODE for mode switching.
    paper_actions: list = []
    if config.TRADING_MODE == "paper":
        try:
            from .paper_trader import get_trader
            from .data import get_fx_series
            trader = get_trader()
            fx_series = get_fx_series()
            fx = float(fx_series.iloc[-1]) if fx_series is not None and not fx_series.empty else 0.79
            # Open new paper positions.  Sort signals by conviction so the
            # limited paper slots go to the HIGHEST-conviction signals first:
            # Tier 1 before Tier 2, and within each tier by descending score.
            sorted_sigs = sorted(
                sigs,
                key=lambda s: (getattr(s, "tier", 999), -getattr(s, "score", 0)),
            )
            # Cap NEW entries per scan.  Even with 8 open slots free, we only
            # open this many in a single bar — prevents one noisy day from
            # filling all slots with correlated trades that all stop out
            # together.  See config.PaperTradingConfig.max_new_entries_per_scan.
            max_new = config.PAPER_CONFIG.max_new_entries_per_scan
            new_entries_this_scan = 0
            for s in sorted_sigs:
                if new_entries_this_scan >= max_new:
                    log.info("Paper: per-scan cap of %d new entries reached, "
                             "skipping remaining %d signals.",
                             max_new, len(sorted_sigs) - sorted_sigs.index(s))
                    break
                if trader.process_signal(s.to_dict(), fx_gbp_per_usd=fx):
                    paper_actions.append({"action": "open", "ticker": s.ticker})
                    new_entries_this_scan += 1
            # Mark existing positions to market
            closed = trader.mark_to_market(fx_gbp_per_usd=fx)
            paper_actions.extend(closed)
            # Snapshot equity
            trader.record_equity_snapshot(fx_gbp_per_usd=fx)
        except Exception as e:
            log.exception("Paper trader failed during scan: %s", e)
            notifier.send_plain(f"⚠️ Paper trader error: {str(e)[:200]}")

    # Send Telegram report — guarded so a report-formatting error
    # (e.g. FX feed down, sizing edge-case) doesn't kill the scan result.
    if notify:
        try:
            _send_scan_report(sigs, paper_actions=paper_actions, regime=regime,
                              blackout_dropped=blackout_dropped)
        except Exception as e:
            log.exception("_send_scan_report failed: %s", e)
            notifier.send_plain(
                f"⚠️ Scan found {len(sigs)} signal(s) but report failed: {str(e)[:150]}"
            )

    log.info("Scan done: %d signals.", len(sigs))
    return len(sigs)


# ---------------------------------------------------------------------------
# Telegram action handlers
# ---------------------------------------------------------------------------
def _cmd_scan() -> Optional[str]:
    notifier.send_plain("🔍 Manual scan triggered...")
    n = run_scan_once(notify=True)
    return None  # _send_scan_report already sent the message


def _cmd_refresh() -> Optional[str]:
    notifier.send_plain("🔄 Refreshing watchlist (30–60s)...")
    try:
        df = _refresh_watchlist_now()
    except Exception as e:
        log.exception("Refresh failed: %s", e)
        return f"❌ Refresh failed: {str(e)[:200]}"
    n = len(df) if df is not None else 0
    top = ", ".join(df.head(10)["ticker"].tolist()) if n else "(none)"
    return f"✅ Watchlist refreshed: {n} tickers passed.\nTop 10: {top}"


def _cmd_equity() -> Optional[str]:
    """Telegram /equity — live equity with per-position marks and P&L.

    Uses yfinance fast_info (typically ~15min-delayed quotes for free
    accounts) when the market is open, falls back to the last cached
    Close when the quote is unavailable or the market is closed.

    The header shows whether marks are LIVE (intraday quote succeeded)
    or CLOSE (using yesterday's/last bar's close).
    """
    if config.TRADING_MODE != "paper":
        eq = _get_equity()
        return f"💼 Equity: £{eq:,.2f}"

    try:
        from .paper_trader import get_trader
        from .data import get_fx_series, get_quote
        trader = get_trader()
        fx_series = get_fx_series()
        fx = float(fx_series.iloc[-1]) if fx_series is not None and not fx_series.empty else 0.79
    except Exception as e:
        return f"❌ /equity failed: {str(e)[:200]}"

    if not trader.positions:
        # No open positions — just cash.
        starting = float(trader.cfg.starting_capital_gbp)
        pnl = trader.cash_gbp - starting
        pnl_emoji = "🟢" if pnl >= 0 else "🔴"
        return (
            f"💼 ICS EQUITY\n"
            f"Cash:    £{trader.cash_gbp:,.2f}\n"
            f"P&L:     {pnl_emoji} £{pnl:+,.2f} "
            f"({(pnl / starting * 100) if starting else 0:+.2f}%)\n"
            f"No open positions."
        )

    # Build per-position rows with intraday marks where possible.
    rows = []
    n_live = 0
    n_close = 0
    unrealised_gbp = 0.0
    for pos in trader.positions.values():
        quote = get_quote(pos.ticker)
        if quote is not None:
            mark = quote
            source = "LIVE"
            n_live += 1
        else:
            # Fall back to the most recent cached daily close.
            try:
                from .data import get_history
                df = get_history(pos.ticker,
                                 start=pos.entry_ts.strftime("%Y-%m-%d"))
                mark = float(df["Close"].iloc[-1]) if df is not None and not df.empty else pos.entry_usd
                source = "CLOSE"
                n_close += 1
            except Exception:
                mark = pos.entry_usd
                source = "?"
        pos_pnl_usd = (mark - pos.entry_usd) * pos.shares
        pos_pnl_gbp = pos_pnl_usd * fx
        pos_pnl_pct = ((mark / pos.entry_usd) - 1.0) * 100.0 if pos.entry_usd else 0.0
        unrealised_gbp += pos_pnl_gbp
        emoji = "🟢" if pos_pnl_gbp >= 0 else "🔴"
        rows.append({
            "ticker": pos.ticker, "tier": pos.tier, "mark": mark,
            "source": source, "pnl_gbp": pos_pnl_gbp,
            "pnl_pct": pos_pnl_pct, "emoji": emoji,
        })

    # Compute totals
    eq_gbp = trader.current_equity(fx)
    starting = float(trader.cfg.starting_capital_gbp)
    total_pnl = eq_gbp - starting
    total_pnl_emoji = "🟢" if total_pnl >= 0 else "🔴"
    total_pnl_pct = (total_pnl / starting * 100) if starting else 0.0

    # Freshness banner
    if n_live > 0 and n_close == 0:
        freshness = "🟢 LIVE quotes (~15min delay)"
    elif n_live == 0 and n_close > 0:
        freshness = "📅 CLOSE marks (market closed / quote unavailable)"
    else:
        freshness = f"⚠️ MIXED: {n_live} live, {n_close} close-marked"

    lines = [
        "💼 ICS EQUITY",
        freshness,
        "",
        f"Total equity:  £{eq_gbp:,.2f}",
        f"Cash:          £{trader.cash_gbp:,.2f}",
        f"Open notional: £{(eq_gbp - trader.cash_gbp):,.2f}",
        f"{total_pnl_emoji} P&L:        £{total_pnl:+,.2f} ({total_pnl_pct:+.2f}%)",
        "",
        "Positions:",
    ]
    for r in rows:
        lines.append(
            f"{r['emoji']} T{r['tier']} {r['ticker']:6s} @ ${r['mark']:>8.2f} "
            f"[{r['source']}]  £{r['pnl_gbp']:+,.2f} ({r['pnl_pct']:+.2f}%)"
        )
    return "\n".join(lines)


def _cmd_regime() -> Optional[str]:
    """Telegram /regime handler — show current broad-market regime status."""
    r = signals.current_regime_status()
    if not r["enabled"]:
        return "⚙️  Regime filter is disabled in config (REGIME_FILTERS.enabled=False)."
    emoji = "🟢" if r["ok"] else "🚨"
    label = "ON — taking new entries" if r["ok"] else "OFF — blocking new entries"
    return (
        f"{emoji} Regime: {label}\n\n"
        f"Reason: {r['reason']}\n"
        f"As of:  {r['as_of']}\n\n"
        + ("New entries allowed. Paper trader will open positions on signals."
           if r["ok"] else
           "New entries are blocked. Existing positions still managed normally.\n"
           "Recommendation: don't add new capital until regime flips back on.")
    )


def _cmd_paper() -> Optional[str]:
    """Telegram /paper handler — show paper-trading status and performance."""
    if config.TRADING_MODE != "paper":
        return f"⚙️  Bot is in {config.TRADING_MODE.upper()} mode (not paper)."
    try:
        from .paper_trader import get_trader
        from .data import get_fx_series
        trader = get_trader()
        fx = float(get_fx_series().iloc[-1])
        eq = trader.current_equity(fx)
        s = trader.summary()
    except Exception as e:
        return f"❌ Paper status failed: {str(e)[:200]}"

    pnl_gbp = eq - s["starting_capital_gbp"]
    pnl_pct = (pnl_gbp / s["starting_capital_gbp"]) * 100 if s["starting_capital_gbp"] else 0
    pnl_emoji = "🟢" if pnl_gbp >= 0 else "🔴"

    lines = [
        "📓 PAPER TRADING STATUS",
        "",
        f"Starting capital:  £{s['starting_capital_gbp']:,.2f}",
        f"Current equity:    £{eq:,.2f}",
        f"{pnl_emoji} P&L:           £{pnl_gbp:+,.2f} ({pnl_pct:+.1f}%)",
        "",
        f"Cash:              £{s['cash_gbp']:,.2f}",
        f"Open positions:    {s['open_positions']} "
        f"(Tier 1: {s.get('open_tier1', 0)}, Tier 2: {s.get('open_tier2', 0)})",
        "",
        f"Closed trades:     {s['n_closed']}",
        f"Win rate:          {s['win_rate_pct']:.1f}%",
        f"Avg trade:         £{s['avg_pnl_gbp']:+.2f}",
        f"Best:              £{s['best_trade_gbp']:+.2f}",
        f"Worst:             £{s['worst_trade_gbp']:+.2f}",
    ]

    # Per-tier breakdown — answers "are Tier 2 trades net-positive?"
    tb = s.get("tier_breakdown") or {}
    if tb and any(tb[t]["n"] > 0 for t in tb):
        lines.append("")
        lines.append("Per-tier performance:")
        for tier_num in (1, 2):
            t = tb.get(tier_num) or tb.get(str(tier_num)) or {}
            n = t.get("n", 0)
            if n == 0:
                lines.append(f"Tier {tier_num}: no closed trades yet")
                continue
            wr = t.get("win_rate_pct", 0)
            total = t.get("total_pnl_gbp", 0)
            avg = t.get("avg_pnl_gbp", 0)
            verdict = "✅" if avg > 0 else "❌"
            lines.append(
                f"{verdict} Tier {tier_num}: {n} trades  |  "
                f"win {wr:.0f}%  |  total £{total:+.0f}  |  avg £{avg:+.2f}"
            )
        # Once both tiers have ≥10 closed trades, surface the recommendation
        t1 = tb.get(1) or {}
        t2 = tb.get(2) or {}
        if t1.get("n", 0) >= 10 and t2.get("n", 0) >= 10:
            lines.append("")
            if t2.get("avg_pnl_gbp", 0) < 0 and t1.get("avg_pnl_gbp", 0) > 0:
                lines.append("⚠️  Tier 2 trades are net-negative; "
                             "consider dropping them.")
            elif t2.get("avg_pnl_gbp", 0) > 0:
                lines.append("✓ Tier 2 trades are net-positive.")

    # Show open positions
    if trader.positions:
        lines.append("")
        lines.append("Open positions:")
        for p in trader.positions.values():
            lines.append(
                f"T{p.tier} {p.ticker} ({p.shares} sh @ ${p.entry_usd:.2f}, "
                f"stop ${p.initial_stop_usd:.2f}, target ${p.target_usd:.2f})"
            )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Execution-audit and slippage handlers
# ---------------------------------------------------------------------------
def _cmd_done(args: str) -> Optional[str]:
    """
    Telegram /done handler — record an actual fill.

    Usage:
        /done 42 178.42           — id 42 filled at $178.42
        /done 42 178.42 25        — id 42, 25 shares (override planned)
        /done AAPL 178.42         — latest pending AAPL alert
        /done AAPL 178.42 25      — latest AAPL with explicit shares

    Returns a single-line confirmation including the slippage delta.
    Fail-soft: any parse problem produces a helpful usage hint instead
    of a crash.
    """
    parts = (args or "").split()
    if len(parts) < 2:
        return ("Usage:\n"
                "  /done <id-or-ticker> <fill_price> [shares]\n"
                "  Examples:\n"
                "    /done 42 178.42\n"
                "    /done AAPL 178.42 25\n"
                "  See /pending for outstanding alerts.")
    key, fill_str, *rest = parts
    try:
        fill = float(fill_str)
    except ValueError:
        return f"❌ Couldn't parse fill price '{fill_str}'. Expected a number."
    if fill <= 0:
        return f"❌ Fill price must be positive (got {fill})."
    shares: Optional[int] = None
    if rest:
        try:
            shares = int(rest[0])
        except ValueError:
            return f"❌ Couldn't parse shares '{rest[0]}'. Expected an integer."
    # Resolve key as either signal_id or ticker
    signal_id: Optional[int] = None
    ticker: Optional[str] = None
    if key.isdigit():
        signal_id = int(key)
    else:
        ticker = key.upper()
    try:
        row = db.record_user_execution(
            signal_id=signal_id, ticker=ticker,
            user_fill_usd=fill, user_shares=shares,
            outcome="executed",
        )
    except Exception as e:
        log.exception("/done failed: %s", e)
        return f"❌ /done failed: {str(e)[:200]}"
    if row is None:
        if signal_id is not None:
            return (f"❌ No alert #{signal_id} found, or it already has an outcome.\n"
                    f"   Use /pending to see open alerts.")
        return (f"❌ No pending alert for {ticker} found.\n"
                f"   Use /pending to see open alerts.")
    slip = row.get("slippage_pct")
    if slip is not None:
        slip_pct = float(slip) * 100
        arrow = "↑" if slip > 0 else ("↓" if slip < 0 else "→")
        return (f"✓ Recorded #{row['id']} {row['ticker']}: "
                f"filled ${float(row['user_fill_usd']):.2f}, "
                f"bot expected ${float(row['expected_fill_usd']):.2f} "
                f"{arrow} {slip_pct:+.3f}%")
    return f"✓ Recorded #{row['id']} {row['ticker']}: filled ${fill:.2f}"


def _cmd_missed(args: str) -> Optional[str]:
    """
    Telegram /missed handler — mark an alert as not executed.

    Usage:
        /missed 42                — id 42 not taken
        /missed AAPL              — latest pending AAPL alert
        /missed 42 in a meeting   — optional notes
    """
    parts = (args or "").split(None, 1)
    if not parts:
        return ("Usage:\n"
                "  /missed <id-or-ticker> [notes]\n"
                "  Marks the alert as 'missed' so it counts against your\n"
                "  execution rate but doesn't pollute slippage stats.")
    key = parts[0]
    notes = parts[1].strip() if len(parts) > 1 else None
    signal_id = int(key) if key.isdigit() else None
    ticker = None if signal_id is not None else key.upper()
    try:
        row = db.record_user_execution(
            signal_id=signal_id, ticker=ticker,
            outcome="missed", notes=notes,
        )
    except Exception as e:
        log.exception("/missed failed: %s", e)
        return f"❌ /missed failed: {str(e)[:200]}"
    if row is None:
        return (f"❌ No pending alert found for {key!r}.\n"
                f"   Use /pending to see open alerts.")
    return f"✓ Marked #{row['id']} {row['ticker']} as missed."


def _cmd_pending(_args: Optional[str] = None) -> Optional[str]:
    """Telegram /pending — list alerts awaiting a /done or /missed reply."""
    try:
        rows = db.get_pending_signals(within_days=7)
    except Exception as e:
        return f"❌ /pending failed: {str(e)[:200]}"
    if not rows:
        return "✓ No pending alerts in the last 7 days."
    lines = [f"⏳ {len(rows)} pending alert(s) — reply /done <id> <fill> or /missed <id>:"]
    for r in rows[:15]:  # cap so a runaway never spams the chat
        ts = (r.get("alert_sent_at") or "")[:16].replace("T", " ")
        lines.append(
            f"[#{r['id']}] {r['ticker']:6s} T{r['tier']} "
            f"expected ${float(r['expected_fill_usd']):.2f} "
            f"({ts} UTC)"
        )
    if len(rows) > 15:
        lines.append(f"... and {len(rows) - 15} more (use CLI: ics slippage-report)")
    return "\n".join(lines)


def _cmd_slippage(args: str) -> Optional[str]:
    """
    Telegram /slippage — distribution of fills vs bot-expected.

    Usage:
        /slippage           — last 30 days
        /slippage 60        — last 60 days
        /slippage 7         — last week
    """
    try:
        days = int(args.strip()) if args and args.strip() else 30
    except ValueError:
        days = 30
    if days < 1 or days > 365:
        days = 30
    try:
        from .slippage import build_report, format_report
        rep = build_report(days=days)
        return format_report(rep)
    except Exception as e:
        log.exception("/slippage failed: %s", e)
        return f"❌ /slippage failed: {str(e)[:200]}"


def _cmd_premarket(_args: Optional[str] = None) -> Optional[str]:
    """Telegram /premarket — pre-market readiness snapshot.

    Computes the same readiness report the scheduled job sends (current
    open positions, available cash, regime state, earnings risk on open
    positions).  Useful when you wake up and want one-shot context
    before the open.
    """
    try:
        return _build_premarket_report()
    except Exception as e:
        log.exception("/premarket failed: %s", e)
        return f"❌ /premarket failed: {str(e)[:200]}"


def _build_premarket_report() -> str:
    """
    Build the pre-market readiness text.  Shared by /premarket and the
    scheduled morning job so both produce identical output.

    Sections (skipped when empty):
      1. Header with date + minutes-to-open
      2. Regime state (banner)
      3. Open positions table with earnings flag per name
      4. Available cash + free slots
      5. Watchlist preview — top candidates by RS
    """
    now = _utc_now()
    lines = ["🌅 ICS PRE-MARKET READINESS"]

    # Time-to-open: DST-aware via _market_open_utc (handles EST/EDT
    # automatically rather than reading a static UTC hour from config).
    next_open_utc = _market_open_utc(now)
    if _market_is_open(now):
        lines.append("Market is OPEN.")
    elif now < next_open_utc:
        delta = next_open_utc - now
        total_min = int(delta.total_seconds() // 60)
        if total_min < 60:
            time_str = f"{total_min} min"
        else:
            h, m = divmod(total_min, 60)
            time_str = f"{h}h {m}m"
        lines.append(
            f"US open in {time_str} ({next_open_utc.strftime('%H:%M UTC')})"
        )
    else:
        lines.append("Market is CLOSED for the day.")
    lines.append("")

    # 1. Regime banner — same logic as scan report
    try:
        regime = signals.current_regime_status()
        if regime.get("enabled"):
            if regime.get("ok"):
                lines.append(f"🟢 Regime ON  —  {regime.get('reason', '')}")
            else:
                lines.append(f"🚨 REGIME OFF — bot will NOT take new entries")
                lines.append(f"Reason: {regime.get('reason', '?')}")
        lines.append("")
    except Exception as e:
        log.debug("premarket regime check failed: %s", e)

    # 2. Open positions (paper mode shows paper book; live mode would
    #    show live book but we don't have an order-router yet, so paper).
    open_pos_lines: list = []
    earnings_today: list = []
    if config.TRADING_MODE == "paper":
        try:
            from .paper_trader import get_trader
            trader = get_trader()
            if trader.positions:
                from . import earnings as earnings_mod
                today_ts = pd.Timestamp(now).normalize()
                for pos in trader.positions.values():
                    risk_dist = pos.entry_usd - pos.initial_stop_usd
                    earn_flag = ""
                    try:
                        days_to_earn = None
                        next_e = earnings_mod.get_next_earnings(pos.ticker)
                        if next_e is not None:
                            days_to_earn = (next_e - today_ts).days
                            if 0 <= days_to_earn <= 7:
                                earn_flag = f"  📅 earnings in {days_to_earn}d"
                                if days_to_earn <= 1:
                                    earnings_today.append(pos.ticker)
                    except Exception:
                        pass
                    open_pos_lines.append(
                        f"T{pos.tier} {pos.ticker:6s} "
                        f"@ ${pos.entry_usd:.2f}  "
                        f"stop ${pos.initial_stop_usd:.2f} "
                        f"(-{risk_dist/pos.entry_usd*100:.1f}%)"
                        f"{earn_flag}"
                    )
        except Exception as e:
            log.debug("premarket positions block failed: %s", e)

    if open_pos_lines:
        lines.append("Open positions:")
        lines.extend(open_pos_lines)
        if earnings_today:
            lines.append("")
            lines.append(f"⚠️ Earnings today/tomorrow on: {', '.join(earnings_today)}")
            lines.append("Watch for opening gaps — your stop may be miles away.")
        lines.append("")

    # 3. Cash / slots
    if config.TRADING_MODE == "paper":
        try:
            from .paper_trader import get_trader
            trader = get_trader()
            n_open = len(trader.positions)
            n_max = config.RISK_PARAMS.max_open_positions
            lines.append(f"Cash: £{trader.cash_gbp:,.0f}  |  "
                         f"Slots: {n_open}/{n_max} open ({n_max - n_open} free)")
            lines.append("")
        except Exception as e:
            log.debug("premarket cash block failed: %s", e)

    # 4. Watchlist preview — top 5 by RS
    try:
        wl = watchlist.load_watchlist()
        if not wl.empty:
            top = wl.head(5)
            lines.append(f"Top watchlist (of {len(wl)}, sorted by RS_1m):")
            for _, r in top.iterrows():
                rs = float(r.get("rs_1m", 0.0)) * 100
                px = float(r.get("last_close", 0.0))
                vr = float(r.get("vol_ratio_20_60", 0.0))
                lines.append(f"{r['ticker']:6s} ${px:>7.2f}  "
                             f"RS {rs:+5.1f}%  vol×{vr:.2f}")
            lines.append("")
            lines.append("(signals fire on bar close, not pre-market — this is")
            lines.append("just what's eligible if a setup completes today.)")
    except Exception as e:
        log.debug("premarket watchlist block failed: %s", e)

    return "\n".join(lines)


def _status_dict() -> dict:
    out = {"mode": config.LIVE_PARAMS.scan_mode}
    if _last_scan_at:
        out["last scan (UTC)"] = _last_scan_at.strftime("%Y-%m-%d %H:%M:%S")
    if _last_refresh_at:
        out["last refresh (UTC)"] = _last_refresh_at.strftime("%Y-%m-%d %H:%M:%S")
    if _last_summary_at:
        out["last summary (UTC)"] = _last_summary_at.strftime("%Y-%m-%d %H:%M:%S")
    out["equity"] = f"£{_get_equity():,.0f}"
    # Surface regime state so you can ping /status anytime to check
    if _last_regime_state is not None:
        emoji = "🟢" if _last_regime_state else "🚨"
        label = "ON" if _last_regime_state else "OFF"
        reason = (_last_regime_reason or "").split(" — ")[0][:60]
        out["regime"] = f"{emoji} {label} ({reason})"
    return out


# ---------------------------------------------------------------------------
# Scheduler loops
# ---------------------------------------------------------------------------
def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# US-market time helpers — DST-aware
#
# US equity market is open 09:30-16:00 NEW YORK TIME, which is:
#   - EST (UTC-5) in winter: open 14:30 UTC, close 21:00 UTC
#   - EDT (UTC-4) in summer: open 13:30 UTC, close 20:00 UTC
#
# config.LIVE_PARAMS.market_open_utc_hour is a static value and gets the
# winter case right by default.  These helpers convert to the correct UTC
# time for the actual date being asked about, so the bot doesn't tell you
# "market opens in 8 minutes" an hour after it actually opened.
#
# Year-round, the NYSE open is 09:30 New York time and close is 16:00.
# We do NOT model US holidays here (yfinance returns yesterday's bar on
# closed days, which the cooldown register handles cleanly).
# ---------------------------------------------------------------------------
_NY_TZ = ZoneInfo("America/New_York")
_NYSE_OPEN_NY = dtime(9, 30)
_NYSE_CLOSE_NY = dtime(16, 0)


def _market_open_utc(now: Optional[datetime] = None) -> datetime:
    """Return the UTC datetime of the next NYSE open at or after `now`.

    If `now` is BEFORE today's open, returns today's open.
    If `now` is AFTER today's close (or on a weekend), returns the next
    weekday's open.
    """
    now = now or _utc_now()
    now_ny = now.astimezone(_NY_TZ)
    candidate = now_ny.replace(
        hour=_NYSE_OPEN_NY.hour, minute=_NYSE_OPEN_NY.minute,
        second=0, microsecond=0,
    )
    today_close = candidate.replace(
        hour=_NYSE_CLOSE_NY.hour, minute=_NYSE_CLOSE_NY.minute,
    )
    # If we're already past today's close, or it's a weekend, roll forward.
    if now_ny >= today_close or now_ny.weekday() >= 5:
        candidate += timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def _market_is_open(now: Optional[datetime] = None) -> bool:
    """True if NYSE is currently open (DST-aware, no holiday calendar)."""
    now = now or _utc_now()
    now_ny = now.astimezone(_NY_TZ)
    if now_ny.weekday() >= 5:
        return False
    return _NYSE_OPEN_NY <= now_ny.time() <= _NYSE_CLOSE_NY


def _seconds_until(target_h: int, target_m: int) -> float:
    """Seconds from now (UTC) until the next occurrence of target_h:target_m UTC."""
    now = _utc_now()
    target = now.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _wait(seconds: float) -> bool:
    """Sleep up to `seconds`, returning True if shutdown was requested."""
    return _shutdown.wait(timeout=max(0.0, seconds))


def _run_intraday_loop() -> None:
    p = config.LIVE_PARAMS
    interval = max(60, int(p.scan_interval_minutes * 60))
    log.info("Intraday loop: every %d minutes during market hours.", p.scan_interval_minutes)

    while not _shutdown.is_set():
        now = _utc_now()
        if _market_is_open(now):
            try:
                run_scan_once(notify=True)
            except Exception as e:
                log.exception("Scan failed: %s", e)
            _maybe_daily_refresh(now)
            _maybe_premarket(now)
            if _wait(interval):
                break
        else:
            # Sleep until next market open or check daily summary first.
            if _maybe_daily_summary(now):
                continue
            # Premarket may need to fire while market is closed (we run it
            # 5 min before open) — check before going to sleep.
            if _maybe_premarket(now):
                continue
            sleep_for = _seconds_until(p.market_open_utc_hour, p.market_open_utc_minute)
            log.info("Market closed — sleeping %.1fh until next open.", sleep_for / 3600)
            if _wait(min(sleep_for, 3600)):  # wake at least hourly to re-check
                break


def _run_daily_loop() -> None:
    p = config.LIVE_PARAMS
    log.info("Daily loop: scan once at %02d:%02d UTC.", p.market_close_utc_hour, p.market_close_utc_minute)

    while not _shutdown.is_set():
        now = _utc_now()
        # Run pre-market readiness if we're at the right time (independent
        # of the end-of-day scan, which runs much later).
        _maybe_premarket(now)
        # End-of-day scan
        sleep_for = _seconds_until(p.market_close_utc_hour, p.market_close_utc_minute)
        log.info("Sleeping %.1fh until next end-of-day scan.", sleep_for / 3600)
        # Wake at least every 30 minutes so we can react to /scan etc.
        if _wait(min(sleep_for, 1800)):
            break
        if _shutdown.is_set():
            break
        # If we've reached the end-of-day window, run a scan.
        now = _utc_now()
        # Trigger if we're within 30 minutes of the configured close time AND
        # haven't scanned yet today.
        target = now.replace(hour=p.market_close_utc_hour,
                             minute=p.market_close_utc_minute,
                             second=0, microsecond=0)
        if abs((now - target).total_seconds()) < 60 * 30 and _can_scan_today(now):
            try:
                _maybe_daily_refresh(now)
                run_scan_once(notify=True)
                _maybe_daily_summary(now)
            except Exception as e:
                log.exception("Daily scan failed: %s", e)


def _can_scan_today(now: datetime) -> bool:
    if _last_scan_at is None:
        return True
    return _last_scan_at.date() < now.date()


def _maybe_daily_refresh(now: datetime) -> None:
    global _last_refresh_at
    if not config.LIVE_PARAMS.refresh_watchlist_daily:
        return
    if _last_refresh_at and _last_refresh_at.date() >= now.date():
        return
    try:
        _refresh_watchlist_now()
    except Exception as e:
        log.error("Daily watchlist refresh failed: %s", e)
    # Pay any due monthly contributions at the same daily cadence.  The
    # paper trader's apply_pending_contributions is idempotent (DB-keyed
    # by date) so multiple calls per day are safe.
    try:
        if config.TRADING_MODE == "paper" and config.CONTRIBUTIONS.enabled:
            from .paper_trader import get_trader
            trader = get_trader()
            credited = trader.apply_pending_contributions(
                now=pd.Timestamp(now).tz_localize(None) if now.tzinfo else pd.Timestamp(now),
            )
            if credited > 0:
                log.info("Daily contribution sweep credited £%.2f to paper account.", credited)
    except Exception as e:
        log.warning("Contribution sweep failed (non-fatal): %s", e)

    # Daily revalidation check.  Cheap when not due (just reads the status
    # file + paper Sharpe).  When DUE, the actual WFO is a 10-20 minute
    # background task — we launch it in a separate thread so the live
    # engine doesn't block.  auto_apply remains FALSE; this only ever
    # produces an alert.
    try:
        from .revalidation import should_revalidate, run_scheduled_revalidation
        decision = should_revalidate(now=now)
        if decision.should_run:
            log.warning("Revalidation triggered: %s", decision.reason)
            import threading
            t = threading.Thread(
                target=run_scheduled_revalidation,
                kwargs={"notify": True},
                name="ics-revalidation",
                daemon=True,
            )
            t.start()
    except Exception as e:
        log.warning("Revalidation check failed (non-fatal): %s", e)


def _maybe_daily_summary(now: datetime) -> bool:
    """If it's near the configured daily-summary time and we haven't sent one
    today, send one. Returns True if it sent."""
    global _last_summary_at
    p = config.LIVE_PARAMS
    target = now.replace(hour=p.daily_summary_utc_hour,
                         minute=p.daily_summary_utc_minute,
                         second=0, microsecond=0)
    if _last_summary_at and _last_summary_at.date() >= now.date():
        return False
    if abs((now - target).total_seconds()) > 60 * 5:
        return False
    try:
        _send_daily_summary()
    except Exception as e:
        log.exception("Daily summary failed: %s", e)
        return False
    _last_summary_at = now
    return True


def _maybe_premarket(now: datetime) -> bool:
    """If it's near the open and we haven't sent the premarket today, send it.

    Computes the target as `actual_open - premarket_lead_minutes` so the
    fire time tracks DST correctly year-round.  The static
    LIVE_PARAMS.premarket_utc_* fields are no longer consulted — they
    remain in config for legacy reasons but are ignored by the scheduler.

    Same once-per-day discipline as the daily summary: a ±5 min trigger
    window, only fires once per calendar day.
    """
    global _last_premarket_at
    p = config.LIVE_PARAMS
    if not p.premarket_enabled:
        return False
    if now.weekday() >= 5:  # don't spam on weekends
        return False
    if _last_premarket_at and _last_premarket_at.date() >= now.date():
        return False
    next_open = _market_open_utc(now)
    lead_minutes = getattr(p, "premarket_lead_minutes", 5)
    target = next_open - timedelta(minutes=lead_minutes)
    # Only fire if we're within ±5 min of target AND target is today.
    if target.date() != now.date():
        return False
    if abs((now - target).total_seconds()) > 60 * 5:
        return False
    try:
        notifier.send_plain(_build_premarket_report())
    except Exception as e:
        log.exception("Pre-market send failed: %s", e)
        return False
    _last_premarket_at = now
    return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _resolve_tickers() -> List[str]:
    """
    Resolve the universe for live scanning.

    Priority:
      1. data/watchlist.csv if present and non-empty
      2. Current NDX constituents (the WFO-validated universe)
      3. config.BASE_UNIVERSE[:60] as last-resort fallback

    The watchlist file remains the authoritative override so you can pin a
    specific universe; if absent we use the same point-in-time NDX universe
    the strategy was validated against.
    """
    # 1. watchlist override
    try:
        wl = pd.read_csv(config.WATCHLIST_CSV)
        ts = wl["ticker"].astype(str).str.strip().tolist()
        if ts:
            return ts
    except Exception:
        pass

    # 2. Current NDX constituents (matches the WFO universe)
    try:
        from .constituents import get_universe_at, check_library
        if check_library():
            today = pd.Timestamp.utcnow().tz_localize(None).normalize().date()
            tickers = get_universe_at(str(today))
            if tickers:
                log.info("Live scan using point-in-time NDX universe: %d tickers.",
                         len(tickers))
                return tickers
    except Exception as e:
        log.warning("Could not load NDX universe (%s) — falling back.", e)

    # 3. Last-resort fallback
    log.warning("Using BASE_UNIVERSE fallback (survivorship-biased).")
    return list(config.BASE_UNIVERSE[:60])


def _refresh_watchlist_now() -> Optional[pd.DataFrame]:
    global _last_refresh_at
    df = refresh_watchlist()
    _last_refresh_at = _utc_now()
    return df


def _get_equity() -> float:
    """
    Best-effort current equity.

    In paper mode, returns the live paper-trader equity (cash + unrealised).
    Otherwise reads the latest equity row from DB, or falls back to starting
    capital if no row exists.
    """
    if config.TRADING_MODE == "paper":
        try:
            from .paper_trader import get_trader
            from .data import get_fx_series
            fx_series = get_fx_series()
            if fx_series is not None and not fx_series.empty:
                fx = float(fx_series.iloc[-1])
                return get_trader().current_equity(fx)
        except Exception as e:
            log.debug("paper current_equity failed, falling back: %s", e)

    try:
        # Filter by mode-specific source so live mode doesn't see paper rows
        # and vice versa.
        source = "paper" if config.TRADING_MODE == "paper" else "live"
        with db.connect() as c:
            row = c.execute(
                "SELECT equity_gbp FROM equity WHERE source = ? "
                "ORDER BY timestamp DESC LIMIT 1",
                (source,)
            ).fetchone()
        if row is not None:
            return float(row["equity_gbp"])
    except Exception:
        pass
    return float(config.STARTING_CAPITAL_GBP)


def _conviction(tier: int, score: int) -> str:
    if tier == 1:
        return "5/5" if score == 6 else "4/5" if score == 5 else "3/5"
    return "3/5" if score >= 5 else "2/5" if score == 4 else "1/5"


def _clean_reasons(reasons: str) -> str:
    if not reasons:
        return ""
    return (
        reasons.replace("Px>", "Price > ")
        .replace("HMA55", "HMA(55)")
        .replace("HMA20", "HMA(20)")
        .replace("Vol>1.5x20d", "Vol 1.5×20d")
        .replace(" | ", ", ")
    )


def _send_scan_report(sigs, paper_actions: list | None = None,
                      regime: dict | None = None,
                      blackout_dropped: list | None = None) -> None:
    """
    Send the scan report to Telegram.

    The regime banner is shown at the top of every report — green when ON,
    red when OFF — so you always know the bot's current stance at a glance.
    A separate one-time transition alert is sent from run_scan_once when
    the state changes; this is the always-on summary.
    """
    # Helper for the regime banner — returns lines to prepend
    def _regime_banner_lines() -> list[str]:
        if not regime or not regime.get("enabled"):
            return []
        if regime.get("ok"):
            return [f"🟢 Regime ON  —  {regime.get('reason', '')}", ""]
        return [
            f"🚨 REGIME OFF — bot is NOT taking new entries",
            f"Reason: {regime.get('reason', '?')}",
            f"As of:  {regime.get('as_of', '?')}",
            "",
            "Existing positions still managed (stops / targets / trails).",
            "Recommendation: don't add new capital until regime flips back.",
            "",
        ]

    # Quiet-day case — but still surface regime if it's OFF, since that's the
    # most important thing to know on a day with no signals.
    if not sigs and not paper_actions:
        if regime and regime.get("enabled") and not regime.get("ok"):
            notifier.send_plain("\n".join(_regime_banner_lines()))
        else:
            # Truly quiet: regime is fine, just no setups today
            banner = _regime_banner_lines()
            msg = ("\n".join(banner) + "🔍 No signals right now."
                   if banner else "🔍 No signals right now.")
            notifier.send_plain(msg)
        return

    equity_gbp = _get_equity()
    fx_series = get_fx_series()
    fx = float(fx_series.iloc[-1]) if not fx_series.empty else 0.79

    rows = []
    for s in sigs:
        tier = int(getattr(s, "tier", 0))
        score = int(getattr(s, "score", 0))
        plan = compute_position(
            equity_gbp=equity_gbp, ticker=s.ticker,
            entry_usd=s.entry_price, stop_usd=s.stop_loss,
            target_usd=s.target_price, tier=tier, fx_gbp_per_usd=fx,
        )
        shares = plan.shares if plan else 0
        risk_gbp = plan.risk_gbp if plan else 0.0

        # Execution audit: log the alert as 'pending'.  The user reports
        # back via `/done <id> <price>` (Telegram) or `record-fill` (CLI).
        # The expected_fill_usd is the bot's slipped next-open price, the
        # same number compute_position used — so slippage_pct measures the
        # gap between the bot's plan and the user's reality.
        audit_id = None
        try:
            audit_id = db.record_signal_sent(
                ticker=s.ticker, tier=tier,
                expected_fill_usd=float(s.entry_price),
                stop_usd=float(s.stop_loss),
                target_usd=float(s.target_price),
                shares_planned=int(shares),
                signal_type=getattr(s, "signal_type", "momentum"),
            )
        except Exception as e:
            log.debug("audit record_signal_sent failed for %s: %s", s.ticker, e)

        rows.append({
            "ticker": s.ticker, "tier": tier, "score": score,
            "conviction": _conviction(tier, score),
            "entry_price": s.entry_price, "stop_loss": s.stop_loss,
            "target_price": s.target_price,
            "stop_pct": (s.entry_price - s.stop_loss) / s.entry_price * 100.0,
            "target_pct": (s.target_price - s.entry_price) / s.entry_price * 100.0,
            "shares": shares, "risk_gbp": risk_gbp,
            "reasons": _clean_reasons(getattr(s, "reasons", "")),
            "audit_id": audit_id,
        })

    # Build per-tier lists sorted by score desc, then take a cap from each so
    # both tiers always get representation in the message.
    #
    # Old logic took top 8 globally, sorted by tier asc.  That was correct as
    # far as priority went, but on bars with 8+ Tier 1 signals it filled all 8
    # display slots and Tier 2 was hidden completely — even when ~16 Tier 2
    # signals existed alongside.  Now we cap each tier independently so the
    # user always sees what's there.
    rows.sort(key=lambda r: (r["tier"], -r["score"]))

    DISPLAY_CAP_TIER1 = 15  # show at most 15 Tier 1 signals
    DISPLAY_CAP_TIER2 = 15  # show at most 15 Tier 2 signals
    tier1 = [r for r in rows if r["tier"] == 1][:DISPLAY_CAP_TIER1]
    tier2 = [r for r in rows if r["tier"] == 2][:DISPLAY_CAP_TIER2]

    # Counts of *all* signals in each tier (not just displayed) for the header
    n_tier1 = sum(1 for r in rows if r["tier"] == 1)
    n_tier2 = sum(1 for r in rows if r["tier"] == 2)

    lines = _regime_banner_lines() + [
        "🚨 ICS SCAN RESULTS 🚨",
        "",
        f"Found {len(sigs)} total | Tier 1: {n_tier1} | Tier 2: {n_tier2}",
    ]

    # If we truncated, tell the user how many we hid
    hidden_t1 = max(0, n_tier1 - DISPLAY_CAP_TIER1)
    hidden_t2 = max(0, n_tier2 - DISPLAY_CAP_TIER2)
    if hidden_t1 or hidden_t2:
        parts = []
        if hidden_t1: parts.append(f"{hidden_t1} more Tier 1")
        if hidden_t2: parts.append(f"{hidden_t2} more Tier 2")
        lines.append(f"(showing top {DISPLAY_CAP_TIER1}+{DISPLAY_CAP_TIER2}; "
                     f"{' and '.join(parts)} not shown)")

    def _block(label: str, items: list) -> None:
        if not items:
            return
        lines.append("")
        lines.append(label)
        for r in items:
            # Lead each signal with its audit id (when present) so the user
            # can reply '/done <id> <fill>' from their phone.  The id is
            # surfaced separately rather than buried in text so it's easy
            # to copy on mobile.
            id_tag = f"[#{r['audit_id']}] " if r.get("audit_id") else ""
            lines.append(f"{id_tag}{r['ticker']}  ${r['entry_price']:.2f}")
            lines.append(
                f"Target ${r['target_price']:.2f} (+{r['target_pct']:.1f}%)   "
                f"Stop ${r['stop_loss']:.2f} (-{r['stop_pct']:.1f}%)"
            )
            lines.append(
                f"Conviction: {r['conviction']}   "
                f"→ Buy {r['shares']:,} shares (£{r['risk_gbp']:.0f} risk)"
            )
            if r["reasons"]:
                lines.append(f"Conditions: {r['reasons']}")
            lines.append("")

    _block("TIER 1 — STRONG", tier1)
    _block("TIER 2", tier2)

    if not tier1 and not tier2 and not paper_actions:
        lines.append("No signals met the minimum criteria today.")

    # Append paper-trading actions if any.  Mode banner + per-action lines.
    if paper_actions:
        lines.append("")
        lines.append("📓 PAPER TRADER — actions this tick:")
        for a in paper_actions:
            if a.get("action") == "open":
                lines.append(f"+ opened {a['ticker']}")
            elif a.get("action") == "close":
                pnl = a.get("pnl_gbp", 0.0)
                rr  = a.get("return_pct", 0.0) * 100
                emoji = "🟢" if pnl >= 0 else "🔴"
                lines.append(
                    f"{emoji} closed {a['ticker']} @ ${a['price']:.2f} "
                    f"({a.get('reason','?')}) → £{pnl:+.2f} ({rr:+.1f}%)"
                )

    if config.TRADING_MODE != "live":
        lines.append("")
        lines.append(f"⚙️  Mode: {config.TRADING_MODE.upper()}")

    # Earnings blackout footer — list which tickers had signals suppressed.
    # Kept compact since this is informational, not actionable: you can't
    # do anything about an earnings date.  Surfaced so you know why a name
    # you might have expected to see isn't in the report.
    if blackout_dropped:
        lines.append("")
        lines.append(
            f"📅 Earnings blackout: skipped {len(blackout_dropped)} signal(s) — "
            + ", ".join(blackout_dropped[:6])
            + ("..." if len(blackout_dropped) > 6 else "")
        )

    notifier.send_plain("\n".join(lines))


def _send_daily_summary() -> None:
    """Lightweight daily heartbeat. Replace with real summary once you wire
    paper-trading P&L into the DB."""
    eq = _get_equity()
    msg = (
        f"📊 ICS Daily Summary\n"
        f"Equity: £{eq:,.2f}\n"
        f"Last scan: {_last_scan_at.strftime('%Y-%m-%d %H:%M UTC') if _last_scan_at else 'n/a'}"
    )
    notifier.send_plain(msg)


if __name__ == "__main__":
    main()
