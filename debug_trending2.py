#!/usr/bin/env python3
"""Debug v2: inspect raw GeckoTerminal response."""

import json
import urllib.request

def gecko_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Hermes-BaseTrendBot/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())

trending_url = "https://api.geckoterminal.com/api/v2/networks/base/trending_pools?page=1"
trending = gecko_json(trending_url)
pools = trending.get("data", [])

print(f"Total: {len(pools)}\n")
print("FIRST POOL RAW:")
pool0 = pools[0]
print(json.dumps(pool0, indent=2))
