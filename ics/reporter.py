"""
reporter.py
-----------
Produce reports for backtest / WFO / MC results.

Outputs (under data/reports/<run_name>/):
  summary.txt
  equity_curve.png  (strategy vs benchmark in GBP)
  drawdown.png
  trades.csv
  mc_distribution.png  (if MC results provided)
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import matplotlib

matplotlib.use("Agg")  # headless for Pi
import matplotlib.pyplot as plt
import pandas as pd

from . import config
from .logging_utils import get_logger

log = get_logger("ics.reporter")


def _report_dir(run_name: str) -> Path:
    p = config.DATA_DIR / "reports" / run_name
    p.mkdir(parents=True, exist_ok=True)
    return p


def _format_summary(summary: dict) -> str:
    lines = ["=== ICS Backtest Summary ==="]
    for k, v in summary.items():
        if isinstance(v, (list, dict, pd.Series, pd.DataFrame)):
            # Skip non-scalar fields — they don't render usefully here.
            continue
        if isinstance(v, float):
            if any(x in k for x in ("pct", "cagr", "drawdown", "rate")):
                lines.append(f"{k:36s}: {v*100:>12.2f} %")
            elif k.endswith("_gbp") or k.startswith("avg_") or k.endswith("_equity_gbp"):
                lines.append(f"{k:36s}: {v:>12,.2f}")
            else:
                lines.append(f"{k:36s}: {v:>12.4f}")
        else:
            lines.append(f"{k:36s}: {v}")
    return "\n".join(lines)


def write_report(
    run_name: str,
    equity_gbp: pd.Series,
    trades: pd.DataFrame,
    summary: dict,
    benchmark_compare: Optional[pd.DataFrame] = None,
    mc_results: Optional[pd.DataFrame] = None,
    contributions_gbp: Optional[pd.Series] = None,
) -> Path:
    out = _report_dir(run_name)

    txt = _format_summary(summary)
    (out / "summary.txt").write_text(txt)

    if not trades.empty:
        trades.to_csv(out / "trades.csv", index=False)

    if contributions_gbp is not None and not contributions_gbp.empty:
        contributions_gbp.to_frame(name="contribution_gbp").to_csv(
            out / "contributions_gbp.csv"
        )

    if not equity_gbp.empty:
        # Save equity series so `ics compare` can re-render against a different
        # benchmark or different date window without re-running the backtest.
        equity_gbp.to_frame(name="equity_gbp").to_csv(out / "equity_gbp.csv")

        fig, ax = plt.subplots(figsize=(10, 5))
        ax.plot(equity_gbp.index, equity_gbp.values,
                label="ICS Strategy (GBP)", linewidth=1.5)
        if (benchmark_compare is not None and not benchmark_compare.empty
                and "benchmark_gbp" in benchmark_compare):
            ax.plot(benchmark_compare.index, benchmark_compare["benchmark_gbp"],
                    label=f"Buy & Hold {config.BENCHMARK_TICKER} (GBP)",
                    linewidth=1.2, alpha=0.85)
        ax.set_title(f"Equity Curve — {run_name}")
        ax.set_ylabel("Equity (GBP)")
        ax.set_xlabel("Date")
        ax.grid(alpha=0.25)
        ax.legend(loc="upper left")
        fig.tight_layout()
        fig.savefig(out / "equity_curve.png", dpi=120)
        plt.close(fig)

        running_max = equity_gbp.cummax()
        dd = (equity_gbp / running_max - 1.0) * 100
        fig, ax = plt.subplots(figsize=(10, 3))
        ax.fill_between(dd.index, dd.values, 0, color="crimson", alpha=0.5)
        ax.set_title(f"Underwater Curve — {run_name}")
        ax.set_ylabel("Drawdown (%)")
        ax.grid(alpha=0.25)
        fig.tight_layout()
        fig.savefig(out / "drawdown.png", dpi=120)
        plt.close(fig)

    if mc_results is not None and not mc_results.empty:
        fig, axes = plt.subplots(1, 3, figsize=(14, 4))
        for ax, col, title in zip(
            axes,
            ["end_equity_gbp", "cagr_pct", "max_drawdown_pct"],
            ["End Equity (GBP)", "CAGR", "Max Drawdown"],
        ):
            if col not in mc_results.columns:
                continue
            vals = mc_results[col].dropna()
            ax.hist(vals, bins=40, color="steelblue", alpha=0.85)
            ax.axvline(vals.median(), color="black", linestyle="--", linewidth=1,
                       label=f"median {vals.median():.3f}")
            ax.axvline(vals.quantile(0.05), color="red", linestyle=":", linewidth=1,
                       label=f"p5 {vals.quantile(0.05):.3f}")
            ax.axvline(vals.quantile(0.95), color="green", linestyle=":", linewidth=1,
                       label=f"p95 {vals.quantile(0.95):.3f}")
            ax.set_title(title)
            ax.legend(fontsize=8)
            ax.grid(alpha=0.25)
        fig.suptitle(f"Monte Carlo distribution ({len(mc_results)} runs)")
        fig.tight_layout()
        fig.savefig(out / "mc_distribution.png", dpi=120)
        plt.close(fig)

    log.info("Report written -> %s", out)
    return out


def print_summary(summary: dict) -> None:
    print(_format_summary(summary))
