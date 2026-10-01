#!/bin/bash
# fix_real_bot_idempotent.sh - CORRECTED version for /home/kenzodadon/base-trend-bot/
# Fixes: shell glue bugs + data-shape bug ({} vs {"open":{},"closed":[]})
set -e

BOT_DIR="/home/kenzodadon/base-trend-bot"
cd "$BOT_DIR"

echo "=== REAL bot at $BOT_DIR ==="

# Backup
TS=$(date +%s)
cp caps.json "caps.json.bak.$TS"
if [ -f shadow_book.json ]; then
  cp shadow_book.json "shadow_book.json.bak.$TS"
fi
if [ -f shadow-book.json ]; then
  cp shadow-book.json "shadow-book.json.bak.$TS"
fi

# 1. caps.json balance
python3 - << 'PY'
import json, pathlib
p = pathlib.Path("caps.json")
caps = json.loads(p.read_text())
before = caps.get("wallet",{}).get("balance_eth")
print(f"Before caps.json wallet.balance_eth: {before}")
# On-chain 0.0052676 ETH at 50366274, stale file said 0.002336
if "wallet" in caps and isinstance(caps["wallet"], dict):
    if "balance_eth" in caps["wallet"]:
        caps["wallet"]["balance_eth"] = "0.0052676"
        caps["wallet"]["balance_eth_updated_block"] = 50366274
        caps["wallet"]["balance_eth_updated"] = "idempotent fix"
p.write_text(json.dumps(caps, indent=2))
after = caps.get("wallet",{}).get("balance_eth")
print(f"After caps.json wallet.balance_eth: {after}")
PY

# 2. shadow_book.json - MUST be {"open":{},"closed":[]} not {}
python3 - << 'PY'
import json, pathlib
for name in ["shadow_book.json", "shadow-book.json"]:
    p = pathlib.Path(name)
    if not p.exists():
        continue
    data = json.loads(p.read_text())
    # Determine if phantom: check if open dict has keys
    open_keys = []
    if isinstance(data, dict):
        if "open" in data and isinstance(data["open"], dict):
            open_keys = list(data["open"].keys())
        else:
            open_keys = list(data.keys())
    print(f"Before {name}: open={open_keys} -> purging phantom (on-chain 0)")
    # Correct shape expected by shadow_book.py: book["open"] must exist
    fixed = {"open": {}, "closed": []}
    p.write_text(json.dumps(fixed, indent=2))
    print(f"After {name}: {fixed}")
    break
else:
    print("shadow_book.json not found, listing json files:")
    print([f.name for f in pathlib.Path(".").glob("*.json")])
PY

echo ""
echo "=== Verification ==="
cat caps.json | grep -A3 -B1 balance_eth || cat caps.json
echo ""
cat shadow_book.json 2>/dev/null || cat shadow-book.json 2>/dev/null
echo ""
echo "Done - idempotent, safe to re-run"
