#!/usr/bin/env python3
"""Debug GeckoTerminal — check base_token/quote_token relationships."""
import json
import urllib.request

url = "https://api.geckoterminal.com/api/v2/networks/base/trending_pools?page=1"
req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
with urllib.request.urlopen(req, timeout=30) as resp:
    data = json.loads(resp.read())

pools = data.get("data", [])

print(f"=== {len(pools)} pools — summary ===")
for i, p in enumerate(pools[:5]):
    attrs = p.get("attributes", {})
    name = attrs.get("name", "N/A")
    reserve = attrs.get("reserve_in_usd", "0")
    rsv_float = float(reserve) if reserve else 0
    price_chg_h24 = attrs.get("price_change_percentage", {}).get("h24", "0")
    vol_h1 = attrs.get("volume_usd", {}).get("h1", "0")
    vol_h24 = attrs.get("volume_usd", {}).get("h24", "0")
    
    # Check relationships
    rels = p.get("relationships", {})
    base_rel = rels.get("base_token", {})
    quote_rel = rels.get("quote_token", {})
    
    print(f"\n[{i}] {name}")
    print(f"    reserve_in_usd: ${rsv_float:,.2f}")
    print(f"    price_change_h24: {price_chg_h24}%")
    print(f"    volume_h1: ${float(vol_h1):,.2f}")
    print(f"    volume_h24: ${float(vol_h24):,.2f}")
    print(f"    base_token rel: {json.dumps(base_rel)[:200]}")
    print(f"    quote_token rel: {json.dumps(quote_rel)[:200]}")
    print(f"    pool id: {p.get('id')}")

print("\n=== ALL POOLS with reserve > $100k and ETH/quote ===")
for p in pools:
    attrs = p.get("attributes", {})
    name = attrs.get("name", "")
    reserve_str = attrs.get("reserve_in_usd", "0")
    try:
        reserve = float(reserve_str)
    except:
        reserve = 0
    
    if reserve < 100000:
        continue
    
    # Check if ETH is the quote (name ends with /ETH or contains ETH)
    is_eth_pair = "/ETH" in name or "ETH/" in name
    print(f"  {name} — reserve: ${reserve:,.2f} — ETH pair: {is_eth_pair} — id: {p.get('id')}")
