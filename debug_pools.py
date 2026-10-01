#!/usr/bin/env python3
"""Quick debug: show what's actually in the trending pools."""

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

pools = gecko_get("/trending_pools", {"page": 1}).get("data", [])
print(f"Total trending: {len(pools)}")
print("="*80)

for p in pools:
    attrs = p.get("attributes", {})
    pid = p.get("id", "?")
    t0 = attrs.get("token0", {})
    t1 = attrs.get("token1", {})
    s0 = t0.get("symbol", "?")
    s1 = t1.get("symbol", "?")
    tvl = float(attrs.get("total_value_locked_usd") or 0)
    vol1h = float(attrs.get("volume_1h") or 0)
    pc24h = attrs.get("price_change_24h") or 0
    pool_toks = attrs.get("pool_tokens", [])
    tok_syms = [t.get("symbol","?") for t in pool_toks]
    
    has_eth = any(x.upper() in ("ETH","WETH") for x in [s0, s1] + tok_syms)
    
    print(f"Pool: {pid[:20]}...")
    print(f"  Pair: {s0}/{s1}")
    print(f"  Pool tokens: {tok_syms}")
    print(f"  TVL: ${tvl:,.0f} | Vol1h: ${vol1h:,.0f} | 24h%: {pc24h:+.2f}%")
    print(f"  ETH/WETH pair: {has_eth}")
    print()
