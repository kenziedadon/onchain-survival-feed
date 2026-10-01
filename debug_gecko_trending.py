#!/usr/bin/env python3
import urllib.request, json

def get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "Hermes-Agent/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())

data = get("https://api.geckoterminal.com/api/v2/networks/base/trending_pools?page=1")
pools = data.get("data", [])
print(f"Total pools: {len(pools)}")
print(f"Keys in first pool: {list(pools[0].keys()) if pools else 'none'}")
print(f"First pool raw:\n{json.dumps(pools[0], indent=2)}")
print(f"\nAttributes keys: {list(pools[0].get('attributes', {}).keys()) if pools else 'none'}")
attr = pools[0].get("attributes", {})
print(f"\npool_usd_liquidity: {attr.get('pool_usd_liquidity')}")
print(f"base_token: {json.dumps(attr.get('base_token', {}), indent=2)}")
print(f"quote_token: {json.dumps(attr.get('quote_token', {}), indent=2)}")
print(f"pool_token: {json.dumps(attr.get('pool_token', {}), indent=2)}")
