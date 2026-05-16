"""
config.py
---------
Central configuration. All tunables live here. Sensitive values are in .env.

v2 changes vs v1:
- STARTING_CAPITAL_GBP = 30000 (was 20000)
- Realistic Robinhood UK ISA cost model: per-trade SEC/TAF fees + FX markup
- Hardened validation defaults (no ad-hoc tightening required)
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List

from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"
DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

WATCHLIST_CSV = DATA_DIR / "watchlist.csv"
DB_PATH = DATA_DIR / "ics.db"
LOG_FILE = LOG_DIR / "ics.log"
PRICE_CACHE_DIR = DATA_DIR / "price_cache"
PRICE_CACHE_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Environment / secrets
# ---------------------------------------------------------------------------
load_dotenv(PROJECT_ROOT / ".env")

TELEGRAM_TOKEN: str | None = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID: str | None = os.getenv("TELEGRAM_CHAT_ID")

# ---------------------------------------------------------------------------
# Capital / FX / benchmark
# ---------------------------------------------------------------------------
STARTING_CAPITAL_GBP: float = 30_000.0     # ← £30,000 ISA
BENCHMARK_TICKER: str = "VWRP.L"           # Vanguard FTSE All-World UCITS ETF in GBP — the ETF you'd actually buy
                                            # in a UK ISA as the "do nothing" alternative.
FX_TICKER: str = "GBPUSD=X"                # for converting USD prices/PnL to GBP
SPY_TICKER: str = "SPY"                    # broad market filter
QQQ_TICKER: str = "QQQ"

# ---------------------------------------------------------------------------
# Robinhood UK ISA cost model
# Per actual Robinhood UK ISA terms (2024–25):
#   - Commission per trade:          £0.00 (commission-free)
#   - SEC fee (sells only):          0.0027% of notional  (~$27.80 per $1M)
#   - TAF / FINRA fee (sells only):  $0.000166 per share, capped $8.30 per trade
#   - FX conversion markup:          0.03% on each USD <-> GBP conversion
# We model these as percentages applied per-side. Buys: only FX. Sells: SEC + FX.
# (TAF is per share — folded into a small effective % since it's tiny for our sizes.)
# ---------------------------------------------------------------------------
@dataclass
class FeeModel:
    sec_fee_pct: float = 0.0000270       # 0.0027% on sells only (USD notional)
    fx_fee_pct: float = 0.0003           # 0.03% on each USD<->GBP conversion (each side)
    taf_per_share_usd: float = 0.000166  # capped at $8.30 per execution
    taf_cap_usd: float = 8.30
    slippage_pct: float = 0.0010         # market microstructure slip (10 bps each side)

FEE_MODEL = FeeModel()

# ---------------------------------------------------------------------------
# Base universe — comprehensive hardcoded list of US tech / AI / momentum.
# The watchlist refresher filters this down by liquidity, RS, etc.
# ---------------------------------------------------------------------------
BASE_UNIVERSE: list[str] = [
    # ==================== SEMICONDUCTORS & HARDWARE ====================
    "NVDA", "AMD", "AVGO", "TSM", "ASML", "AMAT", "LRCX", "KLAC", "MU", "QCOM",
    "MRVL", "ADI", "TXN", "NXPI", "MPWR", "CRDO", "SMCI", "ANET", "VRT", "ARM",
    "INTC", "ON", "MCHP", "WOLF", "RGTI", "IONQ", "SOUN", "TER", "ENTG", "COHR",

    # ==================== SOFTWARE & CLOUD ====================
    "MSFT", "GOOGL", "AMZN", "META", "NOW", "CRWD", "PANW", "SNOW", "DDOG", "MDB",
    "TEAM", "HUBS", "CDNS", "WDAY", "ZS", "NET", "ESTC", "GTLB", "MNDY", "APP",
    "BILL", "OKTA", "TENB", "S", "AI", "RXRX", "CFLT", "CIEN", "ALGM", "PLTR",
    "ADBE", "CRM", "ORCL", "PATH", "UPST", "SYM", "TOST", "RBLX", "U", "ZM",

    # ==================== CONSUMER / INTERNET / ENTERTAINMENT ====================
    "NFLX", "SPOT", "DASH", "ABNB", "SHOP", "MELI", "DKNG", "PINS", "SNAP", "RDDT",
    "DUOL", "MTCH", "CHWY", "TSLA", "CVNA", "LCID", "RIVN", "JOBY", "ACHR", "ASTS",
    "LUNR", "RKLB", "OPEN", "HOOD", "COIN", "SOFI", "AFRM", "PYPL",

    # ==================== FINTECH & FINANCIAL ====================
    "MA", "V", "BKNG", "AXP", "GS", "MS", "SCHW", "JPM",

    # ==================== HEALTHCARE & BIOTECH ====================
    "LLY", "VRTX", "NVO", "REGN", "ISRG", "UNH", "MRNA", "DXCM", "MDT", "TMO",
    "ABT", "PFE", "JNJ", "MRK", "DHR", "SRPT", "REPL",

    # ==================== INDUSTRIALS / AEROSPACE / DEFENSE ====================
    "GEV", "PWR", "ETN", "HON", "RTX", "KTOS", "CAT", "FIX", "CSX", "UNP", "FDX",
    "LMT", "GD", "NOC", "BA", "DE", "EMR",

    # ==================== ENERGY / UTILITIES (AI Power Theme) ====================
    "CEG", "VST", "NRG", "IREN", "MARA", "RIOT", "CLSK", "WULF", "XOM", "CVX",
    "COP", "EOG", "FANG", "OXY",

    # ==================== MATERIALS & COMMODITIES ====================
    "AEM", "NEM", "GOLD", "NUE", "MLI", "FCX", "SCCO",

    # ==================== MEME / HIGH-VOLATILITY / SPECULATIVE ====================
    "GME", "AMC", "BB", "BBAI", "BYND", "POET",
    # NOTE: NVDA / TSLA / PLTR / SOUN are intentionally NOT duplicated here;
    # they already appear in the semis or consumer/internet sections above.
]
# Deduplicate while preserving first-seen order, in case anyone edits the lists
# above and accidentally re-introduces a clash.
BASE_UNIVERSE = list(dict.fromkeys(BASE_UNIVERSE))

# ---------------------------------------------------------------------------
# Watchlist refresh filters
# ---------------------------------------------------------------------------
@dataclass
class WatchlistFilters:
    min_market_cap_usd: float = 2_000_000_000
    min_price_usd: float = 5.0
    min_avg_daily_volume: float = 3_000_000
    rs_lookback_days: int = 21
    rs_reference: str = "SPY"
    sma_long_period: int = 200
    require_close_above_sma: bool = True
    vol_short_window: int = 20
    vol_long_window: int = 60
    vol_short_long_ratio: float = 0.85
    pct_below_52w_high: float = 0.35
    history_days_for_filters: int = 260

WATCHLIST_FILTERS = WatchlistFilters()

# ---------------------------------------------------------------------------
# Watchlist universe source — controls what feeds into the watchlist refresh
#
# v2.5 change: default switched from the hand-curated BASE_UNIVERSE to
# point-in-time NDX membership.  BASE_UNIVERSE is a snapshot of 2024-2025
# momentum winners; using it as the live candidate pool means the bot is
# always trading what worked recently, which is forward-looking survivorship
# bias.  PIT-NDX matches what the WFO validates against and refreshes as
# index membership changes.
#
# Modes:
#   "ndx_pit"      — current-date NDX membership via the PIT library.
#                    Requires nasdaq_100_ticker_history installed; falls
#                    back to BASE_UNIVERSE with a loud warning if not.
#   "base"         — the legacy BASE_UNIVERSE list (kept for diagnostics
#                    and for the comparison harness).
#
# `min_dollar_volume_usd` adds an extra liquidity floor on top of the
# WatchlistFilters check.  60-day median dollar volume must exceed this
# to be considered (default $50M — enough liquidity that £30k positions
# don't move the price).
# ---------------------------------------------------------------------------
@dataclass
class UniverseSource:
    mode: str = "ndx_pit"
    min_dollar_volume_usd: float = 50_000_000

UNIVERSE_SOURCE = UniverseSource()

# ---------------------------------------------------------------------------
# Signal generation parameters
# ---------------------------------------------------------------------------
@dataclass
class SignalParams:
    hma_period_long: int = 55
    hma_period_short: int = 20
    rsi_period: int = 14
    rsi_min: float = 55.0
    rsi_max: float = 75.0
    vol_confirm_window: int = 20
    vol_confirm_mult: float = 1.5
    rs_lookback_days: int = 21
    flag_lookback_bars: int = 15
    pole_lookback_bars: int = 20
    flag_max_range_pct_of_pole: float = 0.30
    pole_min_gain_pct: float = 0.15
    breakout_buffer_pct: float = 0.001
    tier1_min_conditions: int = 4
    tier2_min_conditions: int = 3   # (was 2 in v1; tightened)
    require_spy_above_hma: bool = True

    # Minimum target gain to consider a signal worth showing/trading.
    # Anything below this is almost certainly a degenerate output — typically
    # a bull-flag measured-move computed against a near-zero pole height —
    # and would never clear fees + slippage even if it hit.  Default 2%.
    # Set to 0.0 to disable this filter.
    min_target_gain_pct: float = 0.02
    # Minimum reward-to-risk ratio.  This is intentionally LOW (0.3) — its
    # job is only to catch degenerate signals like target=$16.10 stop=$14.74
    # entry=$16.09 (R/R = 0.01).  Many WFO-validated signals legitimately
    # come in below 1:1 R/R because of wider stops on volatile names; we
    # don't want to filter those out.  Set to 0.0 to disable.
    min_reward_risk_ratio: float = 0.3

    # ----- Weekly HMA bullish-cross filter (per-stock) -----------------------
    # DEPRECATED: stability analysis (multi_wfo run, May 2026) showed this
    # parameter to be NOISE — picked True 46% / False 54% across pooled
    # windows, indistinguishable from random.  Default False; keep the code
    # path so you can still test it on different universes / regimes, but
    # do not turn on without a fresh stability check.
    require_weekly_hma_bullish: bool = False
    weekly_hma_short_period: int = 4    # ~1 month of weekly bars
    weekly_hma_long_period: int = 13    # ~1 quarter of weekly bars

    # ----- Mean-reversion sleeve -------------------------------------------
    # An auxiliary signal family for short-term oversold bounces in
    # established uptrends.  Designed to be uncorrelated with the momentum
    # / breakout signals above: when momentum signals fire in low-vol
    # trending tape, MR signals fire on intra-trend pullbacks.
    #
    # MR entry conditions (all required, no scoring):
    #   - Stock close > SMA(200) — uptrend filter (catches pullbacks not
    #     falling knives)
    #   - RSI(`mr_rsi_period`) < `mr_rsi_threshold` — oversold trigger
    #   - Today's low < yesterday's low — confirmed selling pressure
    #   - Today's close > today's open — sign of intraday reversal
    #
    # MR exit logic (whichever fires first):
    #   - Target: today's close > yesterday's close (any green close)
    #   - Time stop: held > `mr_max_holding_days` bars
    #   - Hard stop: `mr_atr_stop_mult` × ATR(14) below entry
    #
    # MR positions count against the global `max_open_positions` cap, with
    # an additional per-sleeve cap of `mr_max_concurrent`.  Risk per trade
    # is `mr_risk_pct` (smaller than the 0.75% momentum default because
    # MR has shorter hold and lower edge per trade).
    #
    # Default OFF.  Turn on via WFO grid-search and only merge if OOS
    # Sharpe AND OOS Calmar both improve in ≥5/8 windows.
    mean_reversion_enabled: bool = False
    mr_rsi_period: int = 2
    mr_rsi_threshold: float = 10.0
    mr_sma_filter_period: int = 200
    mr_max_concurrent: int = 2
    mr_max_holding_days: int = 5
    mr_atr_stop_mult: float = 1.5
    mr_risk_pct: float = 0.005

    # ----- Earnings blackout -----------------------------------------------
    # Skip new entries within `earnings_blackout_days` calendar days BEFORE
    # a ticker's next earnings call.  Disabled by default in BACKTESTS
    # because we have no point-in-time earnings history (Yahoo only gives
    # the next upcoming earnings, not what was scheduled at any past date).
    # The Backtester ignores this field; live and paper engines apply it.
    #
    # 7 calendar days ≈ 5 trading days, which covers the typical "report
    # within a week" event-risk window without being overly restrictive.
    # No symmetric post-earnings blackout: earnings gaps IN our favour are
    # part of the momentum edge.
    earnings_blackout_enabled_live: bool = True
    earnings_blackout_days: int = 7

SIGNAL_PARAMS = SignalParams()


# ---------------------------------------------------------------------------
# Regime filters — gate new entries by broad-market conditions
#
# These act on top of the per-stock signal logic.  When the regime filter
# returns False, NO new entries are taken on that bar (existing positions are
# still managed normally — stops, targets, trailing all run).
#
# Three independent checks, all configurable so the WFO can grid-search which
# combination matters:
#
#   1. spy_above_sma:   SPY > 200-SMA AND SMA sloping up over `sma_slope_lookback`
#                       bars.  This is the canonical "market in uptrend" check.
#                       Stricter than the HMA filter because the SMA reacts
#                       slowly enough that it doesn't flip on every choppy week.
#
#   2. vix_below_threshold:  VIX < `vix_max`.  VIX > 25-30 historically marks
#                       chop/panic regimes where breakout strategies underperform.
#                       Set vix_max=999 to disable.
#
#   3. spy_drawdown_ok: SPY no more than `max_spy_drawdown_pct` below its
#                       N-day high.  Catches early-warning of regime change
#                       before the SMA filter flips.
#
# Set `enabled=False` on the whole RegimeFilters to bypass entirely.
# ---------------------------------------------------------------------------
@dataclass
class RegimeFilters:
    enabled: bool = True

    # --- SPY trend filter (replaces the old HMA filter when enabled) ---
    spy_sma_period: int = 200
    sma_slope_lookback: int = 20         # bars over which to check the slope
    require_spy_above_sma: bool = True

    # --- VIX regime filter ---
    vix_ticker: str = "^VIX"
    vix_max: float = 25.0                # skip new entries when VIX > this
    require_vix_below_threshold: bool = True

    # --- SPY drawdown filter ---
    spy_drawdown_lookback: int = 60      # days to compute recent high
    max_spy_drawdown_pct: float = 0.05   # skip new entries when DD > 5%
    require_spy_drawdown_ok: bool = True


REGIME_FILTERS = RegimeFilters()


# ---------------------------------------------------------------------------
# Trading mode — paper vs live
# ---------------------------------------------------------------------------
# The bot has THREE modes:
#
#   "paper"  — DEFAULT.  Records every signal as a virtual trade with
#              realistic fills, fees, and FX.  Tracks performance vs WFO
#              expectations.  No real orders sent anywhere.  Use this for
#              60-90 days before going live.
#
#   "live"   — Real money.  Currently has NO order routing wired up — the
#              bot only sends Telegram alerts and you place orders manually
#              through Robinhood UK.  In live mode the bot still tracks
#              positions in the DB so it can size correctly and detect
#              stop/target hits, but YOU are the execution layer.
#
#   "off"    — Scan-only mode.  Sends Telegram alerts but does NOT record
#              anything in the DB.  Useful for testing the alerting path.
#
# Override via TRADING_MODE env var, e.g. TRADING_MODE=live.
import os as _os
TRADING_MODE: str = _os.getenv("TRADING_MODE", "paper").lower()
assert TRADING_MODE in ("paper", "live", "off"), (
    f"TRADING_MODE must be 'paper', 'live', or 'off' (got {TRADING_MODE!r})"
)


@dataclass
class PaperTradingConfig:
    """
    Settings for the simulated paper-trading layer.

    The paper trader mimics what would have happened if every signal the bot
    generated had been executed at the next-day open with realistic costs.
    Compare paper performance to your WFO expectations before going live.

    Targets (rough):
      - paper Sharpe within 50% of WFO OOS Sharpe (>0.5 is acceptable)
      - paper CAGR within 50% of WFO OOS CAGR
      - max DD not materially worse than OOS

    A 60-90 day paper window is recommended before live deployment.
    """
    starting_capital_gbp: float = 30_000.0
    # Realistic execution assumptions for Robinhood UK ISA:
    fill_at: str = "next_open"               # "next_open" or "next_close"
    extra_slippage_bps: float = 0.0          # on top of FEE_MODEL.slippage_pct
    # If any of these conditions hit, mark the position closed at that price:
    enforce_stop: bool = True
    enforce_target: bool = True
    enforce_trailing: bool = True
    # Maximum concurrent open paper positions.  Bumped from 6 to 8 (worst-case
    # loss = 8 × £225 = £1,800 = 6% of capital — still defensive).  Mirrors
    # the realistic constraint of "how many positions can you watch at once?"
    # rather than the WFO default.
    max_open_positions: int = 8
    # Maximum NEW entries per single scan.  Even when 13+ signals fire on a
    # noisy day, we only open this many in one bar.  Prevents the worst case
    # of all slots being filled simultaneously with correlated trades that
    # all stop out together a few days later.
    max_new_entries_per_scan: int = 3
    # When True, refuses new entries if no FX rate is available
    require_live_fx: bool = True


PAPER_CONFIG = PaperTradingConfig()

# ---------------------------------------------------------------------------
# Risk / position sizing / pyramiding
# ---------------------------------------------------------------------------
@dataclass
class RiskParams:
    risk_per_trade_pct: float = 0.0075         # 0.75% initial risk (slightly more conservative)
    # ABSOLUTE GBP cap on per-trade risk, anchored to starting capital so it
    # does NOT grow as equity compounds.  Without this, risk_budget = equity × 0.75%
    # scales linearly with the equity curve — so a strategy that runs from £30k
    # to £1M ends up risking £7,500 per trade instead of £225.  That's the same
    # phantom-leverage bug as the position-size cap, just expressed through risk
    # budget rather than notional.
    #
    # Default: 0.75% of starting £30k = £225.  Set to 0 to disable.
    risk_per_trade_gbp_absolute: float = 225.0
    pyramid_add_risk_pct: float = 0.005        # +0.5% on Tier 1 add
    max_total_risk_pct_per_ticker: float = 0.015  # 1.5% combined cap
    pyramid_trigger_gain_pct: float = 0.06     # add at +6% from entry
    atr_period: int = 14
    # WFO-validated: 1.75 won 100% of windows across 3 objectives
    atr_stop_mult: float = 1.75
    # WFO-validated: 3.0 won 79% of windows across 3 objectives
    target_rr_multiple: float = 3.0
    # NOTE: this is a multiple of INITIAL RISK PER SHARE (entry - initial_stop),
    # not of current ATR.  The trailing stop is therefore tied to the trade's
    # original risk unit and does not float with new volatility.
    trailing_stop_risk_mult: float = 2.5
    max_open_positions: int = 6
    max_position_pct_of_equity: float = 0.20   # cap one position at 20% of equity
    # ABSOLUTE GBP cap on a single position's notional, anchored to STARTING
    # capital so it does NOT grow as the equity curve compounds.  Without
    # this, equity feeds back: as the strategy makes money, it sizes bigger,
    # which makes more money, which sizes bigger... ending up with single
    # positions worth £100k+ in a £30k ISA.  That isn't realistic — Robinhood
    # UK doesn't offer ISA leverage.
    #
    # Default: 20% of starting capital = £6,000.  Matches what
    # max_position_pct_of_equity * STARTING_CAPITAL_GBP would have been at t=0.
    # Set to 0 to disable.
    max_position_gbp_absolute: float = 6_000.0
    # ABSOLUTE GBP cap on TOTAL invested notional across all open positions.
    # In a UK ISA you cannot exceed your cash balance — no margin, no leverage.
    # Default: 1.0 × starting capital so the bot can never go "leveraged" by
    # re-investing unrealised gains.  Set to 0 to disable.
    max_total_invested_gbp_absolute: float = 30_000.0
    min_shares_per_trade: int = 1              # won't open <1 share
    min_dollar_notional_per_trade: float = 200 # skip vanity-tiny trades
    cooldown_days_after_exit: int = 5          # ← FIX: prevents daily re-entries
    # Hard absolute share cap (sanity ceiling). With £30k equity, even at $1
    # per share max position 20% = £6k = ~7,500 shares. 50,000 is generous.
    abs_max_shares: int = 50_000

    # ----- Volatility targeting -------------------------------------------
    # Scale risk-per-trade by (target_vol / realized_SPY_vol).  In calm
    # regimes (low SPY 30-day realized vol) the strategy deploys a bit more
    # risk per signal; in panic regimes it deploys less.  The clip range
    # [vol_scale_min, vol_scale_max] keeps the scaling sane — without it a
    # post-shock 40% vol reading would set scale to ~0.4 and a pre-shock
    # 8% reading would set it to 1.9, which is too aggressive.
    #
    # The vol-target value (15% annualized) approximates the long-run
    # realized vol of SPY.  Setting it to typical-SPY-vol means the
    # average scale factor is ~1.0 — so vol-targeting alone doesn't change
    # average position size, only its dispersion across regimes.
    #
    # Set vol_targeting_enabled=True to enable (default False after the
    # v2.3.1 WFO comparison showed it degrades OOS Sharpe by -0.193 avg
    # and OOS Calmar by -2.324 avg on PIT-NASDAQ100 2020-2025.  Likely
    # cause: the regime filter already handles risk-off when SPY breaks
    # the 200-SMA, so vol-targeting's downscaling in panic regimes
    # scales nothing (no new entries are being taken there), while its
    # upscaling in calm regimes over-bets just before reversals.
    # Kept in the code as a togglable option for future re-validation
    # on different universes / objectives.
    vol_targeting_enabled: bool = False
    vol_target_annualized: float = 0.15
    vol_lookback_days: int = 30
    vol_scale_min: float = 0.5
    vol_scale_max: float = 1.5

RISK_PARAMS = RiskParams()

# ---------------------------------------------------------------------------
# Backtest / WFO / Monte Carlo
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Monthly cash contributions
#
# Models a standing-order deposit into the ISA: by default £750 on the last
# Friday of every month.  If the last Friday of a month is a market holiday
# (e.g. Good Friday) the contribution lands on the immediately-preceding
# trading day so it always counts against the same calendar month.
#
# IMPORTANT — interaction with absolute risk caps
# -----------------------------------------------
# The risk_per_trade_gbp_absolute / max_position_gbp_absolute /
# max_total_invested_gbp_absolute caps in RiskParams are deliberately anchored
# to STARTING capital so they don't compound with the equity curve.  When the
# user pays new principal in via a contribution, that's not compounding —
# it's fresh cash that should be deployable.  Therefore, when contributions
# are enabled and `scale_absolute_caps_with_contributions` is True (default),
# the three GBP-absolute caps are scaled by
#     (starting_capital + cumulative_contributions) / starting_capital
# so the new cash can actually be put to work.  Set to False to keep the caps
# pinned to starting capital regardless of contributions (the bot will then
# accumulate cash but not deploy it past the original cap, which is rarely
# what you want).
#
# Note: UK ISA annual subscription limit is £20,000 (2024/25 tax year).
# £750 × 12 = £9,000/year, well within the limit.  No validation is enforced
# here — if you raise `amount_gbp` above £1,666/month you'll need to think
# about the limit yourself.
# ---------------------------------------------------------------------------
@dataclass
class ContributionsConfig:
    enabled: bool = True
    amount_gbp: float = 750.0
    schedule: str = "last_friday"            # only schedule supported today
    scale_absolute_caps_with_contributions: bool = True

CONTRIBUTIONS = ContributionsConfig()


# ---------------------------------------------------------------------------
# Periodic revalidation
#
# Controls how/when the bot re-runs the WFO audit.  Two triggers, OR'd:
#   1. Cadence: `cadence_days` since last successful run (default 180).
#   2. Paper drift: paper Sharpe is more than `paper_drift_threshold_pct`%
#      below the most recent WFO avg OOS Sharpe (default 50%).
#
# When a revalidation runs it:
#   - Executes multi_wfo with name `multi_<YYYY>q<Q>`.
#   - Diffs against the previous WFO report and counts "category shifts"
#     (windows whose best-IS-combo string changed).
#   - Sends a Telegram alert (if `notify_telegram` and credentials are set).
#   - Writes status to data/revalidation/last_revalidation.json.
#
# It deliberately does NOT auto-apply parameter changes.  `auto_apply=True`
# does nothing in the current build except log a loud warning at startup.
# Parameter changes are a human decision: read the report, think, edit
# config.py.  Trading systems on auto-tune die quietly.
# ---------------------------------------------------------------------------
@dataclass
class RevalidationConfig:
    cadence_days: int = 180                      # 6 months
    paper_drift_threshold_pct: float = 50.0      # |Δ| beyond this triggers
    combo_shift_threshold: int = 4               # windows whose best-combo
                                                 # changed before flagging
    notify_telegram: bool = True
    auto_apply: bool = False                     # DO NOT change without reading
                                                 # the docstring above

REVALIDATION = RevalidationConfig()


@dataclass
class BacktestParams:
    start_date: str = "2019-01-01"
    end_date: str = ""
    # Walk-forward windows in trading days
    wfo_in_sample_days: int = 504              # ~2 years
    wfo_out_sample_days: int = 126             # ~6 months
    wfo_step_days: int = 126
    # Monte Carlo
    mc_runs: int = 2000
    mc_slippage_jitter_pct: float = 0.003
    mc_winrate_jitter: float = 0.05
    # Performance
    risk_free_rate: float = 0.04
    trading_days_per_year: int = 252

BACKTEST_PARAMS = BacktestParams()

# ---------------------------------------------------------------------------
# Live engine
# ---------------------------------------------------------------------------
@dataclass
class LiveParams:
    scan_mode: str = "intraday"
    scan_interval_minutes: int = 30
    market_open_utc_hour: int = 14
    market_open_utc_minute: int = 30
    market_close_utc_hour: int = 21
    market_close_utc_minute: int = 0
    daily_summary_utc_hour: int = 21
    daily_summary_utc_minute: int = 30
    refresh_watchlist_on_start: bool = True
    refresh_watchlist_daily: bool = True
    # Pre-market readiness Telegram — fires `premarket_lead_minutes` before
    # the actual NYSE open.  The scheduler is DST-aware via the
    # _market_open_utc helper in live.py, so the fire time tracks
    # EST/EDT automatically year-round.  Set premarket_enabled=False to
    # suppress.  The /premarket command is always available regardless
    # of this flag.  premarket_utc_hour / _minute are legacy fields,
    # kept for backwards compatibility but no longer drive scheduling.
    premarket_enabled: bool = True
    premarket_lead_minutes: int = 5
    premarket_utc_hour: int = 14
    premarket_utc_minute: int = 25

LIVE_PARAMS = LiveParams(
    scan_mode="daily",                    # daily scan after market close
    scan_interval_minutes=60,             # not used in daily mode
    refresh_watchlist_daily=True,
    market_close_utc_hour=21,             # 16:00 NY = 21:00 London
    market_close_utc_minute=5,            # slight buffer
    daily_summary_utc_hour=21,
    daily_summary_utc_minute=30,
    premarket_enabled=True,
    premarket_utc_hour=14,                # 09:25 NY ET = 14:25 UTC (winter)
    premarket_utc_minute=25,
)
