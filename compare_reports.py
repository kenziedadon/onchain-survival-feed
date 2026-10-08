#!/usr/bin/env python3
import json

# Read previous report
with open('/home/kenzodadon/base-trend-bot/.last_shadow_report', 'r') as f:
    prev_content = f.read()

# Read current report
current_report = '''============================================================
  SHADOW MODE REPORT
  Generated: 2026-10-02 23:46 UTC
============================================================

GATE STATE:
  kill_switch:   ARMED (safe)
  live_trading:  off
  edge_pass:     True
  mode:          DISARMED

SHADOW BOOK (0 open, 68 closed):

  Closed: 68 total | 3W / 26L / 39BE
  Avg PnL: -0.08%
  Win rate: 4%

  Last 5 closed:
    Surplus                   -0.14%  skip_no_signal
    VCAT                      -0.10%  skip_no_signal
    ATLAS                     -0.65%  skip_no_signal
    AERO / WETH 0.3%          -0.06%  skip_no_signal
    eUSD                      -0.00%  skip_no_signal

TELEMETRY (last 7 days, 429 records):
  By type:
    shadow_entry                     194
    skip_no_signal                   188
    shadow_exit                       35
    skip_not_on_sale                   6
    risk_gate_blocked                  6

  Shadow entries: 194 total
  By skip reason:
    skip_no_signal                             188
    skip_not_on_sale:BB%B=0.86                   1
    skip_not_on_sale:BB%B=0.80                   1
    skip_not_on_sale:BB%B=0.78                   1
    skip_not_on_sale:BB%B=0.71                   1
    skip_not_on_sale:BB%B=0.83                   1
    skip_not_on_sale:BB%B=0.76                   1

  Activity by day:
    2026-09-28   137 events
    2026-09-29   121 events
    2026-09-30   105 events
    2026-10-01    30 events
    2026-10-02    36 events

  EMA crossover: 0 skips (no signal)

  Most scanned tokens (last 7d):
    Surplus                      91 scans
    VCAT                         88 scans
    GITLAWB                      82 scans
    NOCK (manual)                70 scans
    AERO / WETH 0.3%             69 scans
    ATLAS                        15 scans
    LAPTOP (manual)               7 scans
    eUSD                          7 scans

============================================================
SUMMARY:
  🟢 Bot is DISARMED — shadow mode only
  🔴 Shadow PnL negative (-0.1%, 4% win)
  Shadow book: 0 open / 68 closed
============================================================'''

# Parse previous report
prev_data = json.loads(prev_content)
prev_report = prev_data['output']

# Normalize reports (remove timestamps for comparison)
def normalize(text):
    lines = text.split('\n')
    normalized = []
    for line in lines:
        # Skip timestamp line
        if 'Generated:' in line:
            continue
        normalized.append(line)
    return '\n'.join(normalized)

prev_normalized = normalize(prev_report)
curr_normalized = normalize(current_report)

if prev_normalized == curr_normalized:
    print('IDENTICAL')
else:
    print('CHANGED')
    # Show diff
    import difflib
    diff = difflib.unified_diff(prev_normalized.split('\n'), curr_normalized.split('\n'), lineterm='')
    print('\n'.join(diff))