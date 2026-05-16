"""
notifier.py
-----------
Telegram notifications via plain `requests` (light Pi footprint).
If TELEGRAM_TOKEN/CHAT_ID missing, logs the message instead.

Includes:
  - MarkdownV2-safe escaping (single regex pass, no double-escape)
  - Plain-text fallback if MarkdownV2 send fails
  - Long-polling command listener: runs in a background thread, no
    webhook / public URL / web server required. Replies to /status,
    /ping, /help.
"""
from __future__ import annotations

import re
import threading
import time
from datetime import datetime
from typing import Callable, Dict, Optional

import requests

from . import config
from .logging_utils import get_logger

log = get_logger("ics.notifier")

API_BASE = "https://api.telegram.org/bot{token}"

# --- bot lifecycle metadata, populated by live.py at startup -----------------
_started_at: Optional[datetime] = None
_status_provider: Optional[Callable[[], Dict[str, str]]] = None

# Registered Telegram command handlers.  Live engine registers these by name,
# the long-poll loop dispatches `/foo` -> _actions["foo"]() and sends the result.
_actions: Dict[str, Callable] = {}
_action_descriptions: Dict[str, str] = {}


def register_action(name: str, handler: Callable,
                    description: Optional[str] = None) -> None:
    """Register a Telegram command handler.

    `name` is the slash-command without the slash (e.g. "scan" -> /scan).
    `handler` may be either:
      - Callable[[], Optional[str]]   — zero-arg
      - Callable[[str], Optional[str]] — one-arg, receives text after the
        command (e.g. for `/done 42 178.42`, handler gets "42 178.42")

    `description` is a one-line summary shown in /help.  If omitted,
    /help falls back to "registered handler" — which used to be the
    only output and made /help useless.

    Handlers may return a string reply, or None to suppress (useful for
    handlers that already send their own messages via send_plain).
    """
    key = name.lstrip("/").lower()
    _actions[key] = handler
    if description:
        _action_descriptions[key] = description
    log.info("Registered Telegram action: /%s", name)


def set_status_provider(provider: Callable[[], Dict[str, str]]) -> None:
    """Live engine registers a callback that returns runtime info for /status."""
    global _status_provider
    _status_provider = provider


def mark_started() -> None:
    """Live engine calls this when the main loop starts."""
    global _started_at
    _started_at = datetime.now()


# --- MarkdownV2 escaping -----------------------------------------------------
_MD2_SPECIAL = re.compile(r'([_*\[\]()~`>#+\-=|{}.!\\])')


def _escape_md(s) -> str:
    """Escape MarkdownV2 special chars. Backslash-safe (no double-escape)."""
    if s is None:
        return ""
    return _MD2_SPECIAL.sub(r'\\\1', str(s))


# --- outbound messaging ------------------------------------------------------
def _send_url(method: str) -> str:
    return f"{API_BASE.format(token=config.TELEGRAM_TOKEN)}/{method}"


def send_message(text: str, parse_mode: str = "MarkdownV2") -> bool:
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
        log.info("[telegram-disabled] %s", text)
        return False
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(_send_url("sendMessage"), json=payload, timeout=10)
        if r.status_code == 200:
            return True
        log.warning("Telegram send failed (%s): %s", r.status_code, r.text[:300])
        # Plain-text fallback
        if parse_mode == "MarkdownV2":
            log.info("Retrying as plain text...")
            plain = text.replace("\\", "")
            r2 = requests.post(_send_url("sendMessage"), json={
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": plain,
                "disable_web_page_preview": True,
            }, timeout=10)
            if r2.status_code == 200:
                log.info("Plain-text fallback succeeded.")
                return True
            log.warning("Plain-text fallback failed: %s %s",
                        r2.status_code, r2.text[:300])
        return False
    except Exception as e:
        log.warning("Telegram send exception: %s", e)
        return False


