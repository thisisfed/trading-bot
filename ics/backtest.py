"""
backtest.py
-----------
Event-driven backtester for the ICS strategy.

v2 fixes (the unrealistic-numbers issues):
  Bug A — sizing was rewritten to "fixed $2000" mode, bypassing risk rules.
          → reverted to true risk-based sizing in sizing.py.
  Bug B — backtester capped shares at 200,000 (=> $millions of notional on
          a £20k account). → removed; rely on validated sizing.
  Bug C — "target hit" registered with negative PnL because target was
          computed off yesterday's Close while entry was tomorrow's gapped Open;
          today's High often already above target. → don't check exits on the
          entry bar; first eligible exit bar is entry_bar + 1.
  Bug D — same problem with stops on entry day (intraday Low below stop
          before we even owned it). → same fix.
  Bug E — `for tkr in tickers: if tkr in positions: break`. Used `break`,
          which silently skipped the rest of the universe. → `continue`.
  Bug F — broad-market filter (SPY > HMA) had been removed in user's signals.
          → reinstated in v2 signals.py.
  Bug G — same ticker re-entered every day (UNH 5 days in a row) → cooldown
          enforced via `_last_exit_ts[ticker]`.
  Bug H — pyramid-share count blew up (100M shares). With v2 sizing those
          shares are pre-validated; backtester also re-validates against
          live equity at the moment of the add and against the absolute cap.

Robinhood UK ISA fees applied per side:
  - Buy:  FX markup on USD->GBP conversion + slippage
  - Sell: FX markup on GBP<-USD + SEC fee + TAF (per share, capped) + slippage

The backtester is event-driven on the SPY trading calendar. Each day:
  1. Update existing positions (next-day onward — never on entry day):
     check stop, target, pyramid trigger, trailing stop, mark-to-market.
  2. Look for new signals on tickers not currently held and past cooldown.
  3. Open at NEXT bar's Open * (1 + slippage); commission/fees applied.
  4. Record GBP equity (cash + MTM open positions).

All P&L is in GBP. Buy & hold benchmark VWRP.L is fetched in GBP.
"""
from __future__ import annotations

import dataclasses
from calendar import monthrange
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from . import config, data
from .signals import _add_indicators, _broad_market_ok, _evaluate_bar
from .sizing import compute_position
from .performance import summarize, vs_benchmark
from .logging_utils import get_logger

log = get_logger("ics.backtest")


# ---------------------------------------------------------------------------
# Contribution-date helper
# ---------------------------------------------------------------------------
def _contribution_dates(
    calendar: pd.DatetimeIndex,
    schedule: str = "last_friday",
) -> List[pd.Timestamp]:
    """
    Return the sorted list of timestamps in `calendar` on which a recurring
    contribution should land, given a schedule rule.

    Currently only "last_friday" is supported: for each calendar month
    represented in the index, pick the calendar-month's last Friday — and if
    that Friday isn't a trading day (e.g. Good Friday), walk back day-by-day
    within the same month to the most recent trading day before it.  This
    matches what a UK standing order would do when the nominal date is a
    bank holiday: payment lands on the previous business day, never the
    next month.

    Edge cases:
      - empty calendar → []
      - month with no trading day on or before its last Friday → skipped
        (effectively impossible in normal markets)
    """
    if schedule != "last_friday":
        raise ValueError(f"Unsupported contribution schedule: {schedule!r}")
    if len(calendar) == 0:
        return []
    cal = pd.DatetimeIndex(pd.DatetimeIndex(calendar).normalize().unique()).sort_values()
    cal_set = set(cal)
    out: List[pd.Timestamp] = []
    seen: set[Tuple[int, int]] = set()
    for ts in cal:
        ym = (int(ts.year), int(ts.month))
        if ym in seen:
            continue
        seen.add(ym)
        last_day = monthrange(ts.year, ts.month)[1]
        last_friday = pd.Timestamp(year=ts.year, month=ts.month, day=last_day)
        offset = (last_friday.weekday() - 4) % 7
        last_friday = last_friday - pd.Timedelta(days=offset)
        candidate = last_friday
        month_start = pd.Timestamp(year=ts.year, month=ts.month, day=1)
        while candidate >= month_start:
            if candidate in cal_set:
                out.append(candidate)
                break
            candidate -= pd.Timedelta(days=1)
    return sorted(out)



# ---------------------------------------------------------------------------
# Open position state
# ---------------------------------------------------------------------------
@dataclass
class OpenPosition:
    ticker: str
    tier: int
    entry_ts: pd.Timestamp           # bar AT WHICH we executed (next bar after signal)
    entry_usd: float
    initial_stop_usd: float
    stop_usd: float                  # current trailing stop
    target_usd: float
    shares: int
    pyramid_shares: int = 0
    pyramid_trigger_usd: Optional[float] = None
    pyramided: bool = False
    fx_entry: float = 1.0
    fx_pyramid: float = 1.0
    high_water_usd: float = 0.0
    avg_entry_usd: float = 0.0
    notes: str = ""
    # Sleeve identifier: "momentum" (the original convergence signal) or
    # "mr" (mean-reversion oversold-bounce).  Drives exit logic in
    # `_update_position` and bookkeeping in trade exports.
    signal_type: str = "momentum"
    # Bars since entry, exclusive of the entry bar itself.  Used by the
    # MR sleeve's time-stop.  Incremented in _update_position.
    bars_held: int = 0
    # Previous-bar close, captured at entry and updated each bar.  The MR
    # exit rule "Close > previous Close" needs the bar-before-current's
    # close, NOT yesterday's at entry — so we maintain it here.
    prev_close_usd: float = 0.0

    def total_shares(self) -> int:
        return self.shares + self.pyramid_shares


