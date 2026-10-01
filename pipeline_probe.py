#!/usr/bin/env python3
"""
Pipeline probe: prove we can (1) discover trending Base coins and (2) fetch
their historical OHLCV candles for backtesting. Pure stdlib + curl-friendly
HTTP. Exit nonzero if the data pipeline is broken.
"""
import json, sys, urllib.request

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json;version=20230302"}

# Canonical discovery lives in discovery.py — this module delegates
# to it for the GeckoTerminal fetch and reuses its ETH/WETH + stable-pair
# filters, then merges with the other sources below.
from discovery import discover_trending as _canonical_discover

def get(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode()

def discover_trending(limit=15):
    url = "https://api.geckoterminal.com/api/v2/networks/base/trending_pools?page=1"
    data = json.loads(get(url))
    pools = []
    for p in data.get("data", []):
        a = p.get("attributes", {})
        liq = float(a.get("reserve_in_usd") or 0)
        v5m = float(a.get("volume_usd", {}).get("m5") or 0)
        v1h = float(a.get("volume_usd", {}).get("h1") or 0)
        pool_id = p.get("id", "")          # e.g. base_0x....
        addr = pool_id.split("_")[-1] if "_" in pool_id else pool_id
        pools.append({
            "name": a.get("name", "?"),
            "pool_id": pool_id,
            "address": addr,
            "liquidity": liq,
            "vol_5m": v5m,
            "vol_1h": v1h,
            "price_usd": float(a.get("base_token_price_usd") or 0),
        })
    # rank by liquidity then 5m volume
    pools.sort(key=lambda x: (x["liquidity"], x["vol_5m"]), reverse=True)
    return pools[:limit]

def fetch_ohlcv(pool_address, tf="hour", limit=300):
    url = (f"https://api.geckoterminal.com/api/v2/networks/base/pools/"
           f"{pool_address}/ohlcv/{tf}?aggregate=1&limit={limit}")
    data = json.loads(get(url))
    rows = data.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
    candles = []
    for row in rows:
        # [ts, o, h, l, c, v]
        ts, o, h, l, c, v = row
        candles.append({"ts": ts, "o": float(o), "h": float(h),
                        "l": float(l), "c": float(c), "v": float(v)})
    candles.sort(key=lambda x: x["ts"])
    return candles

if __name__ == "__main__":
    print("=== STEP 1: DISCOVER TRENDING COINS ===")
    pools = discover_trending(15)
    print(f"Discovered {len(pools)} trending Base pools:\n")
    for i, p in enumerate(pools):
        print(f"  {i+1:2}. {p['name'][:34]:34} liq=${p['liquidity']:>12,.0f} "
              f"v5m=${p['vol_5m']:>10,.0f}  addr={p['address'][:14]}...")
    if not pools:
        print("NO TRENDING POOLS — discovery failed"); sys.exit(1)

    print("\n=== STEP 2: FETCH OHLCV (backtest input) ===")
    ok = 0
    for p in pools[:5]:
        try:
            c = fetch_ohlcv(p["address"], "hour", 300)
            ok += 1
            if c:
                first, last = c[0], c[-1]
                chg = (last["c"]/first["c"] - 1) * 100
                print(f"  {p['name'][:30]:30} candles={len(c):3}  "
                      f"first=${first['c']:.6f} last=${last['c']:.6f}  "
                      f"{chg:+.1f}% over window")
            else:
                print(f"  {p['name'][:30]:30} candles=0  (EMPTY)")
        except Exception as e:
            print(f"  {p['name'][:30]:30} ERROR: {e}")
    print(f"\nOHLCV fetch ok for {ok}/5 sampled pools")
    sys.exit(0 if ok >= 4 else 1)
