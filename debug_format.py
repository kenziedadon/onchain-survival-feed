#!/usr/bin/env python3
"""Debug v3: try correct pool address format."""
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
            body = e.read().decode()
            print(f"  HTTP {e.code}: {body[:150]}")
            if e.code == 429:
                time.sleep(5)
                continue
            return {"error": e.code, "body": body}
        except Exception as e:
            print(f"  Error: {e}")
            return {"error": str(e)}
    return {"error": "exhausted"}

# Try pool address without base_ prefix
test_addr = "0x6cdcb1c4a4d1c3c6d054b27ac5b77e89eafb971d"

print(f"1. Try /networks/base/pools/{test_addr}")
d1 = get(f"{BASE}/networks/base/pools/{test_addr}")
if "error" not in d1:
    print(f"  SUCCESS! Keys: {list(d1.keys())}")
    attrs = d1.get("attributes", {})
    print(f"  pool_tokens: {json.dumps(attrs.get('pool_tokens',{}), indent=2)[:400]}")
    print(f"  base_token: {json.dumps(attrs.get('base_token',{}), indent=2)[:300]}")
    print(f"  quote_token: {json.dumps(attrs.get('quote_token',{}), indent=2)[:300]}")
    print(f"  token_names: {attrs.get('token_names',{})}")
else:
    print(f"  Failed: {d1}")

print(f"\n2. Try /networks/base/pools/{test_addr}/ohlcv/hour?aggregate=1&limit=5")
d2 = get(f"{BASE}/networks/base/pools/{test_addr}/ohlcv/hour?aggregate=1&limit=5")
if "error" not in d2:
    print(f"  SUCCESS! Keys: {list(d2.keys())}")
    for c in d2.get("data", [])[:2]:
        print(f"  Candle: {json.dumps(c.get('attributes',{}), indent=2)[:400]}")
else:
    print(f"  Failed: {d2}")
