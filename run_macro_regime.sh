#!/bin/bash
set -euo pipefail
cd /home/kenzodadon/base-trend-bot
python3 macro_regime.py | tee /tmp/base-macro-regime.txt
tail -n 1 /tmp/base-macro-regime.txt
