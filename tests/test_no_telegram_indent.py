"""Regression test: every Telegram-bound function must render with no
line beginning with whitespace.

Previous versions of this file did a source-text scan for `append(f"  X..."`
patterns inside `live.py`.  That caught some bugs but missed others
(notably the /help builder in `notifier.py`).  The proper check is to
INVOKE each function and inspect its output.
"""
from unittest.mock import patch, MagicMock
from dataclasses import dataclass
from datetime import datetime
import pandas as pd
import pytest


def _check_no_leading_whitespace(name, text):
    bad = []
    for i, line in enumerate(text.split("\n"), start=1):
        if not line:
            continue
        if line[0] not in (" ", "\t"):
            continue
        stripped = line.lstrip()
        if not stripped:
            continue
        # Parenthetical continuation: "(signals fire on bar close...)"
        if stripped.startswith("("):
            continue
        bad.append((i, line))
    return bad


@dataclass
class _FakePos:
    ticker: str
    tier: int
    entry_usd: float
    shares: int
    initial_stop_usd: float
    target_usd: float
    pyramid_shares: int = 0
    entry_ts: datetime = datetime(2026, 5, 1)
    def unrealised_usd(self, mark):
        return (mark - self.entry_usd) * self.shares


class _FakeTrader:
    cash_gbp = 11_869.88
    class _Cfg:
        starting_capital_gbp = 30_000.0
    cfg = _Cfg()
    positions = {
        "TSLA": _FakePos("TSLA", 1, 421.24, 10, 391.64, 508.36),
        "ADI":  _FakePos("ADI",  1, 415.32, 13, 393.06, 480.45),
    }
    def current_equity(self, fx, use_intraday=True):
        return 29_981.89
    def summary(self):
        return {
            "starting_capital_gbp": 30000.0, "cash_gbp": 11869.88,
            "open_positions": 2, "open_tier1": 2, "open_tier2": 0,
            "n_closed": 0, "win_rate_pct": 0.0,
            "avg_pnl_gbp": 0.0, "best_trade_gbp": 0.0, "worst_trade_gbp": 0.0,
            "tier_breakdown": {1: {"n": 0}, 2: {"n": 0}},
        }


def _fake_fx(*a, **kw):
    return pd.Series([0.79], index=[datetime(2026, 5, 11)])


def _fake_ticker(t):
    m = MagicMock()
    m.fast_info = {"last_price": 425.0 if t == "TSLA" else 420.0}
    return m


@pytest.fixture
def patched_live():
    from ics import config, paper_trader, data, live
    config.TRADING_MODE = "paper"
    data._quote_cache.clear()
    patches = [
        patch.object(paper_trader, "get_trader", return_value=_FakeTrader()),
        patch("ics.data.yf.Ticker", side_effect=_fake_ticker),
        patch("ics.data.get_fx_series", side_effect=_fake_fx),
        patch("ics.live.get_fx_series", side_effect=_fake_fx),
        patch.object(live.signals, "current_regime_status",
                     return_value={"enabled": True, "ok": True,
                                   "reason": "all regime checks pass",
                                   "as_of": "2026-05-11"}),
    ]
    for p in patches:
        p.start()
    yield live
    for p in patches:
        p.stop()


def test_cmd_equity_no_leading_whitespace(patched_live):
    text = patched_live._cmd_equity()
    bad = _check_no_leading_whitespace("/equity", text)
    assert not bad, "/equity has indented lines:\n" + "\n".join(
        f"  L{i}: {ln!r}" for i, ln in bad
    )


def test_cmd_paper_no_leading_whitespace(patched_live):
    text = patched_live._cmd_paper()
    bad = _check_no_leading_whitespace("/paper", text)
    assert not bad, "/paper has indented lines:\n" + "\n".join(
        f"  L{i}: {ln!r}" for i, ln in bad
    )


def test_cmd_regime_no_leading_whitespace(patched_live):
    text = patched_live._cmd_regime()
    bad = _check_no_leading_whitespace("/regime", text)
    assert not bad


def test_premarket_no_leading_whitespace(patched_live):
    text = patched_live._build_premarket_report()
    bad = _check_no_leading_whitespace("/premarket", text)
    assert not bad, "/premarket has indented lines:\n" + "\n".join(
        f"  L{i}: {ln!r}" for i, ln in bad
    )


def test_cmd_pending_empty_no_leading_whitespace(patched_live):
    text = patched_live._cmd_pending()
    bad = _check_no_leading_whitespace("/pending", text)
    assert not bad


def test_help_no_leading_whitespace():
    """The /help handler lives in notifier.py, not live.py — was the
    source of the v2.5.3 miss."""
    from ics import notifier
    saved_actions = dict(notifier._actions)
    saved_descs = dict(notifier._action_descriptions)
    try:
        notifier._actions.clear()
        notifier._action_descriptions.clear()
        notifier.register_action("scan", lambda: "ok",
                                 description="trigger a manual scan now")
        notifier.register_action("equity", lambda: "ok",
                                 description="show current equity")
        notifier.register_action("done", lambda a: "ok",
                                 description="record fill")
        notifier.register_action("slippage", lambda a: "ok",
                                 description="execution slippage report")
        text = notifier._handle_command("/help")
        bad = _check_no_leading_whitespace("/help", text)
        assert not bad, "/help has indented lines:\n" + "\n".join(
            f"  L{i}: {ln!r}" for i, ln in bad
        )
    finally:
        notifier._actions.clear()
        notifier._action_descriptions.clear()
        notifier._actions.update(saved_actions)
        notifier._action_descriptions.update(saved_descs)


def test_no_html_tags_in_any_module():
    """No HTML tags in any ics/*.py source."""
    from pathlib import Path
    import glob
    for path in glob.glob("ics/*.py"):
        src = Path(path).read_text()
        assert "<b>" not in src, f"{path} contains <b>"
        assert "</b>" not in src, f"{path} contains </b>"
        assert "<i>" not in src, f"{path} contains <i>"
        assert "</i>" not in src, f"{path} contains </i>"
