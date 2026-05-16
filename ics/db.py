"""
db.py
-----
SQLite persistence layer for signals, trades, watchlist history, equity curve.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from . import config
from .logging_utils import get_logger

import pandas as pd

log = get_logger("ics.db")


SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    ticker TEXT NOT NULL,
    tier INTEGER NOT NULL,
    score INTEGER NOT NULL,
    entry_usd REAL NOT NULL,
    stop_usd REAL NOT NULL,
    target_usd REAL NOT NULL,
    rsi REAL,
    rs REAL,
    breakout INTEGER,
    flag_active INTEGER,
    reasons TEXT,
    payload_json TEXT,
    source TEXT DEFAULT 'live'
);
CREATE INDEX IF NOT EXISTS idx_signals_ticker_ts ON signals(ticker, timestamp);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    tier INTEGER NOT NULL,
    entry_ts TEXT NOT NULL,
    entry_usd REAL NOT NULL,
    exit_ts TEXT,
    exit_usd REAL,
    shares INTEGER NOT NULL,
    pyramid_shares INTEGER DEFAULT 0,
    fx_entry REAL NOT NULL,
    fx_exit REAL,
    pnl_usd REAL,
    pnl_gbp REAL,
    return_pct REAL,
    reason_exit TEXT,
    source TEXT DEFAULT 'live'
);
CREATE INDEX IF NOT EXISTS idx_trades_ticker_ts ON trades(ticker, entry_ts);

CREATE TABLE IF NOT EXISTS watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_ts TEXT NOT NULL,
    ticker TEXT NOT NULL,
    payload_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_watchlist_ts ON watchlist(snapshot_ts);

CREATE TABLE IF NOT EXISTS equity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL UNIQUE,
    equity_gbp REAL NOT NULL,
    cash_gbp REAL,
    open_positions INTEGER,
    source TEXT DEFAULT 'live'
);

CREATE TABLE IF NOT EXISTS earnings_cache (
    ticker TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    next_earnings_date TEXT,    -- ISO date, NULL if none upcoming/known
    source TEXT DEFAULT 'yfinance',
    PRIMARY KEY (ticker)
);
CREATE INDEX IF NOT EXISTS idx_earnings_ticker ON earnings_cache(ticker);

CREATE TABLE IF NOT EXISTS contributions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contribution_date TEXT NOT NULL,
    amount_gbp REAL NOT NULL,
    source TEXT DEFAULT 'paper',
    UNIQUE (contribution_date, source)
);
CREATE INDEX IF NOT EXISTS idx_contrib_date ON contributions(contribution_date);

-- Execution audit trail.  Every alert the bot SENDS to the user is logged
-- here with the bot's expected fill.  When the user replies /done with
-- their actual fill, we update user_executed_at + user_fill_usd and compute
-- the slippage delta.  This is how we measure the bot-vs-reality execution
-- gap — the thing that kills retail strategies between paper and live.
CREATE TABLE IF NOT EXISTS signals_sent (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_sent_at TEXT NOT NULL,            -- ISO timestamp when Telegram fired
    ticker TEXT NOT NULL,
    tier INTEGER NOT NULL,
    signal_type TEXT DEFAULT 'momentum',
    expected_fill_usd REAL NOT NULL,        -- next_open * (1+slip), the bot's plan
    stop_usd REAL,
    target_usd REAL,
    shares_planned INTEGER,
    -- User's actual execution (NULL until /done is received)
    user_executed_at TEXT,
    user_fill_usd REAL,
    user_shares INTEGER,
    slippage_pct REAL,                      -- (user_fill - expected) / expected
    -- "missed" if the user reports they didn't take the trade, "executed" if filled
    outcome TEXT,                           -- NULL | 'executed' | 'missed' | 'cancelled'
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_sent_ticker_ts
    ON signals_sent(ticker, alert_sent_at);
CREATE INDEX IF NOT EXISTS idx_signals_sent_outcome
    ON signals_sent(outcome);
"""


@contextmanager
def connect(path: Optional[Path] = None):
    p = path or config.DB_PATH
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as c:
        c.executescript(SCHEMA)
    log.info("DB initialised at %s", config.DB_PATH)


