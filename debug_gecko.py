#!/usr/bin/env python3
"""Debug GeckoTerminal trending pools structure."""
import json
import urllib.request

url = "https://api.geckoterminal.com/api/v2/networks/base/trending_pools?page=1"
req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
with urllib.request.urlopen(req, timeout=30) as resp:
    data = json.loads(resp.read())

print("=== TOP-LEVEL KEYS ===")
print(list(data.keys()))
print()

pools = data.get("data", [])
print(f"Number of pools: {len(pools)}")
print()

if pools:
    p0 = pools[0]
    print("=== POOL 0 KEYS ===")
    print(list(p0.keys()))
    print()
    
    print("=== POOL 0 ID ===")
    print(p0.get("id"))
    print()
    
    print("=== POOL 0 ATTRIBUTES (raw) ===")
    attrs = p0.get("attributes", {})
    print(json.dumps(attrs, indent=2)[:3000])
    print()
    
    print("=== POOL 0 RELATIONSHIPS ===")
    rels = p0.get("relationships", {})
    print(list(rels.keys()))
    print()
    
    # Check tokens
    for tk_role in ["token0", "token1"]:
        tk_rel = rels.get(tk_role, {})
        print(f"=== {tk_role.upper()} relationship ===")
        print(json.dumps(tk_rel, indent=2)[:1000])
        print()
    
    # Metrics
    metrics = data.get("included", [])
    print(f"=== INCLUDED ITEMS: {len(metrics)} ===")
    for m in metrics[:5]:
        print(f"  type={m.get('type')}, id={m.get('id')}")
        if 'attributes' in m:
            print(f"    attrs keys: {list(m['attributes'].keys())}")
            if 'liquidity_usd' in m.get('attributes', {}):
                print(f"    liquidity_usd: {m['attributes']['liquidity_usd']}")
            if 'market_cap_usd' in m.get('attributes', {}):
                print(f"    market_cap_usd: {m['attributes']['market_cap_usd']}")
        print()
