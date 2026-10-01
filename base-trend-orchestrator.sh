#!/usr/bin/env bash
# base-trend-orchestrator.sh — coordinates discovery, macro, sentiment every 15m
# Replaces 5 individual cron jobs: discovery, execute, monitor, sentiment, macro
set -euo pipefail
cd /home/kenzodadon/base-trend-bot || exit 1

echo "=== ORCHESTRATOR tick $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

# 1. Discovery (trend_discovery.py writes discovered.jsonl)
echo "[1/4] Running trend discovery..."
python3 trend_discovery.py 2>&1 | tail -10

# 2. Macro regime check (runs every 4h but safe to call - has internal gate)
echo "[2/4] Checking macro regime..."
python3 macro_regime.py 2>&1 | tail -5

# 3. Nous sentiment gate (runs every 15m, rate-limits internally)
echo "[3/4] Running Nous sentiment gate..."
python3 nous_sentiment_gate.py 2>&1 | tail -10

# 4. Check caps.json status
echo "[4/4] Checking caps.json..."
python3 -c "
import json
with open('caps.json') as f:
    caps = json.load(f)
print(f\"  live_trading={caps.get('live_trading')} kill_switch={caps.get('kill_switch')}\")
print(f\"  arm_provenance={caps.get('arm_provenance')}\")
print(f\"  edge_evidence={caps.get('edge_evidence')}\")
" 2>&1

echo "=== ORCHESTRATOR complete ==="