def send_plain(text: str) -> bool:
    """
    Send a plain-text Telegram message.  If `text` exceeds Telegram's per-message
    limit (4096 chars), automatically split into multiple messages on logical
    boundaries (blank lines first, then newlines, then hard char boundary).

    Returns True if ALL chunks sent successfully.
    """
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
        log.info("[telegram-disabled] %s", text)
        return False

    # Conservative limit — leave headroom for any HTML entities Telegram counts
    MAX_CHUNK = 3800
    chunks = _split_for_telegram(text, MAX_CHUNK) if len(text) > MAX_CHUNK else [text]

    all_ok = True
    for i, chunk in enumerate(chunks):
        try:
            r = requests.post(_send_url("sendMessage"), json={
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": chunk,
            }, timeout=10)
            if r.status_code != 200:
                log.warning("Telegram plain send failed (chunk %d/%d): %s %s",
                            i + 1, len(chunks), r.status_code, r.text[:300])
                all_ok = False
        except Exception as e:
            log.warning("Telegram plain send exception (chunk %d/%d): %s",
                        i + 1, len(chunks), e)
            all_ok = False
        # Modest pause between chunks to stay under Telegram rate limits
        if len(chunks) > 1 and i < len(chunks) - 1:
            import time
            time.sleep(0.5)
    return all_ok


def _split_for_telegram(text: str, max_chunk: int) -> list[str]:
    """
    Split a long message into chunks <= max_chunk chars.

    Tries hard to split on natural boundaries:
      1. Double newlines (paragraph breaks) — preferred
      2. Single newlines (line breaks) — fallback
      3. Hard character boundary — last resort
    """
    if len(text) <= max_chunk:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_chunk:
        # Try to split at the last paragraph break before max_chunk
        cut = remaining.rfind("\n\n", 0, max_chunk)
        if cut == -1 or cut < max_chunk // 2:
            # No paragraph break in a sensible place; fall back to last newline
            cut = remaining.rfind("\n", 0, max_chunk)
        if cut == -1 or cut < max_chunk // 2:
            # Still nothing useful — hard cut
            cut = max_chunk
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def format_signal_message(signal: dict, plan: Optional[dict] = None) -> str:
    """Build a MarkdownV2-safe Telegram message from a signal dict."""
    tier = int(signal.get("tier", 0))
    tier_emoji = "🚀" if tier == 1 else "📈"
    tier_label = "TIER 1 — STRONG" if tier == 1 else "TIER 2 — NORMAL"

    ticker = signal.get("ticker", "???")
    entry_price = signal.get("entry_price", 0)
    stop_loss = signal.get("stop_loss", 0)
    target_price = signal.get("target_price", 0)
    rsi_val = signal.get("rsi", 0)
    rs_val = signal.get("rs_score", 0)
    reasons = signal.get("reasons", "")

    risk_dollars = entry_price - stop_loss
    target_gain = target_price - entry_price
    risk_pct = (risk_dollars / entry_price * 100) if entry_price > 0 else 0
    gain_pct = (target_gain / entry_price * 100) if entry_price > 0 else 0
    rr = f"{target_gain / risk_dollars:.2f}" if risk_dollars > 0 else "N/A"

    _e = _escape_md
    lines = [
        f"{tier_emoji} *{_e(tier_label)}* — `{ticker}`",
        "",
        f"*Buy:*    `${entry_price:.2f}`",
        f"*Stop:*   `${stop_loss:.2f}`  \\({_e(f'-{risk_pct:.2f}%')}\\)",
        f"*Target:* `${target_price:.2f}`  \\({_e(f'+{gain_pct:.2f}%')}, R:R `{rr}`\\)",
        "",
        f"*RSI:* `{rsi_val:.1f}`   *RS:* `{rs_val*100:.2f}%`",
        f"*Why:* {_e(reasons)}",
    ]

    if plan:
        shares = plan.get("shares", 0)
        notional = plan.get("notional_gbp", 0)
        risk_gbp = plan.get("risk_gbp", 0)
        risk_eq_pct = plan.get("risk_pct_of_equity", 0)
        fx = plan.get("fx_gbp_per_usd", 0)
        lines += [
            "",
            f"*Size:* `{shares}` shares  \\(`£{notional:,.0f}`\\)",
            f"*Risk:* `£{risk_gbp:,.2f}`  \\(`{risk_eq_pct*100:.2f}%` of equity\\)",
            f"*FX:* `{fx:.4f}` GBP/USD",
        ]
        pyr_trigger = plan.get("pyramid_trigger_usd")
        if pyr_trigger:
            ps = plan.get("pyramid_shares", 0)
            pr = plan.get("pyramid_risk_gbp", 0)
            lines += [
                "",
                "*Pyramid plan:*",
                f"  Add `{ps}` sh at `${pyr_trigger:.2f}` \\(extra risk `£{pr:,.2f}`\\)",
                f"  Raise stop on full size to `${entry_price:.2f}` after add",
            ]
        notes = plan.get("notes")
        if notes:
            lines += ["", _e(notes)]

    return "\n".join(lines)


