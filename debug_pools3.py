#!/usr/bin/env python3
"""Inspect GeckoTerminal pool structure in detail — raw dump."""
import urllib.request, json, time

url = "https://api.geckoterminal.com/api/v2/networks/base/trending_pools?page=1"
req = urllib.request.Request(url, headers={"User-Agent": "Hermes/1.0"})
with urllib.request.urlopen(req, timeout=30) as resp:
    data = json.loads(resp.read())

print("=== ATTRIBUTES KEYS (first pool) ===")
a0 = data["data"][0]["attributes"]
for k, v in a0.items():
    if isinstance(v, (int, float, str)):
        print(f"  {k}: {v}")
    else:
        print(f"  {k}: {type(v).__name__} = {json.dumps(v)[:200]}")

print("\n=== RELATIONSHIPS (first pool) ===")
rels = data["data"][0].get("relationships", {})
for k, v in rels.items():
    print(f"  {k}: {json.dumps(v)[:300]}")

print("\n=== ALL POOL NAMES + ADDRESSES ===")
for p in data["data"]:
    a = p["attributes"]
    print(f"  {a['address'][:12]}...  name={a.get('name','?')}  reserve_in_usd={a.get('reserve_in_usd',0)}  volume_usd={a.get('volume_usd',0)}")
