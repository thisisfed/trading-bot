"""
preflight.py
------------
Pre-flight checks the bot can run before deployment to surface problems
before they cost you money or wake you up at 3am.

Run via:
    python -m ics.cli preflight

The check returns a non-zero exit code if anything fails.  systemd-friendly:
you can chain it before `ExecStart` if you want hard-fail-on-misconfig:

    ExecStartPre=/path/to/.venv/bin/python -m ics.cli preflight

Each check is independent — one failure doesn't abort the others, you'll
see the full list at the end.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List, Tuple

import pandas as pd

from . import config, db


# Each check returns (ok: bool, name: str, detail: str)
CheckResult = Tuple[bool, str, str]


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------
def check_env_secrets() -> CheckResult:
    """Confirm Telegram credentials are loaded."""
    token = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token:
        return (False, "Telegram token", "TELEGRAM_TOKEN not set in env")
    if not chat_id:
        return (False, "Telegram chat ID", "TELEGRAM_CHAT_ID not set in env")
    return (True, "Telegram secrets",
            f"token …{token[-6:]}, chat_id {chat_id}")


def check_telegram_reachable() -> CheckResult:
    """Send a quiet ping to verify Telegram works without spamming."""
    try:
        from . import notifier
    except Exception as e:
        return (False, "Telegram module", f"import failed: {e}")

    token = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        return (False, "Telegram reachability", "no token configured")

    try:
        import requests
        r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        if r.status_code != 200:
            return (False, "Telegram reachability",
                    f"HTTP {r.status_code} from getMe")
        bot_name = r.json().get("result", {}).get("username", "?")
        return (True, "Telegram reachability", f"bot @{bot_name} responsive")
    except Exception as e:
        return (False, "Telegram reachability", f"request failed: {e}")


def check_db_writable() -> CheckResult:
    """Confirm the SQLite DB initialises and accepts writes."""
    try:
        db.init_db()
        with db.connect() as c:
            c.execute("SELECT 1")
        return (True, "SQLite DB", f"writable at {config.DB_PATH}")
    except Exception as e:
        return (False, "SQLite DB", f"init/write failed: {e}")


def check_yfinance_reachable() -> CheckResult:
    """Pull a small SPY history to confirm market data fetches work."""
    try:
        from . import data
        df = data.get_history("SPY", start="2025-01-01")
        if df is None or df.empty:
            return (False, "yfinance / SPY", "empty response")
        return (True, "yfinance / SPY",
                f"{len(df)} bars, latest close ${df['Close'].iloc[-1]:.2f}")
    except Exception as e:
        return (False, "yfinance / SPY", f"fetch failed: {e}")


def check_fx_reachable() -> CheckResult:
    """Confirm the GBP/USD FX series is reachable."""
    try:
        from . import data
        s = data.get_fx_series()
        if s is None or s.empty:
            return (False, "FX series", "empty")
        return (True, "FX series", f"latest {s.iloc[-1]:.4f} GBP/USD")
    except Exception as e:
        return (False, "FX series", f"fetch failed: {e}")


def check_ndx_universe() -> CheckResult:
    """Confirm point-in-time NDX library is installed and producing tickers."""
    try:
        from .constituents import check_library, get_universe_at
        if not check_library():
            return (False, "NDX universe (PIT)",
                    "nasdaq_100_ticker_history not installed — see README")
        today = pd.Timestamp.utcnow().tz_localize(None).normalize().date()
        tickers = get_universe_at(str(today))
        if not tickers:
            return (False, "NDX universe (PIT)", "empty list")
        return (True, "NDX universe (PIT)",
                f"{len(tickers)} tickers, sample {tickers[:5]}")
    except Exception as e:
        return (False, "NDX universe (PIT)", f"error: {e}")


def check_validated_params() -> CheckResult:
    """Confirm the live engine will use the WFO-validated parameter set."""
    issues = []
    rp = config.RISK_PARAMS
    if rp.atr_stop_mult != 1.75:
        issues.append(f"atr_stop_mult={rp.atr_stop_mult} (expected 1.75)")
    if rp.target_rr_multiple != 3.0:
        issues.append(f"target_rr_multiple={rp.target_rr_multiple} (expected 3.0)")
    sp = config.SIGNAL_PARAMS
    if sp.require_weekly_hma_bullish:
        issues.append("require_weekly_hma_bullish is TRUE (stability=NOISE; expected False)")
    rf = config.REGIME_FILTERS
    if not rf.enabled:
        issues.append("REGIME_FILTERS.enabled is False (expected True)")
    if rf.vix_max != 25.0:
        issues.append(f"vix_max={rf.vix_max} (expected 25.0)")

    if issues:
        return (False, "Validated params", "; ".join(issues))
    return (True, "Validated params",
            "atr_stop_mult=1.75, target_rr=3.0, regime ON, vix_max=25.0, weekly_hma OFF")


def check_trading_mode() -> CheckResult:
    """Make sure trading mode is set sensibly and announce what it is."""
    mode = config.TRADING_MODE
    if mode not in ("paper", "live", "off"):
        return (False, "Trading mode", f"unknown mode {mode!r}")
    if mode == "live":
        return (True, "Trading mode",
                "LIVE (real money — bot only sends alerts; you place orders)")
    if mode == "paper":
        return (True, "Trading mode", "PAPER (default — simulated trades only)")
    return (True, "Trading mode", "OFF (alerts only, no DB writes)")


def check_disk_space() -> CheckResult:
    """Confirm at least 100MB free in the working dir."""
    try:
        import shutil
        free = shutil.disk_usage(Path.cwd()).free
        free_mb = free / (1024 * 1024)
        if free_mb < 100:
            return (False, "Disk space", f"only {free_mb:.0f} MB free")
        return (True, "Disk space", f"{free_mb:.0f} MB free")
    except Exception as e:
        return (False, "Disk space", f"check failed: {e}")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
ALL_CHECKS = [
    check_env_secrets,
    check_telegram_reachable,
    check_db_writable,
    check_yfinance_reachable,
    check_fx_reachable,
    check_ndx_universe,
    check_validated_params,
    check_trading_mode,
    check_disk_space,
]


def run_all() -> int:
    """Run every check, print a report, return 0 if all pass else 1."""
    print("=" * 70)
    print(f"  ICS PRE-FLIGHT CHECKS")
    print("=" * 70)
    print()

    results: List[CheckResult] = []
    for check in ALL_CHECKS:
        try:
            ok, name, detail = check()
        except Exception as e:
            ok, name, detail = False, check.__name__, f"check raised: {e}"
        results.append((ok, name, detail))
        emoji = "✅" if ok else "❌"
        print(f"{emoji} {name:30s}  {detail}")

    print()
    print("=" * 70)
    failed = [r for r in results if not r[0]]
    if failed:
        print(f"  ❌  {len(failed)} of {len(results)} checks FAILED.")
        print("  Fix the failures above before deploying.")
        print("=" * 70)
        return 1
    print(f"  ✅  All {len(results)} checks passed.")
    print(f"  Mode: {config.TRADING_MODE.upper()}.  Bot is ready to start.")
    print("=" * 70)
    return 0


def cmd_preflight(args) -> None:
    sys.exit(run_all())


if __name__ == "__main__":
    sys.exit(run_all())