@dataclass
class BacktestResult:
    equity_gbp: pd.Series
    trades: pd.DataFrame
    signals: pd.DataFrame
    summary: Dict[str, float]
    benchmark_compare: pd.DataFrame = field(default_factory=pd.DataFrame)
    equity_dates: pd.DatetimeIndex = field(default_factory=pd.DatetimeIndex)
    # Per-bar contribution flow (positive = cash in).  Empty if disabled.
    contributions_gbp: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))


# ---------------------------------------------------------------------------
# Fee helpers (Robinhood UK ISA cost model)
# ---------------------------------------------------------------------------
def _buy_fees_gbp(notional_usd: float, fx_gbp_per_usd: float) -> float:
    """Fees for a BUY (USD -> GBP back at sell time, but on entry only FX on the cash leg)."""
    fm = config.FEE_MODEL
    fx_fee_gbp = notional_usd * fx_gbp_per_usd * fm.fx_fee_pct
    return fx_fee_gbp


def _sell_fees_gbp(notional_usd: float, shares: int, fx_gbp_per_usd: float) -> float:
    """Fees for a SELL: SEC fee + TAF (capped) + FX markup."""
    fm = config.FEE_MODEL
    sec_fee_usd = notional_usd * fm.sec_fee_pct
    taf_usd = min(shares * fm.taf_per_share_usd, fm.taf_cap_usd)
    fx_fee_gbp = notional_usd * fx_gbp_per_usd * fm.fx_fee_pct
    return (sec_fee_usd + taf_usd) * fx_gbp_per_usd + fx_fee_gbp


