#!/usr/bin/env python3
"""Inspect GeckoTerminal trending pools on Base — see what's actually there."""

import json, urllib.request, time

URL = "https://api.geckoterminal.com/api/v2/networks/base/trending_pools?page=1"

req = urllib.request.Request(URL, headers={"User-Agent": "Hermes/1.0"})
with urllib.request.urlopen(req, timeout=30) as resp:
    data = json.loads(resp.read().decode())

pools = data.get("data", [])
print(f"Total trending pools: {len(pools)}\n")

for i, pool in enumerate(pools):
    a = pool.get("attributes", {})
    b = a.get("base_token", {})
    q = a.get("quote_token", {})
    liq = a.get("pool_tokens", {}).get("usd") or a.get("liquidity") or a.get("total_value_locked") or 0
    vol = a.get("volume_usd", 0) or a.get("volume_24h", 0)
    print(f"--- Pool {i+1} ---")
    print(f"  Name: {a.get('name', 'N/A')}")
    print(f"  Address: {pool.get('id', 'N/A')}")
    print(f"  Base: {b.get('symbol','?')} ({b.get('address','')[:10]}...)")
    print(f"  Quote: {q.get('symbol','?')} ({q.get('address','')[:10]}...)")
    print(f"  Liquidity: ${liq:,.0f}")
    print(f"  Volume 24h: ${vol:,.0f}")
    print(f"  Price: ${a.get('price_USD', 'N/A')}")
    print()
    time.sleep(0.5)
