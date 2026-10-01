#!/usr/bin/env python3
"""Debug: inspect full OHLCV response structure."""

import json
import urllib.request
import urllib.parse
import time

GEOCKO_BASE = "https://api.geckoterminal.com/api/v2/networks/base"
GEKO_DELAY = 2.5
_last = 0

def gecko_get(path, params=None):
    global _last
    now = time.time()
    if now - _last < GEKO_DELAY:
        time.sleep(GEKO_DELAY - (now - _last))
    url = GEOCKO_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    _last = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": "Hermes/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())

# Test a known pool
pool_addr = "0x215b2146c3164aba3f5b18e8619b737c7f7dd11e5ba693646dcd9a336c5a30ba"
print(f"Pool: {pool_addr}")
print()

# Try OHLCV endpoint
ohlcv = gecko_get(f"/pools/{pool_addr}/ohlcv/hour", {"aggregate": 1, "limit": 120})
print("=== FULL OHLCV RESPONSE ===")
print(json.dumps(ohlcv, indent=2))
print()

# Check if there's a data field
if "data" in ohlcv:
    print(f"data type: {type(ohlcv['data'])}")
    if isinstance(ohlcv["data"], dict):
        print(f"data keys: {list(ohlcv['data'].keys())}")
        if "attributes" in ohlcv["data"]:
            print(f"attributes keys: {list(ohlcv['data']['attributes'].keys())}")
            attrs = ohlcv["data"]["attributes"]
            for k, v in attrs.items():
                if isinstance(v, list) and len(v) > 0:
                    print(f"  {k}: list of {len(v)} items, first: {v[0]}")
