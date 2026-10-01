#!/usr/bin/env python3
"""Dump raw GeckoTerminal trending response to see actual structure."""

import json, urllib.request, time

GECKO = "https://api.geckoterminal.com/api/v2/networks/base"

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "HermesBot/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())

print("Fetching trending page 1...")
t = fetch(f"{GECKO}/trending_pools?page=1")

print(f"\nTop-level keys: {list(t.keys())}")
print(f"Data type: {type(t.get('data'))}")
print(f"Data count: {len(t.get('data', []))}")

if t.get("data"):
    first = t["data"][0]
    print(f"\nFirst pool keys: {list(first.keys())}")
    print(f"\nFirst pool ID: {first.get('id')}")
    print(f"\nFirst pool full JSON:")
    print(json.dumps(first, indent=2)[:3000])

print(f"\n\nMeta info: {json.dumps(t.get('meta', {}), indent=2)[:1000]}")