def insert_signal(signal_dict: dict, source: str = "live") -> int:
    with connect() as c:
        cur = c.execute(
            """
            INSERT INTO signals
            (timestamp, ticker, tier, score, entry_usd, stop_usd, target_usd,
             rsi, rs, breakout, flag_active, reasons, payload_json, source)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                signal_dict["timestamp"],
                signal_dict["ticker"],
                int(signal_dict["tier"]),
                int(signal_dict["score"]),
                float(signal_dict["entry_price"]),
                float(signal_dict["stop_loss"]),
                float(signal_dict["target_price"]),
                float(signal_dict.get("rsi", 0)),
                float(signal_dict.get("rs_score", 0)),
                int(bool(signal_dict.get("breakout", False))),
                int(bool(signal_dict.get("flag_active", False))),
                signal_dict.get("reasons", ""),
                json.dumps(signal_dict, default=str),
                source,
            ),
        )
        return cur.lastrowid


def insert_trade(trade: dict, source: str = "live") -> int:
    with connect() as c:
        cur = c.execute(
            """
            INSERT INTO trades
            (ticker, tier, entry_ts, entry_usd, exit_ts, exit_usd, shares,
             pyramid_shares, fx_entry, fx_exit, pnl_usd, pnl_gbp, return_pct,
             reason_exit, source)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                trade["ticker"],
                int(trade["tier"]),
                trade["entry_ts"],
                float(trade["entry_usd"]),
                trade.get("exit_ts"),
                float(trade["exit_usd"]) if trade.get("exit_usd") is not None else None,
                int(trade["shares"]),
                int(trade.get("pyramid_shares", 0)),
                float(trade["fx_entry"]),
                float(trade["fx_exit"]) if trade.get("fx_exit") is not None else None,
                float(trade.get("pnl_usd")) if trade.get("pnl_usd") is not None else None,
                float(trade.get("pnl_gbp")) if trade.get("pnl_gbp") is not None else None,
                float(trade.get("return_pct")) if trade.get("return_pct") is not None else None,
                trade.get("reason_exit", ""),
                source,
            ),
        )
        return cur.lastrowid


def insert_equity(timestamp: str, equity_gbp: float, cash_gbp: float = 0.0,
                  open_positions: int = 0, source: str = "live") -> None:
    with connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO equity (timestamp, equity_gbp, cash_gbp, "
            "open_positions, source) VALUES (?,?,?,?,?)",
            (timestamp, equity_gbp, cash_gbp, open_positions, source),
        )


def snapshot_watchlist(rows: Iterable[dict]) -> None:
    ts = datetime.utcnow().isoformat(timespec="seconds")
    with connect() as c:
        for r in rows:
            c.execute(
                "INSERT INTO watchlist (snapshot_ts, ticker, payload_json) VALUES (?,?,?)",
                (ts, r["ticker"], json.dumps(r, default=str)),
            )


def insert_contribution(
    contribution_date: str, amount_gbp: float, source: str = "paper"
) -> bool:
    """Idempotently record a contribution.  Returns True if a NEW row was
    inserted, False if (date, source) was already present."""
    with connect() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO contributions "
            "(contribution_date, amount_gbp, source) VALUES (?,?,?)",
            (contribution_date, float(amount_gbp), source),
        )
        return cur.rowcount > 0


def get_contributions(source: str = "paper") -> list[dict]:
    """All recorded contributions for a given source, oldest first."""
    with connect() as c:
        rows = c.execute(
            "SELECT contribution_date, amount_gbp FROM contributions "
            "WHERE source = ? ORDER BY contribution_date ASC",
            (source,),
        ).fetchall()
        return [dict(r) for r in rows]


def total_contributions(source: str = "paper") -> float:
    """Sum of recorded contributions for a given source."""
    with connect() as c:
        row = c.execute(
            "SELECT COALESCE(SUM(amount_gbp), 0.0) AS total FROM contributions "
            "WHERE source = ?",
            (source,),
        ).fetchone()
        return float(row["total"]) if row else 0.0


