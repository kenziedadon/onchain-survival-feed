#!/usr/bin/env python3
import urllib.request, json

HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    "Origin": "https://www.geckoterminal.com",
    "Referer": "https://www.geckoterminal.com/",
}
GECKO = "https://api.geckoterminal.com/api/v2/networks/base"

req = urllib.request.Request(f"{GECKO}/trending_pools?page=1", headers=HEADERS)
with urllib.request.urlopen(req, timeout=30) as resp:
    data = json.loads(resp.read())

for i, p in enumerate(data["data"][:5]):
    attrs = p.get("attributes", {})
    reserve = attrs.get("reserve_in_usd")
    pool_id = p.get("id", "")
    print(f"\n--- Pool {i+1}: {pool_id} ---")
    print(f"  reserve_in_usd raw: {reserve!r} (type={type(reserve).__name__})")
    print(f"  keys in attrs: {list(attrs.keys())}")
    # Try all possible liquidity keys
    for k in ["reserve_in_usd", "total_liquidity_usd", "fdv_usd", "market_cap_usd"]:
        v = attrs.get(k)
        print(f"  {k}: {v!r}")
