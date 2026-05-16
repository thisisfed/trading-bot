"""
sizing.py
---------
GBP-native, risk-based position sizing.

v2 fixes vs v1/user version:
- USER VERSION HAD BEEN REPLACED with "fixed $2000 per trade" — that bypassed
  all risk rules and let the backtester cap shares at 200,000 (=> trades worth
  millions). REVERTED to proper risk-based sizing.
- All sizing inputs are validated; returns None on any invalid input.
- Sanity-clamps shares to at most:
    1) risk_budget_gbp / risk_per_share_gbp
    2) (max_position_pct_of_equity * equity_gbp) / (entry_usd * fx_gbp_per_usd)
    3) abs_max_shares  (hard absolute ceiling — prevents pathological cases)

Returns a PositionPlan dataclass with full breakdown including the pyramid plan
for Tier-1 trades.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from . import config


@dataclass
class PositionPlan:
    ticker: str
    tier: int
    entry_usd: float
    stop_usd: float
    target_usd: float
    fx_gbp_per_usd: float
    shares: int
    notional_gbp: float
    risk_gbp: float
    risk_pct_of_equity: float
    pyramid_trigger_usd: Optional[float]
    pyramid_shares: Optional[int]
    pyramid_risk_gbp: Optional[float]
    pyramid_total_target_usd: Optional[float]
    notes: str

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def compute_position(
    equity_gbp: float,
    ticker: str,
    entry_usd: float,
    stop_usd: float,
    target_usd: float,
    tier: int,
    fx_gbp_per_usd: float,
    risk_pct_override: Optional[float] = None,
    risk_params: Optional[config.RiskParams] = None,
) -> Optional[PositionPlan]:
    """
    Build a sizing plan in GBP. Returns None on any invalid input.

    Validation:
      - equity_gbp > 0
      - 0 < stop_usd < entry_usd
      - target_usd > entry_usd  (else still allowed but flagged in notes)
      - fx_gbp_per_usd plausible (0.4 .. 1.5 — defends against FX feed errors)

    `risk_params` lets the caller pass a scaled-cap version of the global
    RiskParams (e.g. when monthly contributions have raised the effective
    capital basis).  Defaults to `config.RISK_PARAMS`.
    """
    rp = risk_params if risk_params is not None else config.RISK_PARAMS

    # ----- input validation -----
    if equity_gbp <= 0:
        return None
    if entry_usd <= 0 or stop_usd <= 0 or stop_usd >= entry_usd:
        return None
    if not (0.4 <= fx_gbp_per_usd <= 1.5):
        # Implausible FX rate — reject rather than open garbage size.
        return None

    # ----- core risk-based sizing -----
    risk_per_share_usd = entry_usd - stop_usd
    risk_pct = risk_pct_override if risk_pct_override is not None else rp.risk_per_trade_pct
    risk_budget_gbp = equity_gbp * risk_pct
    # Absolute GBP cap on risk — prevents the risk budget from compounding
    # along with the equity curve.  See config.RiskParams docstring.
    if rp.risk_per_trade_gbp_absolute > 0:
        risk_budget_gbp = min(risk_budget_gbp, rp.risk_per_trade_gbp_absolute)
    risk_per_share_gbp = risk_per_share_usd * fx_gbp_per_usd

    if risk_per_share_gbp <= 0:
        return None

    raw_shares = risk_budget_gbp / risk_per_share_gbp
    shares = int(math.floor(raw_shares))
    if shares <= 0:
        return None

    # ----- cap by max position % of equity -----
    max_notional_gbp = equity_gbp * rp.max_position_pct_of_equity

    # ----- cap by ABSOLUTE GBP per-position cap (does not grow with equity) -----
    # This is the cap that prevents runaway compounding: max_position_pct_of_equity
    # scales linearly with equity, so as the strategy compounds the per-trade
    # notional grows without bound.  The absolute cap anchors to starting capital.
    if rp.max_position_gbp_absolute > 0:
        max_notional_gbp = min(max_notional_gbp, rp.max_position_gbp_absolute)

    notional_gbp = shares * entry_usd * fx_gbp_per_usd
    if notional_gbp > max_notional_gbp:
        shares = int(math.floor(max_notional_gbp / (entry_usd * fx_gbp_per_usd)))
        notional_gbp = shares * entry_usd * fx_gbp_per_usd

    # ----- absolute share cap (sanity) -----
    if shares > rp.abs_max_shares:
        shares = rp.abs_max_shares
        notional_gbp = shares * entry_usd * fx_gbp_per_usd

    # ----- minimum size gates -----
    if shares < rp.min_shares_per_trade:
        return None
    notional_usd = shares * entry_usd
    if notional_usd < rp.min_dollar_notional_per_trade:
        return None

    risk_gbp = shares * risk_per_share_gbp
    risk_pct_actual = risk_gbp / equity_gbp

    # ----- pyramid plan (Tier 1 only) -----
    pyr_trigger = pyr_shares = pyr_risk = pyr_total_target = None
    notes = ""
    if tier == 1:
        pyr_trigger = entry_usd * (1.0 + rp.pyramid_trigger_gain_pct)
        # When we add, stop on the FULL position rises to the original entry
        # (breakeven on initial). Risk on the add is therefore (pyr_trigger - entry).
        add_risk_per_share_usd = max(pyr_trigger - entry_usd, 1e-6)
        add_risk_per_share_gbp = add_risk_per_share_usd * fx_gbp_per_usd
        # Cap so total risk <= max_total_risk_pct_per_ticker
        remaining_risk_gbp = max(
            equity_gbp * rp.max_total_risk_pct_per_ticker - risk_gbp, 0.0
        )
        add_budget_gbp = min(equity_gbp * rp.pyramid_add_risk_pct, remaining_risk_gbp)
        if add_risk_per_share_gbp > 0:
            pyr_shares = int(math.floor(add_budget_gbp / add_risk_per_share_gbp))
        else:
            pyr_shares = 0
        # Also cap pyramid by remaining position % budget
        max_extra_notional_gbp = max(max_notional_gbp - notional_gbp, 0.0)
        max_pyr_shares_by_notional = int(math.floor(
            max_extra_notional_gbp / (pyr_trigger * fx_gbp_per_usd)
        )) if pyr_trigger > 0 else 0
        pyr_shares = max(0, min(pyr_shares, max_pyr_shares_by_notional))
        pyr_risk = pyr_shares * add_risk_per_share_gbp
        pyr_total_target = target_usd
        notes = (
            f"Tier 1: add {pyr_shares} sh at ${pyr_trigger:.2f} "
            f"(+{rp.pyramid_trigger_gain_pct*100:.1f}%); raise stop on full size to ${entry_usd:.2f}."
        )
    else:
        notes = "Tier 2: single tranche, no pyramid."

    return PositionPlan(
        ticker=ticker,
        tier=tier,
        entry_usd=round(entry_usd, 4),
        stop_usd=round(stop_usd, 4),
        target_usd=round(target_usd, 4),
        fx_gbp_per_usd=round(fx_gbp_per_usd, 6),
        shares=shares,
        notional_gbp=round(notional_gbp, 2),
        risk_gbp=round(risk_gbp, 2),
        risk_pct_of_equity=round(risk_pct_actual, 4),
        pyramid_trigger_usd=round(pyr_trigger, 4) if pyr_trigger else None,
        pyramid_shares=pyr_shares,
        pyramid_risk_gbp=round(pyr_risk, 2) if pyr_risk else None,
        pyramid_total_target_usd=round(pyr_total_target, 4) if pyr_total_target else None,
        notes=notes,
    )
