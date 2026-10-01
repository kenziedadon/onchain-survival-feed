#!/usr/bin/env python3
"""Debug v2: fetch pool details for trending pools."""
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
            print(f"  HTTP {e.code}")
            if e.code == 429:
                time.sleep(5)
                continue
            return {"error": e.code}
        except Exception as e:
            print(f"  Error: {e}")
            return {"error": str(e)}
    return {"error": "exhausted"}

# First get trending list
print("1. Fetching trending pool IDs...")
trending = get(f"{BASE}/networks/base/trending_pools?page=1")
pool_ids = [e["id"] for e in trending.get("data", [])]
print(f"  Got {len(pool_ids)} pool IDs")

# Fetch a couple pool details
for pid in pool_ids[:3]:
    print(f"\n2. Fetching pool details: {pid}")
    pd = get(f"{BASE}/networks/base/pools/{pid}")
    if "error" in pd:
        print(f"  Error: {pd['error']}")
        continue
    attrs = pd.get("attributes", {})
    print(f"  Attributes keys: {list(attrs.keys())}")
    print(f"  pool_tokens: {json.dumps(attrs.get('pool_tokens',{}), indent=2)[:300]}")
    print(f"  base_token: {json.dumps(attrs.get('base_token',{}), indent=2)[:200]}")
    print(f"  quote_token: {json.dumps(attrs.get('quote_token',{}), indent=2)[:200]}")
    print(f"  token_names: {attrs.get('token_names',{})}")
    print(f"  tick/death: {attrs.get('tick', attrs.get('created_at', 'N/A'))}")
