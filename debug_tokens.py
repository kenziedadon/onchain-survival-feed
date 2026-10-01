#!/usr/bin/env python3
"""Check how to get token details from GeckoTerminal."""

import json
import urllib.request
import urllib.parse
import time

GEOCKO_BASE = "https://api.geckoterminal.com/api/v2/networks/base"
GEKO_DELAY = 1.5
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

# Get first pool's base_token details
pools = gecko_get("/trending_pools", {"page": 1}).get("data", [])
first = pools[0]

base_token_id = first["relationships"]["base_token"]["data"]["id"]
quote_token_id = first["relationships"]["quote_token"]["data"]["id"]

print(f"Pool: {first['id']}")
print(f"Base token: {base_token_id}")
print(f"Quote token: {quote_token_id}")
print()

# Try to fetch token details
print("=== Fetching base_token details ===")
try:
    token_data = gecko_get(f"/tokens/{base_token_id}")
    print(json.dumps(token_data, indent=2)[:2000])
except Exception as e:
    print(f"Error: {e}")

print("\n\n=== Fetching quote_token details ===")
try:
    token_data = gecko_get(f"/tokens/{quote_token_id}")
    print(json.dumps(token_data, indent=2)[:2000])
except Exception as e:
    print(f"Error: {e}")
