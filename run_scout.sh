#!/bin/bash
cd /home/kenzodadon/base-trend-bot
OUT=$(python3 base_scout.py cron 2>&1 || true)
if echo "$OUT" | grep -q '"status": "ALERT_TRIGGERED"'; then
  echo "$OUT"
fi
