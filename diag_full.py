#!/usr/bin/env python3
"""Fast diagnostic: dump full first pool JSON + check if symbols are inline."""

import json, urllib.request, time

GECKO = "https://api.geckoterminal.com/api/v2/networks/base"

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "HermesBot/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())

t = fetch(f"{GECKO}/trending_pools?page=1")
print("FIRST POOL — ALL KEYS:")
print(json.dumps(t["data"][0], indent=2))
print("\n\nSECOND POOL (just attributes):")
print(json.dumps(t["data"][1]["attributes"], indent=2))
print("\n\nSECOND POOL (relationships):")
print(json.dumps(t["data"][1]["relationships"], indent=2))