# ---------------------------------------------------------------------------
# Backtester
# ---------------------------------------------------------------------------
class Backtester:
    # Calendar-days of history to pre-load BEFORE `start` so that long-lookback
    # indicators (notably the 200-SMA in the regime filter and the
    # mean-reversion sleeve's trend filter) are warm by the time the main
    # loop begins.  ~300 calendar days = ~210 trading days.  Set with a
    # margin over the longest indicator lookback in the code (200-SMA).
    DEFAULT_WARMUP_DAYS = 300

    def __init__(
        self,
        tickers: List[str],
        start: str,
        end: Optional[str] = None,
        starting_capital_gbp: Optional[float] = None,
        signal_params: Optional[config.SignalParams] = None,
        risk_params: Optional[config.RiskParams] = None,
        backtest_params: Optional[config.BacktestParams] = None,
        regime_filters: Optional[config.RegimeFilters] = None,
        slippage_pct: Optional[float] = None,
        contributions: Optional[config.ContributionsConfig] = None,
        warmup_days: Optional[int] = None,
        verbose: bool = False,
    ):
        self.tickers = list(tickers)
        self.start = start
        self.end = end or pd.Timestamp.utcnow().normalize().strftime("%Y-%m-%d")
        self.starting_capital_gbp = starting_capital_gbp or config.STARTING_CAPITAL_GBP
        self.signal_params = signal_params or config.SIGNAL_PARAMS
        # Keep an immutable copy of the base risk params so we can re-derive
        # scaled caps cleanly when contributions land.
        self._base_risk_params = risk_params or config.RISK_PARAMS
        self.risk_params = self._base_risk_params
        self.bp = backtest_params or config.BACKTEST_PARAMS
        self.regime_filters = regime_filters or config.REGIME_FILTERS
        self.slippage = slippage_pct if slippage_pct is not None else config.FEE_MODEL.slippage_pct
        self.contributions_cfg = contributions or config.CONTRIBUTIONS
        self.verbose = verbose

        # Compute warmup-extended fetch start.  Indicators get history from
        # `fetch_start`; the trading loop only begins at `start`.  This was
        # added to fix the WFO-on-short-windows bug where the 200-SMA filter
        # in the MR sleeve never warmed up within a 252-day OOS window, so
        # the variant ran with zero MR signals even though the flag was on.
        self._warmup_days = (
            warmup_days if warmup_days is not None else self.DEFAULT_WARMUP_DAYS
        )
        start_ts = pd.Timestamp(start)
        fetch_start_ts = start_ts - pd.Timedelta(days=self._warmup_days)
        self._fetch_start = fetch_start_ts.strftime("%Y-%m-%d")
        self._start_ts = start_ts.normalize()

        log.info("Backtester loading %d tickers, %s -> %s (warmup from %s), capital £%.0f",
                 len(self.tickers), start, self.end, self._fetch_start,
                 self.starting_capital_gbp)
        if self.contributions_cfg.enabled:
            log.info("Contributions: £%.0f every %s (cap-scaling=%s)",
                     self.contributions_cfg.amount_gbp,
                     self.contributions_cfg.schedule.replace("_", " "),
                     self.contributions_cfg.scale_absolute_caps_with_contributions)

        # Pre-load SPY for calendar + broad market filter + RS reference.
        # Fetch from the warmup-extended date so indicators have history.
        self._spy = data.get_history(config.SPY_TICKER, start=self._fetch_start, end=self.end)
        if self._spy.empty:
            raise RuntimeError("SPY data missing — cannot backtest.")

        # Pre-load price data + indicators per ticker
        self._price: Dict[str, pd.DataFrame] = {}
        self._indicators: Dict[str, pd.DataFrame] = {}
        for t in self.tickers:
            df = data.get_history(t, start=self._fetch_start, end=self.end)
            if df.empty or len(df) < self.signal_params.hma_period_long * 2:
                continue
            self._price[t] = df
            try:
                self._indicators[t] = _add_indicators(
                    df, self._spy["Close"], self.signal_params
                )
            except Exception as e:
                log.warning("Indicator build failed for %s: %s", t, e)
                self._price.pop(t, None)

        # FX series (GBP per USD) — also fetched with warmup so position-bar
        # FX lookups never miss
        self._fx = data.get_fx_series(start=self._fetch_start, end=self.end)

        # Trading calendar = SPY business days, but RESTRICTED to bars at or
        # after `start`.  The warmup period feeds the indicators; the actual
        # backtest only runs on the requested window.
        full_calendar = self._spy.index
        self._calendar = full_calendar[full_calendar >= self._start_ts]

        # Cooldown register: (ticker, signal_type) -> last exit timestamp.
        # Keying by sleeve means a momentum cooldown on AAPL doesn't block
        # an MR entry on AAPL the next day, and vice versa.
        self._last_exit_ts: Dict[Tuple[str, str], pd.Timestamp] = {}

        # Pre-compute SPY annualized realized vol over the configured
        # lookback.  Used by vol-targeting in `_vol_scale_at`.  We compute
        # this once at construction time rather than on each lookup so
        # IS grid search (which calls _open_position thousands of times)
        # doesn't pay the cost repeatedly.
        rp = self._base_risk_params
        if rp.vol_targeting_enabled and not self._spy.empty:
            spy_returns = self._spy["Close"].pct_change()
            self._spy_realized_vol = (
                spy_returns.rolling(rp.vol_lookback_days).std()
                * np.sqrt(self.bp.trading_days_per_year)
            )
        else:
            self._spy_realized_vol = pd.Series(dtype=float)

    # ---------------------------- helpers ----------------------------
    def _vol_scale_at(self, ts: pd.Timestamp) -> float:
        """
        Return the multiplicative risk-scale factor at `ts` based on SPY
        realized vol vs the target.  Returns 1.0 when vol-targeting is
        disabled or there's not enough history yet.

        The scale formula is `target_vol / realized_vol`, clipped to
        [vol_scale_min, vol_scale_max].
        """
        rp = self._base_risk_params
        if not rp.vol_targeting_enabled or self._spy_realized_vol.empty:
            return 1.0
        idx = self._spy_realized_vol.index.searchsorted(ts, side="right") - 1
        if idx < 0:
            return 1.0
        realized = float(self._spy_realized_vol.iloc[idx])
        if not np.isfinite(realized) or realized <= 0:
            return 1.0
        scale = rp.vol_target_annualized / realized
        return float(np.clip(scale, rp.vol_scale_min, rp.vol_scale_max))
    def _fx_at(self, ts: pd.Timestamp) -> float:
        if self._fx.empty:
            return 0.79
        idx = self._fx.index.searchsorted(ts, side="right") - 1
        if idx < 0:
            return float(self._fx.iloc[0])
        v = float(self._fx.iloc[idx])
        # Sanity: clamp to plausible 5y range
        if not (0.4 <= v <= 1.5):
            return 0.79
        return v

    def _signal_at(self, ticker: str, ts: pd.Timestamp) -> Optional[dict]:
        """Read pre-computed indicator row for `ts` and produce a signal dict."""
        ind = self._indicators.get(ticker)
        if ind is None or ts not in ind.index:
            return None
        row = ind.loc[ts]
        if pd.isna(row.get("hma_long")) or pd.isna(row.get("hma_short")):
            return None

        # Regime filter — gate new entries on broad-market conditions.
        # New filter (config.REGIME_FILTERS) checks SPY 200-SMA, VIX, and
        # SPY drawdown.  Falls back to legacy HMA-only filter when disabled.
        from .regime import regime_ok
        if self.regime_filters.enabled:
            if not regime_ok(self._spy, ts, rf=self.regime_filters).ok:
                return None
        elif self.signal_params.require_spy_above_hma and not _broad_market_ok(
            self._spy, self.signal_params, ts
        ):
            return None

        p = self.signal_params

        # Weekly HMA bullish-cross filter (per-stock).  Independent of regime.
        # Three-state: True (allow), False (block), pd.NA / None (fail open).
        if p.require_weekly_hma_bullish:
            wh = row.get("weekly_hma_bullish")
            if wh is False:
                return None

        breakout = bool(row.get("breakout", False))
        flag_active = bool(row.get("flag_active", False))

        # Use the shared scorer from signals.py — single source of truth
        score, _reasons, conds = _evaluate_bar(row, breakout, flag_active, p)

        tier = 0
        if score >= p.tier1_min_conditions and (breakout or flag_active):
            tier = 1
        elif score >= p.tier2_min_conditions:
            tier = 2
        if tier == 0:
            return None

        entry = float(row["Close"])
        atrv = float(row["atr"]) if not pd.isna(row["atr"]) else entry * 0.02
        df_full = self._price[ticker]
        # recent low based on rows up to and including ts
        recent_window = df_full.loc[df_full.index <= ts]["Low"].tail(20)
        recent_low = float(recent_window.min()) if len(recent_window) else entry * 0.95

        flag_low = float(row["flag_low"]) if not pd.isna(row["flag_low"]) else np.nan
        stop_candidates = [entry - 2 * atrv, recent_low]
        if not np.isnan(flag_low):
            stop_candidates.append(flag_low * 0.995)
        valid = [s for s in stop_candidates if 0 < s < entry]
        stop = max(valid) if valid else entry - 2 * atrv
        if stop <= 0 or stop >= entry:
            return None

        mm = (float(row["measured_move_target"])
              if not pd.isna(row["measured_move_target"]) else np.nan)
        risk_per_share = max(entry - stop, atrv * 0.5)
        if not np.isnan(mm) and mm > entry:
            target = mm
        else:
            target = entry + risk_per_share * self.risk_params.target_rr_multiple
        if target <= entry:
            return None

        return {
            "ticker": ticker, "timestamp": ts, "tier": tier, "score": score,
            "entry_price": entry, "stop_loss": stop, "target_price": target,
            "atr": atrv, "rsi": float(row["rsi"]), "rs_score": float(row["rs"]),
            "breakout": breakout, "flag_active": flag_active,
            "reasons": ",".join([k for k, v in conds.items() if v]),
            "signal_type": "momentum",
        }

    def _mr_signal_at(self, ticker: str, ts: pd.Timestamp) -> Optional[dict]:
        """
        Mean-reversion oversold-bounce signal at `ts`.

        Returns a signal dict in the same shape as `_signal_at` (so the
        downstream sizing pipeline doesn't have to branch) but with
        `signal_type="mr"`, `tier=2` (sized like a Tier-2 momentum trade,
        no pyramiding), a tighter ATR-based stop, and a target equal to
        the entry plus 1× initial-risk-per-share — because MR exits are
        time/condition-driven, not target-driven, but a target field has
        to exist for the sizing math.
        """
        p = self.signal_params
        if not p.mean_reversion_enabled:
            return None
        ind = self._indicators.get(ticker)
        if ind is None or ts not in ind.index:
            return None
        row = ind.loc[ts]

        # Need yesterday's row for the "Low < prev Low" check.  Use the
        # underlying price frame, not the indicator frame, since indicators
        # might be NaN at the very start.
        df_full = self._price[ticker]
        idx = df_full.index.get_indexer([ts])[0]
        if idx <= 0:
            return None
        prev_row = df_full.iloc[idx - 1]

        from .signals import _mr_entry_at  # local import keeps top-level lean
        if not _mr_entry_at(row, prev_row, p):
            return None

        # Regime filter: MR also respects broad-market gates.  Mean-reversion
        # in a bear market is a coin flip; we want it as a sleeve that
        # benefits FROM the same uptrending regime momentum likes.
        from .regime import regime_ok
        if self.regime_filters.enabled:
            if not regime_ok(self._spy, ts, rf=self.regime_filters).ok:
                return None
        elif p.require_spy_above_hma and not _broad_market_ok(
            self._spy, p, ts
        ):
            return None

        entry = float(row["Close"])
        atrv = float(row["atr"]) if not pd.isna(row["atr"]) else entry * 0.02
        stop = entry - p.mr_atr_stop_mult * atrv
        if stop <= 0 or stop >= entry:
            return None
        risk_per_share = entry - stop
        # Target is symbolic — actual exit is condition-based — but it has
        # to be > entry for the sizing math in compute_position to accept it.
        target = entry + risk_per_share * 1.0

        return {
            "ticker": ticker, "timestamp": ts, "tier": 2, "score": 0,
            "entry_price": entry, "stop_loss": stop, "target_price": target,
            "atr": atrv, "rsi": float(row.get("mr_rsi_short", np.nan)),
            "rs_score": float(row.get("rs", 0.0) or 0.0),
            "breakout": False, "flag_active": False,
            "reasons": f"MR-RSI{p.mr_rsi_period}<{p.mr_rsi_threshold:.0f}",
            "signal_type": "mr",
        }

    # ---------------------------- entries ----------------------------
    def _scaled_risk_params(self, total_contributions_gbp: float) -> config.RiskParams:
        """
        Return a RiskParams view where the GBP-absolute caps have been scaled
        by (starting + cumulative_contributions) / starting.

        The base caps are deliberately pinned to starting capital so they
        don't compound with equity growth; contributions are NEW principal,
        not compounded gains, so the caps need to grow with them or the new
        cash will sit idle.  Toggle with
        config.CONTRIBUTIONS.scale_absolute_caps_with_contributions.
        """
        rp = self._base_risk_params
        if (
            not self.contributions_cfg.enabled
            or not self.contributions_cfg.scale_absolute_caps_with_contributions
            or total_contributions_gbp <= 0
            or self.starting_capital_gbp <= 0
        ):
            return rp
        scale = 1.0 + total_contributions_gbp / self.starting_capital_gbp
        return dataclasses.replace(
            rp,
            risk_per_trade_gbp_absolute=rp.risk_per_trade_gbp_absolute * scale,
            max_position_gbp_absolute=rp.max_position_gbp_absolute * scale,
            max_total_invested_gbp_absolute=rp.max_total_invested_gbp_absolute * scale,
        )

    def _open_position(
        self, sig: dict, equity_gbp: float, exec_ts: pd.Timestamp,
        next_open_usd: float,
    ) -> Tuple[Optional[OpenPosition], float, Optional[dict]]:
        """Open a position at `exec_ts` using `next_open_usd * (1+slip)` as fill."""
        fx = self._fx_at(exec_ts)
        entry_usd = next_open_usd * (1.0 + self.slippage)
        # Defensive: stops set on yesterday's close may be invalid relative to
        # today's gapped Open. If the gap puts entry below or right at stop, skip.
        stop_usd = sig["stop_loss"]
        target_usd = sig["target_price"]
        if entry_usd <= stop_usd:
            return None, 0.0, None
        if target_usd <= entry_usd:
            return None, 0.0, None
        # Also: if the gap is too wide, the original 1% risk gets distorted.
        # Re-derive risk_per_share using the slipped entry.
        # (compute_position handles the rest of validation.)

        # Vol-targeting: scale the risk percentage and the absolute GBP cap
        # by (target_vol / realized_SPY_vol).  Clipped in _vol_scale_at.
        # The notional caps (max_position_gbp_absolute and
        # max_total_invested_gbp_absolute) are NOT scaled by vol — those are
        # capital-based, not risk-based, so they stay anchored.
        vol_scale = self._vol_scale_at(exec_ts)

        # MR sleeve uses a smaller risk-per-trade than momentum.  Override
        # the rate AND the absolute cap; vol-targeting then multiplies them.
        is_mr = sig.get("signal_type") == "mr"
        if is_mr:
            base_pct = self.signal_params.mr_risk_pct
            base_abs = (
                self.risk_params.risk_per_trade_gbp_absolute
                * (base_pct / max(self.risk_params.risk_per_trade_pct, 1e-9))
            )
            sizing_rp = dataclasses.replace(
                self.risk_params,
                risk_per_trade_pct=base_pct * vol_scale,
                risk_per_trade_gbp_absolute=base_abs * vol_scale,
            )
        elif vol_scale != 1.0:
            sizing_rp = dataclasses.replace(
                self.risk_params,
                risk_per_trade_pct=self.risk_params.risk_per_trade_pct * vol_scale,
                risk_per_trade_gbp_absolute=(
                    self.risk_params.risk_per_trade_gbp_absolute * vol_scale
                ),
            )
        else:
            sizing_rp = self.risk_params

        # MR is always a single tranche — never pyramid.  Force tier=2
        # into compute_position so the pyramid plan branch is skipped.
        plan_tier = 2 if is_mr else sig["tier"]

        plan = compute_position(
            equity_gbp=equity_gbp, ticker=sig["ticker"],
            entry_usd=entry_usd, stop_usd=stop_usd, target_usd=target_usd,
            tier=plan_tier, fx_gbp_per_usd=fx,
            risk_params=sizing_rp,
        )
        if plan is None or plan.shares <= 0:
            return None, 0.0, None

        notional_usd = plan.shares * entry_usd
        cost_gbp = notional_usd * fx
        buy_fees_gbp = _buy_fees_gbp(notional_usd, fx)
        cash_used = cost_gbp + buy_fees_gbp

        if cash_used > equity_gbp:
            # Not enough free cash — also defensive (sizing usually prevents this)
            return None, 0.0, None

        pos = OpenPosition(
            ticker=sig["ticker"], tier=sig["tier"], entry_ts=exec_ts,
            entry_usd=entry_usd, initial_stop_usd=stop_usd, stop_usd=stop_usd,
            target_usd=target_usd, shares=plan.shares,
            pyramid_trigger_usd=None if is_mr else plan.pyramid_trigger_usd,
            fx_entry=fx, fx_pyramid=fx,
            high_water_usd=entry_usd, avg_entry_usd=entry_usd, notes=plan.notes,
            signal_type=sig.get("signal_type", "momentum"),
            bars_held=0,
            prev_close_usd=entry_usd,  # set below to actual prev close
        )
        # Capture previous-bar close for the MR exit rule "Close > prev Close".
        # On the entry bar, we don't have the EXEC bar's prior close yet
        # (entry happens at exec_ts open), so use the bar BEFORE exec_ts.
        df_full = self._price.get(sig["ticker"])
        if df_full is not None:
            iidx = df_full.index.get_indexer([exec_ts])[0]
            if iidx > 0:
                pos.prev_close_usd = float(df_full["Close"].iloc[iidx - 1])

        sig_log = dict(sig)
        sig_log["executed_entry_usd"] = entry_usd
        sig_log["shares"] = plan.shares
        sig_log["risk_gbp"] = plan.risk_gbp
        sig_log["fx_entry"] = fx
        sig_log["vol_scale"] = vol_scale
        return pos, cash_used, sig_log

    # ---------------------------- per-bar update ----------------------------
    def _update_position(
        self, pos: OpenPosition, bar: pd.Series, ts: pd.Timestamp,
        equity_gbp_for_pyramid: float,
    ) -> Optional[Tuple[float, str]]:
        """
        Run on bars STRICTLY AFTER entry_ts.
        Returns (exit_price_usd, reason) if the position should close, else None.
        Mutates pos in place for trailing stop / pyramid additions.
        """
        # NEVER process the entry bar itself — fixes Bugs C & D
        if ts <= pos.entry_ts:
            return None

        high = float(bar["High"])
        low = float(bar["Low"])
        close = float(bar["Close"])
        pos.high_water_usd = max(pos.high_water_usd, high)
        pos.bars_held += 1

        # Mean-reversion sleeve: exit logic is condition-driven, not
        # trailing-stop-driven.  Three exit conditions; first one wins:
        #   1. Hard ATR stop (set at entry — same field as momentum stop)
        #   2. Time stop after `mr_max_holding_days` bars
        #   3. "Close > previous Close" — green close, take the bounce
        # The hard stop has priority over target-style exits, matching
        # the momentum branch's conservative-on-intraday-conflict rule.
        if pos.signal_type == "mr":
            p = self.signal_params
            if low <= pos.stop_usd:
                exit_price = pos.stop_usd * (1.0 - self.slippage)
                pos.prev_close_usd = close
                return exit_price, "mr_stop"
            if pos.bars_held >= p.mr_max_holding_days:
                exit_price = close * (1.0 - self.slippage)
                pos.prev_close_usd = close
                return exit_price, "mr_time_stop"
            if close > pos.prev_close_usd:
                exit_price = close * (1.0 - self.slippage)
                pos.prev_close_usd = close
                return exit_price, "mr_target"
            # No exit — update prev_close for tomorrow and bail.
            pos.prev_close_usd = close
            return None

        # ---------- Momentum sleeve (pyramid + trailing stop) ----------
        # Pyramid (Tier 1, only once)
        if pos.tier == 1 and not pos.pyramided and pos.pyramid_trigger_usd is not None:
            if high >= pos.pyramid_trigger_usd:
                add_price = pos.pyramid_trigger_usd * (1.0 + self.slippage)
                fx_now = self._fx_at(ts)
                rp = self.risk_params

                # Re-derive add size from CURRENT equity (not stale at-open equity).
                # BUT cap every equity-derived quantity to an absolute GBP ceiling
                # anchored to starting capital — same fix as the initial sizing.
                # Without this, pyramid risk scales linearly with the equity curve:
                # a strategy that compounds from £30k to £1M would pyramid 33× bigger
                # than it should, producing 5,000-share adds on a retail account.
                add_risk_per_share_usd = max(add_price - pos.entry_usd, 1e-6)
                add_risk_per_share_gbp = add_risk_per_share_usd * fx_now

                # Pyramid risk budget — capped absolutely
                already_risked_gbp = pos.shares * (pos.entry_usd - pos.initial_stop_usd) * pos.fx_entry
                max_total_risk_gbp = equity_gbp_for_pyramid * rp.max_total_risk_pct_per_ticker
                if rp.risk_per_trade_gbp_absolute > 0:
                    # Total ticker risk = base + add, both portions share the cap
                    max_total_risk_gbp = min(
                        max_total_risk_gbp,
                        rp.risk_per_trade_gbp_absolute * 2,  # 2× allows for the add
                    )
                remaining_total_risk_gbp = max(max_total_risk_gbp - already_risked_gbp, 0.0)

                add_budget_gbp = equity_gbp_for_pyramid * rp.pyramid_add_risk_pct
                if rp.risk_per_trade_gbp_absolute > 0:
                    add_budget_gbp = min(add_budget_gbp, rp.risk_per_trade_gbp_absolute)
                add_budget_gbp = min(add_budget_gbp, remaining_total_risk_gbp)

                if add_risk_per_share_gbp > 0 and add_budget_gbp > 0:
                    add_shares = int(np.floor(add_budget_gbp / add_risk_per_share_gbp))
                else:
                    add_shares = 0

                # Notional cap — also anchored absolutely
                max_notional_gbp = equity_gbp_for_pyramid * rp.max_position_pct_of_equity
                if rp.max_position_gbp_absolute > 0:
                    max_notional_gbp = min(max_notional_gbp, rp.max_position_gbp_absolute)
                current_notional_gbp = pos.shares * pos.entry_usd * pos.fx_entry
                room_gbp = max(max_notional_gbp - current_notional_gbp, 0.0)
                add_notional_per_share_gbp = add_price * fx_now
                if add_notional_per_share_gbp > 0:
                    max_by_notional = int(np.floor(room_gbp / add_notional_per_share_gbp))
                    add_shares = min(add_shares, max_by_notional)
                add_shares = max(0, min(add_shares, rp.abs_max_shares))

                if add_shares > 0:
                    pos.pyramid_shares = add_shares
                    pos.pyramided = True
                    pos.fx_pyramid = fx_now
                    pos.stop_usd = max(pos.stop_usd, pos.entry_usd)  # raise to BE
                    total = pos.shares + pos.pyramid_shares
                    pos.avg_entry_usd = (
                        pos.entry_usd * pos.shares + add_price * pos.pyramid_shares
                    ) / total
                    if self.verbose:
                        log.debug("PYRAMID %s: +%d sh @ $%.2f (fx=%.4f)",
                                  pos.ticker, add_shares, add_price, fx_now)

        # Trailing stop: high_water - trailing_stop_risk_mult * initial_risk_per_share
        risk_per_share = max(pos.entry_usd - pos.initial_stop_usd, 1e-6)
        trail_stop = pos.high_water_usd - self.risk_params.trailing_stop_risk_mult * risk_per_share
        if trail_stop > pos.stop_usd:
            pos.stop_usd = trail_stop

        # Stop has priority over target if both fire intraday (conservative).
        if low <= pos.stop_usd:
            exit_price = pos.stop_usd * (1.0 - self.slippage)
            return exit_price, "stop"
        if high >= pos.target_usd:
            exit_price = pos.target_usd * (1.0 - self.slippage)
            return exit_price, "target"
        return None

    # ---------------------------- mark to market ----------------------------
    def _mark_to_market_gbp(self, positions: Dict[str, OpenPosition],
                            ts: pd.Timestamp) -> float:
        if not positions:
            return 0.0
        fx = self._fx_at(ts)
        total = 0.0
        for tkr, pos in positions.items():
            df = self._price.get(tkr)
            if df is None:
                continue
            sub = df["Close"].loc[df.index <= ts]
            if sub.empty:
                continue
            px = float(sub.iloc[-1])
            total += pos.total_shares() * px * fx
        return total

    # ---------------------------- main loop ----------------------------
    def run(self) -> BacktestResult:
        cash_gbp = self.starting_capital_gbp
        equity_curve: Dict[pd.Timestamp, float] = {}
        positions: Dict[str, OpenPosition] = {}
        trades: List[dict] = []
        signals_log: List[dict] = []

        cal = self._calendar
        if len(cal) == 0:
            raise RuntimeError("Empty trading calendar.")
        rp = self.risk_params

        # Pre-compute contribution dates (last Friday of each month, with
        # holiday fallback to the prior trading day).  Empty if disabled.
        contrib_dates_set: set[pd.Timestamp] = set()
        if self.contributions_cfg.enabled and self.contributions_cfg.amount_gbp > 0:
            contrib_dates_set = set(
                _contribution_dates(cal, schedule=self.contributions_cfg.schedule)
            )
            log.info("Contributions schedule: %d dates over the calendar.",
                     len(contrib_dates_set))
        contributions_log: Dict[pd.Timestamp, float] = {}
        total_contributions_gbp = 0.0

        for ts in cal:
            # ------------- 0) Apply contribution if due -------------
            if ts in contrib_dates_set:
                amt = float(self.contributions_cfg.amount_gbp)
                cash_gbp += amt
                total_contributions_gbp += amt
                contributions_log[ts] = amt
                # Re-scale the absolute caps to reflect the new capital basis.
                self.risk_params = self._scaled_risk_params(total_contributions_gbp)
                rp = self.risk_params  # local alias used below
                if self.verbose:
                    log.debug("Contribution +£%.2f on %s (total £%.2f)",
                              amt, ts.date(), total_contributions_gbp)

            # ------------- 1) Update existing positions -------------
            mtm_for_pyramid = self._mark_to_market_gbp(positions, ts) + cash_gbp
            to_close: List[Tuple[str, float, str]] = []
            for tkr, pos in positions.items():
                df = self._price.get(tkr)
                if df is None or ts not in df.index:
                    continue
                bar = df.loc[ts]
                exit_info = self._update_position(pos, bar, ts, mtm_for_pyramid)
                if exit_info is not None:
                    to_close.append((tkr, exit_info[0], exit_info[1]))

            # Process exits
            for tkr, exit_price, reason in to_close:
                pos = positions.pop(tkr)
                fx_exit = self._fx_at(ts)
                shares = pos.total_shares()
                proceeds_usd = shares * exit_price
                proceeds_gbp = proceeds_usd * fx_exit
                sell_fees = _sell_fees_gbp(proceeds_usd, shares, fx_exit)
                cash_gbp += proceeds_gbp - sell_fees

                cost_basis_gbp = (
                    pos.shares * pos.entry_usd * pos.fx_entry
                    + pos.pyramid_shares * (pos.pyramid_trigger_usd or pos.entry_usd) * pos.fx_pyramid
                )
                # We don't capture buy fees in cost_basis here for the trade row
                # (they were paid out of cash at entry). PnL net of all fees:
                pnl_gbp = (proceeds_gbp - sell_fees) - cost_basis_gbp
                pnl_usd = shares * (exit_price - pos.avg_entry_usd)
                ret_pct = pnl_gbp / cost_basis_gbp if cost_basis_gbp > 0 else 0.0

                trades.append(dict(
                    ticker=tkr, tier=pos.tier,
                    entry_ts=pos.entry_ts.isoformat(), exit_ts=ts.isoformat(),
                    entry_usd=pos.entry_usd, exit_usd=exit_price,
                    shares=pos.shares, pyramid_shares=pos.pyramid_shares,
                    fx_entry=pos.fx_entry, fx_exit=fx_exit,
                    pnl_usd=pnl_usd, pnl_gbp=pnl_gbp, return_pct=ret_pct,
                    reason_exit=reason,
                    signal_type=pos.signal_type,
                ))
                self._last_exit_ts[(tkr, pos.signal_type)] = ts

            # ------------- 2) Look for new entries -------------
            # Pre-count current MR positions for the per-sleeve cap.
            mr_open = sum(1 for p in positions.values() if p.signal_type == "mr")
            mr_cap = self.signal_params.mr_max_concurrent
            mr_enabled = self.signal_params.mean_reversion_enabled

            if len(positions) < rp.max_open_positions:
                equity_for_size = self._mark_to_market_gbp(positions, ts) + cash_gbp
                for tkr in self.tickers:
                    if len(positions) >= rp.max_open_positions:
                        break  # full — stop scanning
                    if tkr in positions:
                        continue  # already holding — skip but keep scanning

                    df = self._price.get(tkr)
                    if df is None or ts not in df.index:
                        continue

                    # Try momentum first.  If it fires and is past its
                    # per-sleeve cooldown, take it.  Otherwise consider MR.
                    sig = None
                    chosen_type = None

                    mom_sig = self._signal_at(tkr, ts)
                    if mom_sig is not None:
                        last_exit = self._last_exit_ts.get((tkr, "momentum"))
                        if last_exit is not None:
                            bars_since = len(cal[(cal > last_exit) & (cal <= ts)])
                            if bars_since < rp.cooldown_days_after_exit:
                                mom_sig = None
                    if mom_sig is not None:
                        sig = mom_sig
                        chosen_type = "momentum"
                    elif mr_enabled and mr_open < mr_cap:
                        mr_sig = self._mr_signal_at(tkr, ts)
                        if mr_sig is not None:
                            last_exit = self._last_exit_ts.get((tkr, "mr"))
                            if last_exit is not None:
                                bars_since = len(cal[(cal > last_exit) & (cal <= ts)])
                                if bars_since < rp.cooldown_days_after_exit:
                                    mr_sig = None
                        if mr_sig is not None:
                            sig = mr_sig
                            chosen_type = "mr"

                    if sig is None:
                        continue

                    signals_log.append(dict(sig, executed=False))
                    pos_idx = df.index.get_indexer([ts])[0]
                    if pos_idx < 0 or pos_idx + 1 >= len(df):
                        continue
                    next_open = float(df["Open"].iloc[pos_idx + 1])
                    exec_ts = df.index[pos_idx + 1]
                    pos, cash_used, sig_log = self._open_position(
                        sig, equity_for_size, exec_ts, next_open
                    )
                    if pos is None or cash_used > cash_gbp:
                        continue

                    # Enforce ABSOLUTE total-invested cap.  An ISA can't lever:
                    # if cash + currently invested + new entry would exceed the
                    # absolute cap, refuse the entry.  Without this the strategy
                    # can compound notional well beyond its starting capital.
                    cap = self.risk_params.max_total_invested_gbp_absolute
                    if cap > 0:
                        currently_invested_gbp = sum(
                            p.entry_usd * p.shares * p.fx_entry
                            for p in positions.values()
                        )
                        if currently_invested_gbp + cash_used > cap:
                            continue

                    cash_gbp -= cash_used
                    positions[tkr] = pos
                    if chosen_type == "mr":
                        mr_open += 1
                    if sig_log:
                        signals_log[-1].update(sig_log)
                        signals_log[-1]["executed"] = True

            # ------------- 3) Record equity -------------
            equity_curve[ts] = self._mark_to_market_gbp(positions, ts) + cash_gbp

        # ------------- Final close-out -------------
        last_ts = cal[-1]
        for tkr in list(positions.keys()):
            pos = positions.pop(tkr)
            df = self._price.get(tkr)
            if df is None:
                continue
            sub_close = df["Close"].loc[df.index <= last_ts]
            if sub_close.empty:
                continue
            last_close = float(sub_close.iloc[-1])
            fx_exit = self._fx_at(last_ts)
            shares = pos.total_shares()
            proceeds_usd = shares * last_close
            proceeds_gbp = proceeds_usd * fx_exit
            sell_fees = _sell_fees_gbp(proceeds_usd, shares, fx_exit)
            cash_gbp += proceeds_gbp - sell_fees

            cost_basis_gbp = (
                pos.shares * pos.entry_usd * pos.fx_entry
                + pos.pyramid_shares * (pos.pyramid_trigger_usd or pos.entry_usd) * pos.fx_pyramid
            )
            pnl_gbp = (proceeds_gbp - sell_fees) - cost_basis_gbp
            pnl_usd = shares * (last_close - pos.avg_entry_usd)
            ret_pct = pnl_gbp / cost_basis_gbp if cost_basis_gbp > 0 else 0.0
            trades.append(dict(
                ticker=tkr, tier=pos.tier,
                entry_ts=pos.entry_ts.isoformat(), exit_ts=last_ts.isoformat(),
                entry_usd=pos.entry_usd, exit_usd=last_close,
                shares=pos.shares, pyramid_shares=pos.pyramid_shares,
                fx_entry=pos.fx_entry, fx_exit=fx_exit,
                pnl_usd=pnl_usd, pnl_gbp=pnl_gbp, return_pct=ret_pct,
                reason_exit="end_of_backtest",
                signal_type=pos.signal_type,
            ))
            equity_curve[last_ts] = cash_gbp

        # ------------- Build outputs -------------
        eq_series = pd.Series(equity_curve).sort_index()
        eq_series.name = "equity_gbp"
        trades_df = pd.DataFrame(trades)
        signals_df = pd.DataFrame(signals_log)

        if contributions_log:
            contrib_series = pd.Series(contributions_log).sort_index()
            contrib_series.name = "contribution_gbp"
        else:
            contrib_series = pd.Series(dtype=float, name="contribution_gbp")

        bench = data.get_history(config.BENCHMARK_TICKER, start=self.start, end=self.end)
        bench_close = bench["Close"] if not bench.empty else pd.Series(dtype=float)
        bench_compare = vs_benchmark(eq_series, bench_close, self.starting_capital_gbp)

        summary = summarize(eq_series, trades_df)

        # Money-weighted return helpers when contributions are in play.
        # `total_return_pct` and `cagr_pct` from `summarize` are computed off
        # the raw equity series, so they over-state performance by treating
        # contributions as if they were trading gains.  Patch them with
        # contribution-aware metrics.
        if total_contributions_gbp > 0:
            end_eq = float(eq_series.iloc[-1])
            net_pnl_gbp = end_eq - self.starting_capital_gbp - total_contributions_gbp
            avg_capital_basis = self.starting_capital_gbp + 0.5 * total_contributions_gbp
            summary["total_contributions_gbp"] = total_contributions_gbp
            summary["net_pnl_gbp_after_contributions"] = net_pnl_gbp
            # Simple money-weighted return: net pnl divided by the
            # *time-average* capital invested.  Treats every contribution
            # as if it sat on the account for half the period — a decent
            # first-order approximation that doesn't require numpy_financial
            # or solving an IRR equation.  For a sharper number, use the
            # `equity_gbp` and `contribution_gbp` series which are saved
            # to the report folder.
            if avg_capital_basis > 0:
                summary["money_weighted_return_pct"] = net_pnl_gbp / avg_capital_basis
            else:
                summary["money_weighted_return_pct"] = 0.0
            # Replace the misleading raw total_return_pct with the
            # contribution-aware version (keeps the field name compatible
            # for existing reporters but with corrected semantics when
            # contributions are present).
            summary["raw_equity_total_return_pct"] = summary.get("total_return_pct", 0.0)
            summary["total_return_pct"] = summary["money_weighted_return_pct"]
        else:
            summary["total_contributions_gbp"] = 0.0
            summary["net_pnl_gbp_after_contributions"] = (
                float(eq_series.iloc[-1]) - self.starting_capital_gbp
                if not eq_series.empty else 0.0
            )

        if not bench_compare.empty:
            summary["benchmark_total_return_pct"] = float(
                bench_compare["benchmark_gbp"].iloc[-1] / self.starting_capital_gbp - 1.0
            )
            summary["alpha_total_pct"] = (
                summary["total_return_pct"] - summary["benchmark_total_return_pct"]
            )
        else:
            summary["benchmark_total_return_pct"] = 0.0
            summary["alpha_total_pct"] = summary.get("total_return_pct", 0.0)

        log.info(
            "Backtest done: trades=%d, end £%.0f, contrib £%.0f, net pnl £%.0f, "
            "MDD %.2f%%, Sharpe %.2f",
            summary["n_trades"], summary["end_equity_gbp"],
            summary["total_contributions_gbp"],
            summary["net_pnl_gbp_after_contributions"],
            summary["max_drawdown_pct"] * 100, summary["sharpe"],
        )
        return BacktestResult(
            equity_gbp=eq_series, trades=trades_df, signals=signals_df,
            summary=summary, benchmark_compare=bench_compare,
            equity_dates=eq_series.index, contributions_gbp=contrib_series,
        )
