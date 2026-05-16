"""
run_wfo_mc.py — Final working Monte Carlo script
Just run: python run_wfo_mc.py
"""

import sys
from pathlib import Path

# === FORCE PATH CORRECTLY ===
root = Path(__file__).resolve().parent
sys.path.insert(0, str(root))           # Add ics/ folder
sys.path.insert(0, str(root.parent))    # Add parent folder as backup

import pandas as pd

# Import directly from files
import montecarlo
import reporter

parametric_mc = montecarlo.parametric_mc
shuffle_mc = montecarlo.shuffle_mc
write_report = reporter.write_report


def main():
    report_dir = root / "data" / "reports"
    wfo_folders = sorted(report_dir.glob("wfo_pro*"), reverse=True)

    if not wfo_folders:
        print("❌ No WFO results found.")
        print("Run this first:")
        print("   python -m ics.cli wfo --from-watchlist --start 2010-01-01 --name pro_wfo")
        return

    latest = wfo_folders[0]
    print(f"📊 Using WFO: {latest.name}")

    # Find trades
    trades_files = list(latest.glob("**/*trade*.csv")) + list(latest.glob("**/*oos*.csv"))
    if not trades_files:
        print("❌ No trades file found.")
        return

    trades = pd.read_csv(trades_files[0])
    print(f"✅ Loaded {len(trades):,} OOS trades\n")

    print("🔄 Running Parametric Monte Carlo (2000 runs)...")
    parametric = parametric_mc(trades, runs=2000)

    print("🔀 Running Shuffle Monte Carlo (1000 runs)...")
    shuffle = shuffle_mc(trades, runs=1000)

    write_report(
        run_name=f"{latest.name}_MC",
        equity_gbp=pd.Series(),
        trades=trades,
        summary={},
        mc_results=parametric
    )

    p = parametric
    print("\n" + "="*90)
    print("🎯 MONTE CARLO ROBUSTNESS REPORT")
    print("="*90)
    print(f"Starting Capital          : £30,000")
    print(f"Median Final Equity       : £{p['end_equity_gbp'].median():,.0f}")
    print(f"5th Percentile (Worst)    : £{p['end_equity_gbp'].quantile(0.05):,.0f}")
    print(f"95th Percentile (Best)    : £{p['end_equity_gbp'].quantile(0.95):,.0f}")
    print(f"Median CAGR               : {p['cagr_pct'].median()*100:.1f}%")
    print(f"Median Max Drawdown       : {p['max_drawdown_pct'].median()*100:.1f}%")
    print(f"Worst 5% Drawdown         : {p['max_drawdown_pct'].quantile(0.05)*100:.1f}%")

    print(f"\nProbability of Doubling (£60k+) : {(p['end_equity_gbp'] > 60000).mean()*100:.1f}%")
    print(f"Probability of Loss (< £30k)    : {(p['end_equity_gbp'] < 30000).mean()*100:.1f}%")

    print(f"\n✅ Report saved in: data/reports/{latest.name}_MC/")


if __name__ == "__main__":
    main()