# ----------------------------------------------------------------------------
# Execution audit trail: signals_sent table
# ----------------------------------------------------------------------------
def record_signal_sent(
    ticker: str,
    tier: int,
    expected_fill_usd: float,
    stop_usd: Optional[float] = None,
    target_usd: Optional[float] = None,
    shares_planned: Optional[int] = None,
    signal_type: str = "momentum",
    alert_sent_at: Optional[str] = None,
) -> int:
    """Record an alert dispatched to the user.  Returns the new row id, which
    becomes a unique handle the user can reference (e.g. '/done 42' or via
    the ticker)."""
    ts = alert_sent_at or datetime.utcnow().isoformat(timespec="seconds")
    with connect() as c:
        cur = c.execute(
            "INSERT INTO signals_sent "
            "(alert_sent_at, ticker, tier, signal_type, expected_fill_usd, "
            " stop_usd, target_usd, shares_planned) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (ts, ticker.upper(), int(tier), signal_type,
             float(expected_fill_usd),
             float(stop_usd) if stop_usd is not None else None,
             float(target_usd) if target_usd is not None else None,
             int(shares_planned) if shares_planned is not None else None),
        )
        return int(cur.lastrowid)


def record_user_execution(
    signal_id: Optional[int] = None,
    ticker: Optional[str] = None,
    user_fill_usd: Optional[float] = None,
    user_shares: Optional[int] = None,
    outcome: str = "executed",
    notes: Optional[str] = None,
) -> Optional[dict]:
    """
    Record the user's actual fill against a previously-sent signal.  Either
    pass `signal_id` directly, or pass `ticker` to update the most recent
    pending (outcome IS NULL) signal for that ticker.

    Returns the updated row as a dict, or None if no matching signal was found.

    Computes `slippage_pct = (user_fill - expected) / expected`.  Negative
    means the user filled BELOW the bot's expected price (positive for the
    user on a long).
    """
    with connect() as c:
        if signal_id is not None:
            row = c.execute(
                "SELECT * FROM signals_sent WHERE id = ?", (signal_id,)
            ).fetchone()
        elif ticker is not None:
            row = c.execute(
                "SELECT * FROM signals_sent "
                "WHERE ticker = ? AND outcome IS NULL "
                "ORDER BY alert_sent_at DESC LIMIT 1",
                (ticker.upper(),),
            ).fetchone()
        else:
            raise ValueError("Pass either signal_id or ticker")

        if row is None:
            return None
        row = dict(row)

        slippage_pct: Optional[float] = None
        if user_fill_usd is not None and row["expected_fill_usd"]:
            expected = float(row["expected_fill_usd"])
            if expected > 0:
                slippage_pct = (float(user_fill_usd) - expected) / expected

        c.execute(
            "UPDATE signals_sent SET "
            "user_executed_at = ?, user_fill_usd = ?, user_shares = ?, "
            "slippage_pct = ?, outcome = ?, notes = COALESCE(?, notes) "
            "WHERE id = ?",
            (datetime.utcnow().isoformat(timespec="seconds"),
             float(user_fill_usd) if user_fill_usd is not None else None,
             int(user_shares) if user_shares is not None else None,
             slippage_pct, outcome, notes, row["id"]),
        )
        updated = c.execute(
            "SELECT * FROM signals_sent WHERE id = ?", (row["id"],)
        ).fetchone()
        return dict(updated) if updated else None


def get_pending_signals(within_days: int = 3) -> list[dict]:
    """All signals sent in the last `within_days` that haven't been resolved."""
    cutoff = (datetime.utcnow() - pd.Timedelta(days=within_days)).isoformat()
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM signals_sent "
            "WHERE outcome IS NULL AND alert_sent_at >= ? "
            "ORDER BY alert_sent_at DESC",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_execution_audit(days: int = 30) -> list[dict]:
    """All resolved signals from the last N days, for slippage reporting."""
    cutoff = (datetime.utcnow() - pd.Timedelta(days=days)).isoformat()
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM signals_sent "
            "WHERE outcome IS NOT NULL AND alert_sent_at >= ? "
            "ORDER BY alert_sent_at DESC",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]