def send_signal(signal: dict, plan: Optional[dict] = None) -> bool:
    return send_message(format_signal_message(signal, plan))


def send_daily_summary(summary: dict) -> bool:
    lines = ["📊 *ICS Daily Summary*", ""]
    for k, v in summary.items():
        if isinstance(v, float):
            if "pct" in k or k in ("cagr_pct", "max_drawdown_pct", "win_rate"):
                v_str = f"{v*100:.2f}%"
            else:
                v_str = f"{v:.2f}"
        else:
            v_str = str(v)
        lines.append(f"_{_escape_md(k)}:_ `{v_str}`")
    return send_message("\n".join(lines))


# --- inbound: long-polling command listener ---------------------------------
def _format_uptime(started: datetime) -> str:
    delta = datetime.now() - started
    total = int(delta.total_seconds())
    d, rem = divmod(total, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _build_status_reply() -> str:
    lines = ["✅ ICS Bot is running"]
    if _started_at:
        lines.append(f"Uptime: {_format_uptime(_started_at)}")
        lines.append(f"Started: {_started_at.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Now (host): {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if _status_provider is not None:
        try:
            extra = _status_provider() or {}
            for k, v in extra.items():
                lines.append(f"{k}: {v}")
        except Exception as e:
            lines.append(f"(status provider error: {e})")
    return "\n".join(lines)


import inspect as _inspect

def _handle_command(text: str) -> Optional[str]:
    """Return a reply string for a recognised command, or None.

    Handlers can be registered as zero-arg `Callable[[], Optional[str]]`
    or one-arg `Callable[[str], Optional[str]]` (receives the message
    args after the command keyword).  We inspect the signature once and
    pass args only to handlers that accept them — preserves backwards
    compat with all existing handlers.
    """
    raw = (text or "").strip()
    t = raw.lower()
    # Strip an @botname suffix Telegram adds in groups, e.g. /status@my_bot
    if "@" in t:
        t = t.split("@", 1)[0]
    if t in ("/status", "status"):
        return _build_status_reply()
    if t in ("/ping", "ping"):
        return "🏓 pong"
    if t in ("/help", "help", "/start"):
        # Group commands by purpose so /help is actually scannable.
        # Built-ins come first; registered actions follow in groups.
        groups = [
            ("Status & info", [
                ("/status", "show uptime, last scan, regime state"),
                ("/ping",   "quick liveness check"),
                ("/help",   "this message"),
            ]),
        ]
        # Bucket registered actions into action groups by their command name.
        action_groups = {
            "Scans & state": ["scan", "refresh", "equity", "paper", "regime",
                              "premarket"],
            "Execution audit": ["done", "missed", "pending", "slippage"],
        }
        for group_label, cmd_order in action_groups.items():
            rows = []
            for cmd in cmd_order:
                if cmd in _actions:
                    desc = _action_descriptions.get(cmd, "registered handler")
                    rows.append((f"/{cmd}", desc))
            if rows:
                groups.append((group_label, rows))
        # Anything registered but not in the predefined order, append last.
        known = set()
        for _, rows in action_groups.items():
            known.update(rows)
        leftover = [c for c in sorted(_actions.keys()) if c not in known]
        if leftover:
            rows = [(f"/{c}", _action_descriptions.get(c, "registered handler"))
                    for c in leftover]
            groups.append(("Other", rows))

        lines = ["ICS Bot commands:"]
        for label, rows in groups:
            lines.append("")
            lines.append(label + ":")
            # Compute padding for alignment within each group
            width = max(len(name) for name, _ in rows) if rows else 0
            for name, desc in rows:
                lines.append(f"{name:<{width}}  — {desc}")
        return "\n".join(lines)
    # Split into command + args (preserve case in args because tickers care)
    parts = raw.split(None, 1)
    if not parts:
        return None
    cmd = parts[0].lstrip("/").lower()
    cmd = cmd.split("@", 1)[0] if "@" in cmd else cmd
    rest = parts[1].strip() if len(parts) > 1 else ""
    handler = _actions.get(cmd)
    if handler is not None:
        try:
            sig = _inspect.signature(handler)
            if len(sig.parameters) >= 1:
                return handler(rest)
            return handler()
        except Exception as e:
            log.exception("Action /%s failed: %s", cmd, e)
            return f"❌ /{cmd} failed: {str(e)[:200]}"
    return None


def _poll_once(offset):
    """One getUpdates call. Returns (next_offset, updates)."""
    params = {"timeout": 25}  # long-poll: hold the request open up to 25s
    if offset is not None:
        params["offset"] = offset
    try:
        r = requests.get(_send_url("getUpdates"), params=params, timeout=35)
        if r.status_code != 200:
            log.warning("getUpdates failed: %s %s", r.status_code, r.text[:200])
            time.sleep(5)
            return offset, []
        body = r.json()
        if not body.get("ok"):
            log.warning("getUpdates not ok: %s", body)
            time.sleep(5)
            return offset, []
        updates = body.get("result", []) or []
        if updates:
            offset = updates[-1]["update_id"] + 1
        return offset, updates
    except requests.exceptions.Timeout:
        return offset, []
    except Exception as e:
        log.warning("getUpdates exception: %s", e)
        time.sleep(5)
        return offset, []


def _command_loop() -> None:
    """Background thread: poll Telegram for commands and reply."""
    log.info("Telegram command listener started (long-polling).")
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
        log.info("Telegram disabled — command listener exiting.")
        return

    # Whitelist: only respond to messages from the configured chat
    try:
        allowed_chat_id = int(config.TELEGRAM_CHAT_ID)
    except (TypeError, ValueError):
        log.error("TELEGRAM_CHAT_ID is not a valid integer; command listener disabled.")
        return

    offset = None
    while True:
        offset, updates = _poll_once(offset)
        for upd in updates:
            try:
                msg = upd.get("message") or upd.get("edited_message") or {}
                chat_id = msg.get("chat", {}).get("id")
                if chat_id != allowed_chat_id:
                    log.warning("Ignored message from unauthorised chat %s", chat_id)
                    continue
                text = msg.get("text") or ""
                reply = _handle_command(text)
                if reply is not None:
                    send_plain(reply)
            except Exception as e:
                log.exception("Error handling Telegram update: %s", e)


def start_command_listener():
    """Spawn the long-polling listener in a daemon thread. Returns the thread (or None)."""
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
        log.info("Telegram not configured — skipping command listener.")
        return None
    # Make sure no webhook is set, otherwise getUpdates returns 409 Conflict
    try:
        requests.post(_send_url("deleteWebhook"),
                      json={"drop_pending_updates": False}, timeout=10)
    except Exception as e:
        log.debug("deleteWebhook call failed (non-fatal): %s", e)
    t = threading.Thread(target=_command_loop, name="ics-telegram-listener", daemon=True)
    t.start()
    return t
