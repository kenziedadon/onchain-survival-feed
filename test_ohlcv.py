#!/usr/bin/env python3
"""Quick test: can we fetch OHLCV for one pool after a longer delay?"""

import urllib.request
import json
import ssl
import time

GECKO = "https://api.geckoterminal.com/api/v2/networks/base"
ctx = ssl.create_default_context()
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

def get(path, params=None):
    url = GECKO + path
    if params:
        url += "?" + "&".join(f"{k}={v}" for k,v in params.items())
    req = urllib.request.Request(url, headers={"accept": "application/json", "user-agent": UA})
    with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
        return json.loads(r.read())

# First, trending pools
print("Fetching trending pools...")
tp = get("/trending_pools", {"page": 1})
pools = tp.get("data", [])
print(f"  {len(pools)} pools")

# Pick first WETH pool
target = None
for p in pools:
    a = p.get("attributes", {})
    name = a.get("name", "")
    if "/WETH" in name:
        target = p
        break

if not target:
    print("No WETH pool found")
    exit()

pid = target["id"]
name = target["attributes"]["name"]
print(f"\nTarget: {name} ({pid[:12]})")

# Wait 5s after trending call
print("Waiting 5s...")
time.sleep(5)

print(f"Fetching OHLCV for {pid}...")
try:
    od = get(f"/pools/{pid}/ohlcv/hour", {"aggregate": 1, "limit": 5})
    candles = od.get("data", [])
    print(f"  Got {len(candles)} candles")
    if candles:
        c0 = candles[0].get("attributes", {})
        print(f"  First: close_quote={c0.get('close_quote')} volume={c0.get('volume')}")
        print(f"  Full attrs keys: {list(c0.keys())}")
except Exception as e:
    print(f"  ERROR: {e}")
