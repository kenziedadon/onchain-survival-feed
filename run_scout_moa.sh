#!/bin/bash
# On-demand MoA-enriched scout run. Unlike run_scout.sh (silent cron monitor),
# this enriches any fired alert with a synthesized signal-vs-noise verdict from
# the nous-free MoA pool. Use for manual review, not the 15-min cron.
cd /home/kenzodadon/base-trend-bot
python3 base_scout.py cron --moa
