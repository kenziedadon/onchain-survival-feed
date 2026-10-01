#!/usr/bin/env python3
import json, urllib.request, time

def fetch(url, retries=3):
    for a in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent":"HermesBot/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except Exception as e:
            print(f"  retry {a}: {e}")
            if a < retries-1:
                time.sleep(3)
    return None

print("Waiting for rate limit to clear...")
time.sleep(5)

d = fetch("https://api.geckoterminal.com/api/v2/networks/base/trending_pools?page=1")
if not d:
    print("FAILED to fetch")
    exit(1)

print(f"Fetched {len(d.get('data',[]))} pools\n")
for i, p in enumerate(d.get("data", [])):
    a = p.get("attributes", {})
    bt = a.get("base_token", {})
    qt = a.get("quote_token", {})
    liq = a.get("total_value_locked_usd") or 0
    vol = a.get("volume_usd") or {}
    vol24 = vol.get("h24", 0) if isinstance(vol, dict) else 0
    print(f"[{i}] pool_id={p['id'][:50]}")
    print(f"    base={bt.get('symbol')} ({bt.get('address','')[:12]}...)")
    print(f"    quote={qt.get('symbol')} ({qt.get('address','')[:12]}...)")
    print(f"    liq=${liq:,.0f}  vol24h=${vol24:,.0f}  price=${a.get('price_usd','?')}")
    print()
