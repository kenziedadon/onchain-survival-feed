#!/usr/bin/env python3
"""Debug: dump trending pools raw data."""
import json, time, urllib.request, urllib.error

BASE = "https://api.geckoterminal.com/api/v2"

def get(url, retries=3):
    headers = {"Accept": "application/json", "User-Agent": "Debug/1.0"}
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            print(f"HTTP {e.code}: {e.read().decode()[:200]}")
            if e.code == 429:
                time.sleep(5)
                continue
            return {"error": e.code}
        except Exception as e:
            print(f"Error: {e}")
            return {"error": str(e)}
    return {"error": "exhausted"}

print("Fetching trending pools...")
d = get(f"{BASE}/networks/base/trending_pools?page=1")
print(f"Keys: {list(d.keys())}")

if "data" in d:
    for i, entry in enumerate(d["data"][:20]):
        attrs = entry.get("attributes", {})
        pt = attrs.get("pool_tokens", {})
        print(f"\n[{i}] {entry['id']}")
        print(f"  pool_tokens.usd = {pt.get('usd')}")
        print(f"  pool_tokens.usd_volume_24h = {pt.get('usd_volume_24h')}")
        print(f"  base_token {attrs.get('base_token',{})}")
        print(f"  quote_token {attrs.get('quote_token',{})}")
        print(f"  pool_token_count = {attrs.get('pool_token_count')}")
        tn = attrs.get("token_names", {})
        print(f"  token_names = {tn}")
else:
    print(json.dumps(d, indent=2)[:1000])
