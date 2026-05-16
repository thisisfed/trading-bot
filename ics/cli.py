"""
cli.py
------
Command-line interface for the ICS bot.

Subcommands:
  refresh-watchlist   build/refresh the dynamic universe
  scan                run a single live scan now
  live                run the live engine (intraday or eod) with auto-restart
  backtest            run a backtest over a date range
  wfo                 walk-forward optimisation
  mc                  Monte Carlo on a saved trade list
  fulltest            one-shot: refresh watchlist + backtest + WFO + MC + reports
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from . import config, db, watchlist, live as live_mod
from .backtest import Backtester
from .wfo import run_wfo
from .multi_wfo import run_multi_wfo, OBJECTIVES, OBJECTIVES_ALL
from .preflight import cmd_preflight
from .montecarlo import parametric_mc, shuffle_mc
from .reporter import write_report, print_summary
from .logging_utils import get_logger

log = get_logger("ics.cli")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _resolve_tickers(args) -> list[str]:
    if getattr(args, "tickers", None):
        return [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    if getattr(args, "from_watchlist", False):
        ts = watchlist.get_tickers()
        if ts:
            log.info("Using watchlist (%d tickers).", len(ts))
            return ts
    cap = getattr(args, "universe_cap", 80) or 80
    out = config.BASE_UNIVERSE[:cap]
    log.info("Falling back to BASE_UNIVERSE (cap=%d -> %d tickers).", cap, len(out))
    return out


# ---------------------------------------------------------------------------
# subcommands
# ---------------------------------------------------------------------------
def cmd_refresh_watchlist(args):
    df = watchlist.refresh_watchlist()
    if df.empty:
        print("Watchlist is empty.")
    else:
        print(df.to_string(index=False))


def cmd_scan(args):
    db.init_db()
    n = live_mod.run_scan_once(notify=False)
    print(f"Scan complete: {n} new signals.")


def cmd_live(args):
    live_mod.main()


def cmd_backtest(args):
    db.init_db()
    tickers = _resolve_tickers(args)
    log.info("Backtesting %d tickers from %s to %s ...",
             len(tickers), args.start, args.end or "today")

    # Resolve contributions config from CLI flags (overrides config.CONTRIBUTIONS).
    contribs = None
    if getattr(args, "no_contributions", False):
        contribs = config.ContributionsConfig(enabled=False)
    elif getattr(args, "contribution_gbp", None) is not None:
        contribs = config.ContributionsConfig(
            enabled=True,
            amount_gbp=float(args.contribution_gbp),
        )

    bt = Backtester(
        tickers=tickers,
        start=args.start,
        end=args.end,
        starting_capital_gbp=args.capital or config.STARTING_CAPITAL_GBP,
        contributions=contribs,
    )
    result = bt.run()
    print_summary(result.summary)

    run_name = args.name or "backtest"
    write_report(
        run_name=run_name,
        equity_gbp=result.equity_gbp,
        trades=result.trades,
        summary=result.summary,
        benchmark_compare=result.benchmark_compare,
        contributions_gbp=result.contributions_gbp,
    )

    if args.mc and not result.trades.empty:
        log.info("Running parametric Monte Carlo ...")
        mc = parametric_mc(result.trades, starting_capital_gbp=bt.starting_capital_gbp)
        write_report(
            run_name=run_name + "_mc",
            equity_gbp=result.equity_gbp,
            trades=result.trades,
            summary=result.summary,
            benchmark_compare=result.benchmark_compare,
            mc_results=mc,
            contributions_gbp=result.contributions_gbp,
        )

    return result


def cmd_wfo(args):
    db.init_db()
    tickers = _resolve_tickers(args)

    out = run_wfo(
        tickers=tickers,
        start=args.start,
        end=args.end,
        objective=args.objective,
        name=args.name or "wfo",
        is_days=getattr(args, "is_days", 504),
        oos_days=getattr(args, "oos_days", 252),
        step_days=getattr(args, "step_days", 252),
        universe=getattr(args, "universe", "nasdaq100"),
        universe_cap=getattr(args, "universe_cap", 105),
    )
    print_summary({k: v for k, v in out["summary"].items() if k != "windows"})

    run_name = args.name or "wfo"
    if not out["os_equity_gbp"].empty:
        write_report(
            run_name=run_name,
            equity_gbp=out["os_equity_gbp"],
            trades=out["os_trades"],
            summary={k: v for k, v in out["summary"].items() if k != "windows"},
        )

    if args.mc and not out["os_trades"].empty:
        mc = parametric_mc(out["os_trades"])
        write_report(
            run_name=run_name + "_mc",
            equity_gbp=out["os_equity_gbp"],
            trades=out["os_trades"],
            summary={k: v for k, v in out["summary"].items() if k != "windows"},
            mc_results=mc,
        )


def cmd_multi_wfo(args):
    run_multi_wfo(
        start=args.start,
        end=args.end,
        is_days=args.is_days,
        oos_days=args.oos_days,
        step_days=args.step_days,
        objectives=args.objectives,
        starting_capital_gbp=args.capital,
        universe=getattr(args, "universe", "nasdaq100"),
        universe_cap=getattr(args, "universe_cap", 105),
    )


def cmd_compare(args):
    """Run a backtest (or load a saved one) and compare it to VWRP buy-and-hold."""
    from .compare import compare_to_benchmark

    # If --from-report points at an existing reports/<name>/equity_gbp.csv, load it.
    # Otherwise run a fresh backtest.
    eq: Optional[pd.Series] = None

    if args.from_report:
        eq_path = Path("data/reports") / args.from_report / "equity_gbp.csv"
        if not eq_path.exists():
            print(f"❌ No equity_gbp.csv found at {eq_path}")
            print("   Run a backtest first, or omit --from-report to run one now.")
            sys.exit(2)
        df = pd.read_csv(eq_path, index_col=0, parse_dates=True)
        eq = df["equity_gbp"]
        print(f"📂 Loaded equity from {eq_path} ({len(eq)} days)")
    else:
        db.init_db()
        tickers = _resolve_tickers(args)
        log.info("Running backtest %s → %s for comparison ...",
                 args.start, args.end or "today")
        bt = Backtester(
            tickers=tickers,
            start=args.start, end=args.end,
            starting_capital_gbp=args.capital or config.STARTING_CAPITAL_GBP,
        )
        result = bt.run()
        eq = result.equity_gbp
        if eq is None or eq.empty:
            print("❌ Backtest produced no equity series.  Aborting.")
            sys.exit(2)

    out = compare_to_benchmark(
        strategy_equity_gbp=eq,
        start=args.start, end=args.end,
        benchmark_ticker=args.benchmark or config.BENCHMARK_TICKER,
        starting_capital_gbp=args.capital or config.STARTING_CAPITAL_GBP,
    )
    print(out["render"])

    # Save the comparison alongside the report dir so it's accessible later
    if args.from_report:
        save_dir = Path("data/reports") / args.from_report
    else:
        save_dir = Path("data/reports") / (args.name or "compare")
        save_dir.mkdir(parents=True, exist_ok=True)
    (save_dir / "comparison.txt").write_text(out["render"])
    print(f"\n📝 Saved → {save_dir / 'comparison.txt'}")


def cmd_compare_variants(args):
    """Run baseline vs variant WFOs and apply the merge criterion."""
    from .compare_variants import compare, FEATURES

    if args.feature not in FEATURES:
        print(f"❌ Unknown feature: {args.feature}")
        print(f"   Known: {list(FEATURES)}")
        sys.exit(2)

    compare(
        feature=args.feature,
        start=args.start, end=args.end,
        is_days=args.is_days, oos_days=args.oos_days, step_days=args.step_days,
        objective=args.objective,
        universe=args.universe, universe_cap=args.universe_cap,
        n_pass=args.n_pass,
    )


def cmd_record_fill(args):
    """Record a manual fill against a previously-sent signal.

    Usage:
        ics record-fill --id 42 --fill 178.42 --shares 25
        ics record-fill --ticker AAPL --fill 178.42        # latest pending AAPL
        ics record-fill --id 42 --missed --notes "in meeting"
    """
    if args.missed:
        outcome = "missed"
        fill = None
        shares = None
    else:
        if args.fill is None:
            print("❌ Either --fill <price> or --missed must be provided.")
            sys.exit(2)
        outcome = "executed"
        fill = args.fill
        shares = args.shares

    if args.id is None and args.ticker is None:
        print("❌ Either --id or --ticker must be provided.")
        sys.exit(2)

    row = db.record_user_execution(
        signal_id=args.id, ticker=args.ticker,
        user_fill_usd=fill, user_shares=shares,
        outcome=outcome, notes=args.notes,
    )
    if row is None:
        print("❌ No matching pending signal found.")
        if args.id is not None:
            print(f"   Signal id {args.id} doesn't exist or already has an outcome.")
        else:
            print(f"   No unresolved signals for {args.ticker!r} found.")
        sys.exit(1)
    slip_pct = row.get("slippage_pct")
    if slip_pct is not None:
        print(f"✓ Recorded fill for #{row['id']} {row['ticker']}: "
              f"slippage {float(slip_pct)*100:+.3f}% "
              f"(expected ${float(row['expected_fill_usd']):.2f}, "
              f"actual ${float(row['user_fill_usd']):.2f})")
    else:
        print(f"✓ Recorded outcome for #{row['id']} {row['ticker']}: {outcome}")


def cmd_slippage_report(args):
    """Render the execution-audit slippage report for the last N days."""
    from .slippage import build_report, format_report
    rep = build_report(days=args.days)
    print(format_report(rep))


def cmd_paper_status(args):
    """Dashboard: paper-trading metrics vs WFO baseline + pass criteria."""
    from .paper_status import evaluate, format_report
    result = evaluate(
        min_days=args.min_days,
        min_trades=args.min_trades,
        sharpe_ratio_threshold=args.sharpe_ratio,
        mdd_ratio_threshold=args.mdd_ratio,
        wfo_name=args.wfo_name,
    )
    print(format_report(result))
    # Non-zero exit when not ready for live, so this can be cron-chained
    if result.overall_pass is False:
        sys.exit(1)


def cmd_revalidate(args):
    """Run a scheduled (or forced) revalidation cycle."""
    from .revalidation import (
        should_revalidate, run_scheduled_revalidation, format_drift_alert,
    )
    decision = should_revalidate()
    if args.force:
        print(f"Forcing revalidation (trigger check said: {decision.reason})")
    elif not decision.should_run:
        print(f"Not due — {decision.reason}")
        print(f"  Use --force to run anyway, or --check to just print the decision.")
        sys.exit(0)
    else:
        print(f"Running revalidation: {decision.reason}")

    if args.check:
        return  # already printed the decision; don't run the WFO

    summary = run_scheduled_revalidation(
        universe=args.universe,
        start=args.start,
        is_days=args.is_days,
        oos_days=args.oos_days,
        step_days=args.step_days,
        notify=not args.no_notify,
        dry_run=args.dry_run,
    )
    print(f"\nRevalidation complete: {summary['name']}")
    if summary.get("drift"):
        d = summary["drift"]
        print(f"  Combo shifts: {d['windows_with_combo_shift']} / "
              f"{d['windows_compared']} windows")
        print(f"  Avg Sharpe Δ: {d['avg_sharpe_change']:+.3f}")


def cmd_mc(args):
    if not Path(args.trades_csv).exists():
        print(f"Trades CSV not found: {args.trades_csv}")
        sys.exit(2)
    trades = pd.read_csv(args.trades_csv)
    if args.mode == "shuffle":
        df = shuffle_mc(trades, runs=args.runs)
    else:
        df = parametric_mc(trades, runs=args.runs)
    print(df.describe(percentiles=[0.05, 0.25, 0.5, 0.75, 0.95]).to_string())
    if args.out:
        df.to_csv(args.out, index=False)
        print(f"Saved -> {args.out}")


def cmd_fulltest(args):
    """One-stop: refresh watchlist, backtest, WFO, MC, write everything."""
    db.init_db()

    if getattr(args, "refresh", True):
        log.info("=== Refreshing watchlist ===")
        watchlist.refresh_watchlist()

    tickers = watchlist.get_tickers() or config.BASE_UNIVERSE[:60]
    log.info("=== FULL TEST on %d tickers ===", len(tickers))

    log.info(">>> 1. Backtest")
    bt_args = argparse.Namespace(
        tickers=None, from_watchlist=True, start=args.start, end=args.end,
        name=args.name or "fulltest_backtest", universe_cap=80, mc=True,
        capital=args.capital,
    )
    cmd_backtest(bt_args)

    if not getattr(args, "no_wfo", False):
        log.info(">>> 2. Walk-Forward Optimisation")
        wfo_args = argparse.Namespace(
            tickers=None, from_watchlist=True, start=args.start, end=args.end,
            name=args.name or "fulltest_wfo", objective="sharpe",
            universe_cap=60, mc=True,
            is_days=504, oos_days=252, step_days=252,
        )
        cmd_wfo(wfo_args)

    log.info("=== FULL TEST DONE ===")


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ics", description="Internal Convergence Scanner bot.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("preflight", help="Run startup checks; non-zero exit if any fail.")
    sp.set_defaults(func=cmd_preflight)

    sp = sub.add_parser("refresh-watchlist", help="Refresh the dynamic watchlist.")
    sp.set_defaults(func=cmd_refresh_watchlist)

    sp = sub.add_parser("scan", help="Run one live scan now.")
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("live", help="Run the live engine.")
    sp.set_defaults(func=cmd_live)

    sp = sub.add_parser("backtest", help="Run a backtest over a date range.")
    sp.add_argument("--tickers", help="Comma-separated tickers (overrides watchlist).")
    sp.add_argument("--from-watchlist", action="store_true",
                    help="Use the saved watchlist as the universe.")
    sp.add_argument("--universe-cap", type=int, default=80,
                    help="Cap on BASE_UNIVERSE if no tickers/watchlist given.")
    sp.add_argument("--start", default="2019-01-01")
    sp.add_argument("--end", default=None)
    sp.add_argument("--capital", type=float, default=None,
                    help="Starting capital in GBP (default: config.STARTING_CAPITAL_GBP).")
    sp.add_argument("--name", default=None, help="Run name for the report folder.")
    sp.add_argument("--mc", action="store_true", help="Also run Monte Carlo.")
    sp.add_argument("--no-contributions", action="store_true",
                    dest="no_contributions",
                    help="Disable monthly £750 (or configured) contributions.")
    sp.add_argument("--contribution-gbp", type=float, default=None,
                    dest="contribution_gbp",
                    help="Override monthly contribution amount in GBP "
                         "(default from config; standing-order day = last Friday).")
    sp.set_defaults(func=cmd_backtest)

    sp = sub.add_parser("wfo", help="Walk-forward optimisation.")
    sp.add_argument("--tickers")
    sp.add_argument("--from-watchlist", action="store_true")
    sp.add_argument("--universe-cap", type=int, default=60)
    sp.add_argument("--start", default="2019-01-01")
    sp.add_argument("--end", default=None)
    sp.add_argument("--objective", default="sharpe",
                    choices=["sharpe", "cagr", "calmar", "profit_factor"])
    sp.add_argument("--is-days", type=int, default=504, dest="is_days",
                    help="In-sample window length (calendar days; ~504 ≈ 2y).")
    sp.add_argument("--oos-days", type=int, default=252, dest="oos_days",
                    help="Out-of-sample window length (calendar days; ~252 ≈ 1y).")
    sp.add_argument("--step-days", type=int, default=252, dest="step_days",
                    help="Days to advance between windows (default 252 = 1y).")
    sp.add_argument("--name", default=None)
    sp.add_argument("--mc", action="store_true")
    sp.add_argument("--universe", default="nasdaq100", choices=["nasdaq100", "sp500"],
                    help="Point-in-time universe (default: nasdaq100). "
                         "sp500 covers 1996-onwards but is slower (5x more tickers).")
    sp.set_defaults(func=cmd_wfo)

    sp = sub.add_parser("multi-wfo", help="Run WFO under all 4 objectives and compare OOS results.")
    sp.add_argument("--start",     default="2019-01-01")
    sp.add_argument("--end",       default=None)
    sp.add_argument("--is-days",   type=int, default=504, dest="is_days")
    sp.add_argument("--oos-days",  type=int, default=252, dest="oos_days")
    sp.add_argument("--step-days", type=int, default=252, dest="step_days")
    sp.add_argument("--capital",   type=float, default=None)
    sp.add_argument("--universe",  default="nasdaq100", choices=["nasdaq100", "sp500"],
                    help="Point-in-time universe (default: nasdaq100).")
    sp.add_argument("--universe-cap", type=int, default=105, dest="universe_cap",
                    help="Max tickers per IS/OOS window (default 105 for NDX; "
                         "for SPX consider lower, e.g. 100, to keep WFO compute manageable).")
    sp.add_argument(
        "--objectives", nargs="+", default=OBJECTIVES, choices=OBJECTIVES_ALL,
        help=(f"Which objectives to run (default: {' '.join(OBJECTIVES)}; "
              "pass 'cagr' to include it as an opt-in)."),
    )
    sp.set_defaults(func=cmd_multi_wfo)

    sp = sub.add_parser("compare",
                        help=f"Compare strategy to {config.BENCHMARK_TICKER} buy-and-hold.")
    sp.add_argument("--from-report", default=None, dest="from_report",
                    help="Load equity from data/reports/<NAME>/equity_gbp.csv "
                         "instead of running a fresh backtest.")
    sp.add_argument("--tickers")
    sp.add_argument("--from-watchlist", action="store_true")
    sp.add_argument("--start", default="2019-01-01")
    sp.add_argument("--end", default=None)
    sp.add_argument("--capital", type=float, default=None)
    sp.add_argument("--benchmark", default=None,
                    help=f"Override benchmark ticker (default {config.BENCHMARK_TICKER}).")
    sp.add_argument("--name", default=None,
                    help="Report name (only used when running a fresh backtest).")
    sp.set_defaults(func=cmd_compare)

    sp = sub.add_parser("compare-variants",
                        help="Run baseline vs variant WFOs and apply the merge criterion.")
    sp.add_argument("feature", choices=["vol_targeting", "mean_reversion"],
                    help="Which additive feature to test.")
    sp.add_argument("--start", default="2019-01-01")
    sp.add_argument("--end", default=None)
    sp.add_argument("--is-days", type=int, default=504, dest="is_days")
    sp.add_argument("--oos-days", type=int, default=252, dest="oos_days")
    sp.add_argument("--step-days", type=int, default=252, dest="step_days")
    sp.add_argument("--objective", default="sharpe",
                    choices=["sharpe", "cagr", "calmar", "profit_factor"])
    sp.add_argument("--universe", default="nasdaq100", choices=["nasdaq100", "sp500"])
    sp.add_argument("--universe-cap", type=int, default=105, dest="universe_cap")
    sp.add_argument("--n-pass", type=int, default=5, dest="n_pass",
                    help="Min windows where variant must improve BOTH Sharpe AND Calmar (default 5).")
    sp.set_defaults(func=cmd_compare_variants)

    sp = sub.add_parser("record-fill",
                        help="Record an actual fill against a previously-sent signal.")
    sp.add_argument("--id", type=int, default=None,
                    help="Signal id from the audit (#42 in the Telegram alert).")
    sp.add_argument("--ticker", default=None,
                    help="Alternative: use latest pending alert for this ticker.")
    sp.add_argument("--fill", type=float, default=None,
                    help="Actual USD fill price.")
    sp.add_argument("--shares", type=int, default=None,
                    help="Actual share count filled (defaults to planned).")
    sp.add_argument("--missed", action="store_true",
                    help="Mark as missed instead of recording a fill.")
    sp.add_argument("--notes", default=None, help="Free-text notes.")
    sp.set_defaults(func=cmd_record_fill)

    sp = sub.add_parser("slippage-report",
                        help="Aggregate execution-audit slippage stats.")
    sp.add_argument("--days", type=int, default=30,
                    help="Window of days to include (default 30).")
    sp.set_defaults(func=cmd_slippage_report)

    sp = sub.add_parser("paper-status",
                        help="Dashboard: paper-trading metrics vs WFO baseline.")
    sp.add_argument("--min-days", type=int, default=30, dest="min_days",
                    help="Days of paper data required before judging (default 30).")
    sp.add_argument("--min-trades", type=int, default=30, dest="min_trades",
                    help="Paper trades required before judging (default 30).")
    sp.add_argument("--sharpe-ratio", type=float, default=0.5,
                    help="Paper Sharpe / WFO Sharpe pass ratio (default 0.5).")
    sp.add_argument("--mdd-ratio", type=float, default=1.5,
                    help="Paper MDD / WFO MDD pass ratio (default 1.5).")
    sp.add_argument("--wfo-name", default=None, dest="wfo_name",
                    help="Specific WFO report to compare against; defaults to latest.")
    sp.set_defaults(func=cmd_paper_status)

    sp = sub.add_parser("revalidate",
                        help="Run scheduled revalidation: WFO + drift diff + alert.")
    sp.add_argument("--universe", default="nasdaq100",
                    choices=["nasdaq100", "sp500"])
    sp.add_argument("--start", default="2020-01-01")
    sp.add_argument("--is-days", type=int, default=504, dest="is_days")
    sp.add_argument("--oos-days", type=int, default=252, dest="oos_days")
    sp.add_argument("--step-days", type=int, default=252, dest="step_days")
    sp.add_argument("--force", action="store_true",
                    help="Run even if not due per cadence.")
    sp.add_argument("--check", action="store_true",
                    help="Just print whether a revalidation is due, don't run.")
    sp.add_argument("--no-notify", action="store_true",
                    help="Don't send Telegram alert.")
    sp.add_argument("--dry-run", action="store_true", dest="dry_run",
                    help="Skip the WFO compute, just exercise the diff+alert path.")
    sp.set_defaults(func=cmd_revalidate)

    sp = sub.add_parser("mc", help="Monte Carlo on a saved trade list.")
    sp.add_argument("trades_csv")
    sp.add_argument("--mode", default="parametric", choices=["parametric", "shuffle"])
    sp.add_argument("--runs", type=int, default=2000)
    sp.add_argument("--out", default=None)
    sp.set_defaults(func=cmd_mc)

    sp = sub.add_parser("fulltest", help="One-shot full test (refresh + bt + wfo + mc).")
    sp.add_argument("--start", default="2019-01-01")
    sp.add_argument("--end", default=None)
    sp.add_argument("--capital", type=float, default=None)
    sp.add_argument("--name", default=None)
    sp.add_argument("--no-wfo", action="store_true")
    sp.add_argument("--no-refresh", action="store_false", dest="refresh")
    sp.set_defaults(func=cmd_fulltest, refresh=True)

    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
