"""
paper_trader.py
---------------
Simulated trading layer that records every signal as a virtual trade with
realistic fills, fees, FX, and exit logic.  No real orders are sent anywhere.

The point of this module is to give you a proper out-of-sample test on LIVE
data before committing real capital.  Run for 60-90 days, compare paper
performance to WFO expectations, then decide.

Architecture
------------
- `PaperTrader` holds the in-memory state (open positions, equity history)
  but persists everything to SQLite via `db.insert_trade(... source='paper')`.
- `process_signal()`  — called when the live scanner produces a new signal.
- `mark_to_market()`  — called after each scan with the latest OHLC bar for
  each open position.  Detects stop / target / trailing-stop hits.
- `current_equity()`  — cash + unrealised P&L on open positions, in GBP.
- `summary()`         — performance stats for Telegram /paper_status.

What it does NOT do
-------------------
- It does not access the broker. There is no order routing.
- It does not handle dividends, splits, or corporate actions.
- It does not handle pyramid adds (yet) — Tier 1 positions get a single
  tranche.  Adding pyramiding is a future enhancement.

Persistence
-----------
Every paper trade is written to the `trades` table with `source='paper'`,
which keeps it cleanly separated from real-money trades (`source='live'`)
when those eventually exist.  Equity points are written to `equity` with
`source='paper'` too.

Restart behaviour
-----------------
On bot restart, `PaperTrader` reads open paper positions from the DB and
resumes managing them.  No state is lost across restarts.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

from . import config, db, data
from .logging_utils import get_logger
from .sizing import compute_position

log = get_logger("ics.paper")


# ---------------------------------------------------------------------------
# Open position state
# ---------------------------------------------------------------------------
@dataclass
class PaperPosition:
    ticker: str
    tier: int
    entry_ts: pd.Timestamp
    entry_usd: float
    initial_stop_usd: float
    target_usd: float
    shares: int
    fx_entry: float            # GBP per USD at entry
    high_water_usd: float      # for trailing stop
    db_id: Optional[int] = None  # row id in `trades` table

    def unrealised_usd(self, current_close: float) -> float:
        return (current_close - self.entry_usd) * self.shares

    def to_state_dict(self) -> dict:
        """Serialise just enough state to reload from DB on restart."""
        return {
            "ticker":           self.ticker,
            "tier":             self.tier,
            "entry_ts":         self.entry_ts.isoformat(),
            "entry_usd":        self.entry_usd,
            "initial_stop_usd": self.initial_stop_usd,
            "target_usd":       self.target_usd,
            "shares":           self.shares,
            "fx_entry":         self.fx_entry,
            "high_water_usd":   self.high_water_usd,
            "db_id":            self.db_id,
        }


# ---------------------------------------------------------------------------
# PaperTrader
# ---------------------------------------------------------------------------
class PaperTrader:
    """
    Holds paper-trading state and persists transitions to SQLite.

    Singleton-ish: the live engine creates one instance at startup.
    """

    def __init__(self, cfg: Optional[config.PaperTradingConfig] = None):
        self.cfg = cfg or config.PAPER_CONFIG
        self.cash_gbp: float = self.cfg.starting_capital_gbp
        self.positions: Dict[str, PaperPosition] = {}
        self._restore_from_db()

    # -----------------------------------------------------------------
    # Restoration on restart
    # -----------------------------------------------------------------
    def _restore_from_db(self) -> None:
        """Load open paper positions and prior cash balance from DB on startup."""
        try:
            db.init_db()
            with db.connect() as c:
                # Open positions = paper trades with no exit_ts
                rows = c.execute(
                    "SELECT id, ticker, tier, entry_ts, entry_usd, shares, fx_entry "
                    "FROM trades WHERE source='paper' AND exit_ts IS NULL"
                ).fetchall()
                for r in rows:
                    # Reconstruct stop / target / high_water from the
                    # paper_state JSON we stored on entry (see process_signal).
                    state_row = c.execute(
                        "SELECT payload_json FROM watchlist "
                        "WHERE snapshot_ts = ? AND ticker = ?",
                        (f"paper_state:{r['id']}", r["ticker"])
                    ).fetchone()
                    if state_row is None:
                        log.warning(
                            "Paper position %s has no saved state — "
                            "skipping restore.", r["ticker"]
                        )
                        continue
                    state = json.loads(state_row["payload_json"])
                    pos = PaperPosition(
                        ticker=r["ticker"],
                        tier=int(r["tier"]),
                        entry_ts=pd.Timestamp(r["entry_ts"]),
                        entry_usd=float(r["entry_usd"]),
                        initial_stop_usd=float(state["stop"]),
                        target_usd=float(state["target"]),
                        shares=int(r["shares"]),
                        fx_entry=float(r["fx_entry"]),
                        high_water_usd=float(state.get("high_water", r["entry_usd"])),
                        db_id=int(r["id"]),
                    )
                    self.positions[pos.ticker] = pos
                    log.info("Restored paper position: %s (entry $%.2f, %d shares)",
                             pos.ticker, pos.entry_usd, pos.shares)

                # Cash: start from configured capital + previously-recorded
                # contributions, deduct invested amount.  This keeps prior
                # standing-order deposits visible across restarts.
                invested_gbp = sum(
                    p.entry_usd * p.shares * p.fx_entry for p in self.positions.values()
                )
                prior_contribs = db.total_contributions(source="paper")
                self.cash_gbp = (
                    self.cfg.starting_capital_gbp + prior_contribs - invested_gbp
                )

            if self.positions:
                log.info("Paper trader restored: %d open positions, £%.2f cash",
                         len(self.positions), self.cash_gbp)
        except Exception as e:
            log.warning("Paper state restore failed (starting fresh): %s", e)

    def _save_state(self, pos: PaperPosition) -> None:
        """Persist the per-position stop/target/high_water for restart.

        Uses a unique snapshot_ts key (`paper_state:<db_id>`) so a simple
        DELETE+INSERT updates the state idempotently.  If the snapshot_ts
        index/uniqueness changes in db.py later, this will need to adapt.
        """
        if pos.db_id is None:
            return
        state = {
            "stop":       pos.initial_stop_usd,
            "target":     pos.target_usd,
            "high_water": pos.high_water_usd,
        }
        try:
            with db.connect() as c:
                c.execute(
                    "DELETE FROM watchlist WHERE snapshot_ts = ?",
                    (f"paper_state:{pos.db_id}",)
                )
                c.execute(
                    "INSERT INTO watchlist(snapshot_ts, ticker, payload_json) "
                    "VALUES (?, ?, ?)",
                    (f"paper_state:{pos.db_id}", pos.ticker, json.dumps(state))
                )
        except Exception as e:
            log.warning("Could not save paper state for %s: %s", pos.ticker, e)

    # -----------------------------------------------------------------
    # Process a new signal
    # -----------------------------------------------------------------
    def process_signal(self, signal: dict, fx_gbp_per_usd: float) -> bool:
        """
        Receive a Signal-like dict and open a paper position if eligible.

        Returns True if a paper position was opened, False otherwise.
        """
        ticker = signal["ticker"]
        if ticker in self.positions:
            log.info("Paper: %s already has open position, ignoring new signal.", ticker)
            return False
        if len(self.positions) >= self.cfg.max_open_positions:
            log.info("Paper: at max %d positions, refusing new entry %s.",
                     self.cfg.max_open_positions, ticker)
            return False

        # Use the same sizing logic as the backtest
        entry  = float(signal["entry_price"])
        stop   = float(signal["stop_loss"])
        target = float(signal["target_price"])
        tier   = int(signal.get("tier", 0))

        plan = compute_position(
            equity_gbp=self.current_equity(fx_gbp_per_usd),
            ticker=ticker,
            entry_usd=entry, stop_usd=stop, target_usd=target,
            tier=tier, fx_gbp_per_usd=fx_gbp_per_usd,
        )
        if plan is None or plan.shares <= 0:
            log.info("Paper: sizing rejected %s (no shares).", ticker)
            return False

        # Apply slippage (entry is filled higher than the close)
        slip_pct = config.FEE_MODEL.slippage_pct + (self.cfg.extra_slippage_bps / 10_000.0)
        fill_price = round(entry * (1 + slip_pct), 4)

        invested_gbp = fill_price * plan.shares * fx_gbp_per_usd
        if invested_gbp > self.cash_gbp:
            log.info("Paper: insufficient cash for %s (need £%.0f, have £%.0f).",
                     ticker, invested_gbp, self.cash_gbp)
            return False

        # Enforce absolute total-invested cap (no-leverage rule).
        cap = config.RISK_PARAMS.max_total_invested_gbp_absolute
        if cap > 0:
            currently_invested = sum(
                p.entry_usd * p.shares * p.fx_entry for p in self.positions.values()
            )
            if currently_invested + invested_gbp > cap:
                log.info(
                    "Paper: total-invested cap reached (£%.0f + £%.0f > £%.0f), "
                    "skipping %s.",
                    currently_invested, invested_gbp, cap, ticker,
                )
                return False

        # Persist to DB first so we have an id to attach to the in-memory position
        trade_id = db.insert_trade({
            "ticker":   ticker,
            "tier":     tier,
            "entry_ts": pd.Timestamp.now("UTC").tz_localize(None).isoformat(),
            "entry_usd": fill_price,
            "shares":   plan.shares,
            "fx_entry": fx_gbp_per_usd,
        }, source="paper")

        pos = PaperPosition(
            ticker=ticker, tier=tier,
            entry_ts=pd.Timestamp.now("UTC").tz_localize(None),
            entry_usd=fill_price,
            initial_stop_usd=stop,
            target_usd=target,
            shares=plan.shares,
            fx_entry=fx_gbp_per_usd,
            high_water_usd=fill_price,
            db_id=trade_id,
        )
        self.positions[ticker] = pos
        self.cash_gbp -= invested_gbp
        self._save_state(pos)

        log.info("Paper OPEN: %s tier=%d %d shares @ $%.2f stop=$%.2f target=$%.2f "
                 "→ £%.2f invested, £%.2f cash left",
                 ticker, tier, plan.shares, fill_price, stop, target,
                 invested_gbp, self.cash_gbp)
        return True

    # -----------------------------------------------------------------
    # Mark-to-market and exits
    # -----------------------------------------------------------------
    def mark_to_market(self, fx_gbp_per_usd: float) -> List[dict]:
        """
        Fetch latest bar for each open position and check for stop/target hits.

        Returns a list of {ticker, action, price, pnl_gbp, reason} dicts for
        any positions that were closed this tick.
        """
        closed: List[dict] = []
        if not self.positions:
            return closed

        for ticker in list(self.positions.keys()):
            pos = self.positions[ticker]
            try:
                df = data.get_history(ticker, start=pos.entry_ts.strftime("%Y-%m-%d"))
                if df is None or df.empty:
                    continue
                bar = df.iloc[-1]
            except Exception as e:
                log.warning("Paper m2m: could not fetch %s: %s", ticker, e)
                continue

            high  = float(bar["High"])
            low   = float(bar["Low"])
            close = float(bar["Close"])

            # Update high-water for trailing stop
            if high > pos.high_water_usd:
                pos.high_water_usd = high
                self._save_state(pos)

            # --- Check exits in same priority order as the backtest ---
            risk_per_share = max(pos.entry_usd - pos.initial_stop_usd, 1e-6)
            trail_stop = pos.high_water_usd - (
                config.RISK_PARAMS.trailing_stop_risk_mult * risk_per_share
            )
            effective_stop = max(pos.initial_stop_usd, trail_stop) \
                if self.cfg.enforce_trailing else pos.initial_stop_usd

            exit_price: Optional[float] = None
            exit_reason: Optional[str] = None
            if self.cfg.enforce_stop and low <= effective_stop:
                exit_price = effective_stop
                exit_reason = "trailing_stop" if effective_stop > pos.initial_stop_usd else "stop"
            elif self.cfg.enforce_target and high >= pos.target_usd:
                exit_price = pos.target_usd
                exit_reason = "target"

            if exit_price is not None:
                result = self._close_position(pos, exit_price, exit_reason, fx_gbp_per_usd)
                closed.append(result)

        return closed

    def _close_position(
        self, pos: PaperPosition,
        exit_price: float, reason: str, fx_exit: float,
    ) -> dict:
        """Realise P&L, update DB, free up cash."""
        # Apply slippage on exit
        slip_pct = config.FEE_MODEL.slippage_pct + (self.cfg.extra_slippage_bps / 10_000.0)
        fill_price = round(exit_price * (1 - slip_pct), 4)

        pnl_usd = (fill_price - pos.entry_usd) * pos.shares
        # Apply realistic fees: SEC + TAF on sell, FX on both legs
        fm = config.FEE_MODEL
        sec_fee_usd = fill_price * pos.shares * fm.sec_fee_pct
        taf_usd = min(pos.shares * fm.taf_per_share_usd, fm.taf_cap_usd)
        fx_fee_gbp = (
            (pos.entry_usd * pos.shares * pos.fx_entry +
             fill_price   * pos.shares * fx_exit) * fm.fx_fee_pct
        )
        pnl_gbp = (
            pnl_usd * fx_exit                    # convert P&L to GBP
            - (sec_fee_usd + taf_usd) * fx_exit  # sell fees
            - fx_fee_gbp                          # FX markup, both legs
        )
        return_pct = pnl_usd / (pos.entry_usd * pos.shares) if pos.entry_usd > 0 else 0

        # Restore proceeds to cash
        proceeds_gbp = fill_price * pos.shares * fx_exit \
            - (sec_fee_usd + taf_usd) * fx_exit \
            - (fill_price * pos.shares * fx_exit * fm.fx_fee_pct)
        self.cash_gbp += proceeds_gbp

        # Update DB
        try:
            with db.connect() as c:
                c.execute(
                    "UPDATE trades SET exit_ts = ?, exit_usd = ?, fx_exit = ?, "
                    "pnl_usd = ?, pnl_gbp = ?, return_pct = ?, reason_exit = ? "
                    "WHERE id = ?",
                    (pd.Timestamp.now("UTC").tz_localize(None).isoformat(), fill_price, fx_exit,
                     pnl_usd, pnl_gbp, return_pct, reason, pos.db_id)
                )
                # Tidy up the saved state row
                c.execute(
                    "DELETE FROM watchlist WHERE snapshot_ts = ?",
                    (f"paper_state:{pos.db_id}",)
                )
        except Exception as e:
            log.warning("Could not update DB for paper close %s: %s", pos.ticker, e)

        del self.positions[pos.ticker]
        log.info("Paper CLOSE: %s @ $%.2f reason=%s → £%.2f P&L (%.1f%%)",
                 pos.ticker, fill_price, reason, pnl_gbp, return_pct * 100)
        return {
            "ticker": pos.ticker, "action": "close",
            "price": fill_price, "pnl_gbp": pnl_gbp,
            "return_pct": return_pct, "reason": reason,
        }

    # -----------------------------------------------------------------
    # State queries
    # -----------------------------------------------------------------
    def current_equity(self, fx_gbp_per_usd: float,
                       use_intraday: bool = True) -> float:
        """Cash + unrealised P&L on open positions, in GBP.

        When `use_intraday=True` (default) we try to get a near-real-time
        quote per ticker via `data.get_quote()` — that's yfinance's
        fast_info, typically the most recent traded price with a ~15min
        delay.  Falls back to the last cached Close per position if the
        quote fails.

        When `use_intraday=False` we skip the quote entirely and use
        the last Close from the cached daily bars.  Useful for
        non-realtime contexts (daily snapshot, backtesting flows).
        """
        unrealised = 0.0
        for pos in self.positions.values():
            mark_usd: Optional[float] = None
            if use_intraday:
                mark_usd = data.get_quote(pos.ticker)
            if mark_usd is None:
                # Fallback: last cached close
                try:
                    df = data.get_history(pos.ticker,
                                          start=pos.entry_ts.strftime("%Y-%m-%d"))
                    if df is not None and not df.empty:
                        mark_usd = float(df["Close"].iloc[-1])
                except Exception:
                    mark_usd = None
            if mark_usd is not None:
                try:
                    unrealised += pos.unrealised_usd(mark_usd) * fx_gbp_per_usd
                except Exception:
                    pass
        # Add the entry-cost notional back so equity = cash + position_market_value
        # (entry cost was already deducted from cash; m2m gives us the gain/loss)
        position_value_gbp = sum(
            pos.entry_usd * pos.shares * fx_gbp_per_usd for pos in self.positions.values()
        )
        return round(self.cash_gbp + position_value_gbp + unrealised, 2)

    def record_equity_snapshot(self, fx_gbp_per_usd: float) -> None:
        """Write a daily equity row to the DB."""
        eq = self.current_equity(fx_gbp_per_usd)
        try:
            db.insert_equity(
                timestamp=pd.Timestamp.now("UTC").tz_localize(None).isoformat(),
                equity_gbp=eq,
                cash_gbp=self.cash_gbp,
                open_positions=len(self.positions),
                source="paper",
            )
        except Exception as e:
            log.warning("Could not record equity snapshot: %s", e)

    # -----------------------------------------------------------------
    # Monthly cash contributions
    # -----------------------------------------------------------------
    def apply_pending_contributions(
        self,
        now: Optional[pd.Timestamp] = None,
        cfg: Optional[config.ContributionsConfig] = None,
    ) -> float:
        """
        Credit any due-but-unrecorded contributions to `self.cash_gbp` and
        persist them via `db.insert_contribution`.

        Should be called once per day from the live engine.  Idempotent: a
        contribution date that is already in the DB is silently skipped, so
        repeated calls (or restarts during the day) won't double-pay.

        Returns the total amount credited on this call (0.0 if none).
        """
        cfg = cfg or config.CONTRIBUTIONS
        if not cfg.enabled or cfg.amount_gbp <= 0:
            return 0.0

        now = (now or pd.Timestamp.utcnow().tz_localize(None)).normalize()

        # Build the list of historical "last-Friday" dates from a generous
        # back-window through today.  Two years of look-back is more than
        # enough catch-up for a paper bot left offline for a while; limits
        # the dataset size we feed to _contribution_dates.
        from .backtest import _contribution_dates
        window_start = (now - pd.Timedelta(days=730)).normalize()
        days = pd.date_range(window_start, now, freq="D")
        # Drop weekends to mimic the trading calendar — Good-Friday-style
        # holidays are rare enough that we don't fetch the full SPY index
        # here.  If a contribution gets pushed off a Friday, the prior
        # Thursday will still satisfy the "last business day before last
        # Friday" rule once the calendar is dropped to weekdays.
        weekdays = days[days.dayofweek < 5]
        candidates = _contribution_dates(weekdays, schedule=cfg.schedule)
        if not candidates:
            return 0.0

        # Filter to dates on or before today.
        due = [d for d in candidates if d <= now]
        if not due:
            return 0.0

        already = {row["contribution_date"] for row in db.get_contributions(source="paper")}
        credited = 0.0
        for d in due:
            key = d.strftime("%Y-%m-%d")
            if key in already:
                continue
            inserted = db.insert_contribution(
                contribution_date=key, amount_gbp=cfg.amount_gbp, source="paper"
            )
            if inserted:
                self.cash_gbp += cfg.amount_gbp
                credited += cfg.amount_gbp
                log.info("Paper contribution credited: +£%.2f on %s (cash now £%.2f)",
                         cfg.amount_gbp, key, self.cash_gbp)
        return credited

    def summary(self) -> dict:
        """Performance stats for /paper_status reporting."""
        closed_trades = self._fetch_closed_trades()
        if closed_trades.empty:
            n_closed = 0
            wins = 0
            win_rate = 0.0
            total_pnl_gbp = 0.0
            avg_pnl_gbp = 0.0
            best = worst = 0.0
            tier_breakdown: dict = {}
        else:
            n_closed = len(closed_trades)
            wins = (closed_trades["pnl_gbp"] > 0).sum()
            win_rate = wins / n_closed if n_closed else 0
            total_pnl_gbp = float(closed_trades["pnl_gbp"].sum())
            avg_pnl_gbp = float(closed_trades["pnl_gbp"].mean())
            best = float(closed_trades["pnl_gbp"].max())
            worst = float(closed_trades["pnl_gbp"].min())
            tier_breakdown = self._compute_tier_breakdown(closed_trades)

        # Open-position tier counts (live, not from DB)
        open_by_tier = {1: 0, 2: 0}
        for p in self.positions.values():
            if p.tier in open_by_tier:
                open_by_tier[p.tier] += 1

        return {
            "starting_capital_gbp": self.cfg.starting_capital_gbp,
            "cash_gbp":             round(self.cash_gbp, 2),
            "open_positions":       len(self.positions),
            "open_tier1":           open_by_tier[1],
            "open_tier2":           open_by_tier[2],
            "n_closed":             int(n_closed),
            "win_rate_pct":         round(win_rate * 100, 1),
            "total_pnl_gbp":        round(total_pnl_gbp, 2),
            "avg_pnl_gbp":          round(avg_pnl_gbp, 2),
            "best_trade_gbp":       round(best, 2),
            "worst_trade_gbp":      round(worst, 2),
            # Per-tier breakdown for "are Tier 2 trades worth taking?" analysis
            "tier_breakdown":       tier_breakdown,
        }

    @staticmethod
    def _compute_tier_breakdown(closed: pd.DataFrame) -> dict:
        """
        Per-tier performance to answer "are Tier 2 trades net-positive?"
        empirically by day 60 of paper trading.
        """
        out: dict = {}
        for tier in (1, 2):
            sub = closed[closed["tier"] == tier]
            if sub.empty:
                out[tier] = {
                    "n": 0, "win_rate_pct": 0.0,
                    "total_pnl_gbp": 0.0, "avg_pnl_gbp": 0.0,
                    "expectancy_gbp": 0.0,
                }
                continue
            n = len(sub)
            wins = (sub["pnl_gbp"] > 0).sum()
            wr = wins / n if n else 0
            total = float(sub["pnl_gbp"].sum())
            avg = float(sub["pnl_gbp"].mean())
            # Expectancy = avg P&L per trade (same as avg here since equal
            # weighting; included as a separate field for clarity in the report).
            out[tier] = {
                "n": int(n),
                "win_rate_pct": round(wr * 100, 1),
                "total_pnl_gbp": round(total, 2),
                "avg_pnl_gbp": round(avg, 2),
                "expectancy_gbp": round(avg, 2),
            }
        return out

    @staticmethod
    def _fetch_closed_trades() -> pd.DataFrame:
        try:
            with db.connect() as c:
                rows = c.execute(
                    "SELECT * FROM trades WHERE source='paper' AND exit_ts IS NOT NULL "
                    "ORDER BY exit_ts DESC"
                ).fetchall()
            if not rows:
                return pd.DataFrame()
            return pd.DataFrame([dict(r) for r in rows])
        except Exception:
            return pd.DataFrame()


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
_singleton: Optional[PaperTrader] = None


def get_trader() -> PaperTrader:
    """Lazy singleton — created on first access."""
    global _singleton
    if _singleton is None:
        _singleton = PaperTrader()
    return _singleton


def reset_for_tests() -> None:
    """Clear the module-level singleton (used in tests)."""
    global _singleton
    _singleton = None
