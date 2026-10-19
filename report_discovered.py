import json
from collections import defaultdict
from _tolerant_read import iter_jsonl_entries

latest = {}
for rec in iter_jsonl_entries('/home/kenzodadon/base-trend-bot/discovered.jsonl'):
    ts = rec.get('ts','')
    latest[ts] = rec

if not latest:
    print("[SILENT] - no discovered.jsonl data")
    exit()

last_ts = max(latest.keys())
last = latest[last_ts]
coins = last.get('coins', [])
print(f"=== LAST DISCOVERY SCAN ===")
print(f"Timestamp: {last_ts}")
print(f"Coins discovered this cycle: {len(coins)}")
print()
for c in sorted(coins, key=lambda x: x.get('score',0), reverse=True):
    name = c.get('name','?')
    score = c.get('score',0)
    liq = c.get('liquidity',0)
    vol24 = c.get('vol_24h',0)
    chg = c.get('change_24h',0)
    price = c.get('price_usd',0)
    addr = c.get('token_addr','?')[:10]
    print(f"  {name:25s} score={score:3d} liq=${liq/1e6:.1f}M 24h=${vol24/1e3:.0f}k chg={chg:+.1f}% price=${price:.6f} addr={addr}...")

print()
print("=== UNIQUE COINS IN LAST 10 SCANS ===")
unique = {}
for ts in sorted(latest.keys())[-10:]:
    rec = latest[ts]
    for c in rec.get('coins', []):
        addr = c.get('token_addr')
        if addr and addr not in unique:
            unique[addr] = c

for addr, c in sorted(unique.items(), key=lambda x: x[1].get('score',0), reverse=True):
    name = c.get('name','?')
    score = c.get('score',0)
    liq = c.get('liquidity',0)
    vol24 = c.get('vol_24h',0)
    chg = c.get('change_24h',0)
    print(f"  {name:25s} score={score:3d} liq=${liq/1e6:.1f}M 24h=${vol24/1e3:.0f}k chg={chg:+.1f}%")
