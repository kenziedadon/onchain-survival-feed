#!/usr/bin/env bash
# run_discovery.sh — scan trending Base pools and write discovered.jsonl
# Called by the base-trend-discovery cron every 15m.
# Pure stdlib, keyless GeckoTerminal + DEXScreener. Writes discovered.jsonl
# for executor.py and backtest.py to consume.
set -euo pipefail
cd /home/kenzodadon/base-trend-bot || exit 1
python3 trend_discovery.py 2>&1
echo "--- discovery complete, latest snapshot in discovered.jsonl ---"
