#!/usr/bin/env python3
"""Inspect raw trending pools JSON structure."""

import json
import urllib.request

def fetch_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "base-trend-bot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())

gecko_base = "https://api.geckoterminal.com/api/v2/networks/base"
trending_url = f"{gecko_base}/trending_pools?page=1"

trending = fetch_json(trending_url)
pools = trending.get("data", [])

print("=== FULL RAW JSON (first pool) ===")
print(json.dumps(pools[0], indent=2))
