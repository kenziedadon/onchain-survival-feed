#!/usr/bin/env bash
# run_execute.sh — run executor.py to trade from discovered.jsonl
# Called by the base-trend-trade-execute cron every 15m (staggered +1m after discovery).
# Dry-run unless live_trading=true AND kill_switch=false in caps.json.
set -euo pipefail
cd /home/kenzodadon/base-trend-bot || exit 1
# Source .env to make GMGN_API_KEY and other env vars available
set -a
source .env 2>/dev/null || true
set +a
python3 executor.py 2>&1
