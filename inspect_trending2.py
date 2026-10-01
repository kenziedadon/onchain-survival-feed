#!/usr/bin/env python3
"""Inspect trending pools structure."""
import urllib.request, json, time

GECKO = "https://api.geckoterminal.com/api/v2/networks/base"

def fetch(url):
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "hermes/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(6 * (2 ** attempt))
            else:
                raise
    raise Exception("Failed")

data = fetch(f"{GECKO}/trending_pools?page=1")
pools = data.get("data", [])
print(f"Total: {len(pools)}")
for p in pools:
    a = p.get("attributes", {})
    bt = a.get("base_token", {})
    print(f"\n--- {p.get('address','')[:10]}... ---")
    print(f"  Base: {bt.get('symbol')} {bt.get('address','')[:10]}...")
    print(f"  Pool tokens count: {len(a.get('pool_tokens', []))}")
    for tk in a.get("pool_tokens", []):
        c = tk.get("currency", {})
        print(f"    Token: {c.get('symbol')} {tk.get('address','')[:10]}... liq={tk.get('liquidity',0)}")
    print(f"  liquidity field: {a.get('liquidity')}")
    print(f"  total_value_locked: {a.get('total_value_locked')}")
    print(f"  lister: {a.get('lister',{}).get('address','')[:10]}")
    print(f"  category: {a.get('category')}")
    time.sleep(1.5)
