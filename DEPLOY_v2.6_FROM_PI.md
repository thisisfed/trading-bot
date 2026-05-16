# Deploying v2.6 onto the Pi

This release contains:
- Two changed Python files (`constituents.py` and `watchlist.py`)
- Two CHANGELOGs (this file and `CHANGELOG_v2.6.md`)
- A clean test suite (202 tests)

It deliberately does NOT contain `data/sp500_constituents.csv`. Your
Pi already has the correct cleaned dated version at
`~/data/sp500_constituents.csv` (verified yesterday — TSLA correctly
absent from 2020-06-01 membership, etc.). Shipping a CSV in this
release would risk overwriting your good one with whatever this zip
was built from, which may have been the un-dated original (dirty
`-YYYYMM` schema). Your CSV stays untouched.

## Production config you should NOT change

After the deploy, leave these untouched:

```python
# ics/config.py
UNIVERSE_SOURCE.mode = "ndx_pit"     # production stays on NDX
REVALIDATION.auto_apply = False       # never enable
```

The S&P 500 experiment confirmed the strategy is NDX-specific. Don't
flip the mode to `"sp500_pit"` based on the misconfigured experiments
from yesterday — they didn't measure what we thought they did.

## Sequence

```bash
# 1. Get the v2.6 zip onto the Pi
#    (scp from your laptop, or download via curl if you have a URL).
#    This example assumes you've put it at ~/thisisfed_v2.6.zip.

# 2. Stop the live bot if it's running
pkill -f "ics.cli live"; sleep 3
ps aux | grep "ics.cli live" | grep -v grep
# (empty output = bot stopped)

# 3. Back up the current state
cd ~
cp -r ics ics.bak_$(date +%Y%m%d_%H%M)

# 4. Extract v2.6 into a temp location
mkdir -p ~/upgrade_v2.6
cd ~/upgrade_v2.6
unzip ~/thisisfed_v2.6.zip
# This creates ~/upgrade_v2.6/work/ which mirrors the new release.

# 5. Copy the two changed files into place
cp ~/upgrade_v2.6/work/ics/constituents.py ~/ics/constituents.py
cp ~/upgrade_v2.6/work/ics/watchlist.py    ~/ics/watchlist.py

# 6. Copy the changelog (helpful for reference, not required)
cp ~/upgrade_v2.6/work/CHANGELOG_v2.6.md       ~/ics/
cp ~/upgrade_v2.6/work/DEPLOY_v2.6_FROM_PI.md  ~/ics/

# 7. Verify import surface is clean
cd ~
source ~/.venv/bin/activate
python -c "
from ics.constituents import check_library, get_universe_at
from ics.sp500_constituents import check_library as check_spx, get_universe_at as get_spx
from ics.watchlist import _resolve_base_universe
from ics.config import UNIVERSE_SOURCE
print('NDX library:    ', check_library())
print('SPX dataset:    ', check_spx())
print('Universe mode:  ', UNIVERSE_SOURCE.mode)
print()
print('NDX universe today:', len(get_universe_at('2026-05-12')))
print('SPX universe today:', len(get_spx('2026-05-12')))
print()
# Critical point-in-time spot check
t = get_spx('2020-06-01')
print(f'SPX 2020-06-01: {len(t)} tickers')
print(f'  TSLA (should be False): {\"TSLA\" in t}')
print(f'  META (should be False): {\"META\" in t}')
print(f'  FB (should be True):    {\"FB\" in t}')
"

# Expected output:
#   NDX library:     True
#   SPX dataset:     True
#   Universe mode:   ndx_pit
#   NDX universe today: ~100
#   SPX universe today: ~503
#   SPX 2020-06-01: 505 tickers
#     TSLA (should be False): False
#     META (should be False): False
#     FB (should be True):    True

# 8. Run the test suite to confirm nothing broke
python -m pytest ~/upgrade_v2.6/work/tests/ -q --no-header 2>&1 | tail -3
# Should show: 202 passed (or 198 + 4 skipped, depending on cache state)

# 9. Restart the live bot
cd ~/ics  # or your project root
mkdir -p logs
nohup python -m ics.cli live > logs/ics.log 2>&1 &
sleep 5
ps aux | grep "ics.cli live" | grep -v grep
tail -20 logs/ics.log

# 10. (Optional) Cleanup
rm -rf ~/upgrade_v2.6
```

## Sanity check after restart

From your phone:

- Send `/ping` — should reply `🏓 pong`
- Send `/help` — should see grouped sections, no leading whitespace,
  real descriptions (not "registered handler")
- Send `/equity` — should see real-time per-position marks with
  LIVE/CLOSE freshness banner
- Send `/regime` — should see no `<b>...</b>` tags

If any of those look wrong, the v2.5.4 fixes didn't apply correctly.
You can roll back from the backup directory created in step 3:

```bash
pkill -f "ics.cli live"; sleep 3
mv ~/ics ~/ics.bad
mv ~/ics.bak_* ~/ics
nohup python -m ics.cli live > ~/ics/logs/ics.log 2>&1 &
```

## What NOT to do for the next 30 days

- Don't run more WFO experiments. The validation done in this session
  is the answer.
- Don't change `UNIVERSE_SOURCE.mode`. Stay on `ndx_pit`.
- Don't change `REVALIDATION.auto_apply`. Keep it False.
- Don't add strategy features. The list of "could we improve X"
  experiments is closed for at least 30 days.

## What TO do for the next 30 days

- Run the bot. Receive Telegram alerts.
- Reply `/done <id> <fill>` for every trade you take.
- Reply `/missed <id> <notes>` for every signal you skip.
- Every 7 days, run `python -m ics.cli slippage-report --days 7` and
  look at the median slippage and execution rate.

After 30 days you'll have enough fills to know what your live-vs-backtest
gap actually is. THAT is the data that turns the 8.5/10 rating into
either a 9.0 ("strategy validated in practice") or a downward revision
("execution gap too large to capture the backtest edge").

The strategy is done. The next chapter is operator discipline.
