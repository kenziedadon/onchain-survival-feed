#!/usr/bin/env bash
# enable_broadcast.sh — the SINGLE deliberate go-live step.
#
# This is the only supported way to turn off the kill_switch and let the bot
# stage+submit trades. It is intentionally interactive and refuses to proceed
# unless you acknowledge two hard facts:
#
#   1. The trading wallet is a BARE EOA (0xe7a16B...e540F), NOT a smart wallet.
#      It has NO native session-key / tap-approve. Actual on-chain broadcast
#      requires a signing path you must connect yourself (Coinbase session,
#      or a key you supply). This script does NOT create that path.
#   2. This script is now the ONLY path that sets live_trading=true. The
#      monitor (arm_check.py) records edge evidence but NEVER arms on its own
#      (true Level-1: a human must run this script to go live). Per-tx human
#      confirmation is retained (auto_confirm=False) so each broadcast still
#      needs a tap.
#
# Safe by construction: it flips kill_switch=false AND arms via risk.arm_signal()
# using the monitor's recorded edge_evidence. The monitor cannot set
# live_trading on its own.
set -euo pipefail
cd /home/kenzodadon/base-trend-bot || exit 1

WALLET=$(python3 -c "import json;print(json.load(open('wallet.json'))['trading_address'])")
echo "=========================================================="
echo "  GO-LIVE STEP — enable broadcast"
echo "  trading wallet : $WALLET"
echo "  wallet type    : BARE EOA (no smart-wallet session key)"
echo "=========================================================="
echo
echo "ACKNOWLEDGE before proceeding (type the exact phrase when prompted):"
echo "  - The wallet above is a bare EOA with NO native tap-approve."
echo "  - Actual broadcast needs a signing path you connect (Coinbase/session/key)."
echo "  - This script ARMS the bot (live_trading=true) using the monitor's edge."
echo "  - Per-tx human confirmation is still required (no silent auto-submit)."
echo

read -r -p 'Type "I ACKNOWLEDGE THE EOA HAS NO SESSION KEY" to continue: ' ACK
if [ "$ACK" != "I ACKNOWLEDGE THE EOA HAS NO SESSION KEY" ]; then
  echo "Aborted — acknowledgement not matched. kill_switch left ON. Nothing changed."
  exit 1
fi

# Flip the kill switch OFF, then explicitly ARM (Level-1: human-initiated only).
# arm_signal() sets live_trading=true using the monitor's recorded edge_evidence.
# Per-trade human confirmation is retained (auto_confirm=False).
python3 - <<'PY'
import risk, json
risk.unkill()
c = risk.load_caps()
ev = c.get("edge_evidence") or {}
grid = bool(ev.get("grid_pass"))
early = bool(ev.get("early_edge_pass"))
meth = bool(ev.get("methodology_pass"))
if not (grid or early or meth):
    print("  NOTE: no passing edge_evidence from monitor; arming on explicit human request.")
risk.arm_signal(grid_pass=grid, early_edge_pass=early, methodology_pass=meth,
                detail="explicit human arm via enable_broadcast.sh", auto_confirm=False)
c = risk.load_caps()
print(f"  kill_switch      = {c['kill_switch']}")
print(f"  live_trading     = {c['live_trading']}  (armed by explicit human action)")
print(f"  require_conf     = {c.get('require_confirmation')}  (per-tx tap retained)")
PY

echo
echo "kill_switch is now OFF and the bot is ARMED (live_trading=true) by your explicit"
echo "action. Each staged trade still requires a per-tx human confirmation tap."
echo "To revert at any time:  python3 -c 'import risk; risk.kill()'"
