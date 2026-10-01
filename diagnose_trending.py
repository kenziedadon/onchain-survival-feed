#!/usr/bin/env python3
"""Diagnose GeckoTerminal trending pools - what symbols are present?"""

import json, urllib.request, time

GECKO = "https://api.geckoterminal.com/api/v2/networks/base"

def fetch(url, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "HermesBot/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                wait = 5 * (2 ** attempt)
                print(f"  429, retry {attempt+1}/{retries} in {wait}s...")
                time.sleep(wait)
                continue
            raise

print("=== Trending page 1 ===")
t = fetch(f"{GECKO}/trending_pools?page=1")
print(f"Total: {len(t['data'])}")
for p in t["data"]:
    a = p["attributes"]
    bt = a.get("base_token", {})
    quote = a.get("quote_token", {})
    liq = a.get("pool_listings", {}).get("floor_liquidity_value", "?")
    print(f"  {bt.get('symbol','?'):8s} / {quote.get('symbol','?'):8s}  liq={liq}  base_addr={bt.get('address','')[:10]}...")

print()
print("=== Trending page 2 ===")
t2 = fetch(f"{GECKO}/trending_pools?page=2")
print(f"Total: {len(t2['data'])}")
for p in t2["data"]:
    a = p["attributes"]
    bt = a.get("base_token", {})
    quote = a.get("quote_token", {})
    liq = a.get("pool_listings", {}).get("floor_liquidity_value", "?")
    print(f"  {bt.get('symbol','?'):8s} / {quote.get('symbol','?'):8s}  liq={liq}  base_addr={bt.get('address','')[:10]}...")

print()
print("=== ETH/WETH count ===")
for label, data in [("page1", t["data"]), ("page2", t2["data"])]:
    for p in data:
        bt = p["attributes"].get("base_token", {})
        quote = p["attributes"].get("quote_token", {})
        if bt.get("symbol", "").upper() in ("ETH", "WETH") or quote.get("symbol", "").upper() in ("ETH", "WETH"):
            print(f"  ETH pair on {label}: {bt.get('symbol')}/{quote.get('symbol')} liq={p['attributes'].get('pool_listings', {}).get('floor_liquidity_value', '?')}")
