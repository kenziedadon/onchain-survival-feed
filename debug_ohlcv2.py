#!/usr/bin/env python3
"""Debug OHLCV - inspect full response."""
import json, time, urllib.request, urllib.error

BASE = "https://api.geckoterminal.com/api/v2"

def get(url, retries=5):
    headers = {"Accept": "application/json", "User-Agent": "Debug/1.0"}
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(5)
                continue
            return {"error": e.code}
        except Exception as e:
            return {"error": str(e)}
    return {"error": "exhausted"}

addr = "0x6cdcb1c4a4d1c3c6d054b27ac5b77e89eafb971d"

for url in [
    f"{BASE}/networks/base/pools/{addr}/ohlcv/hour?aggregate=1&limit=120",
    f"{BASE}/networks/base/pools/{addr}/ohlcv?aggregate=1&limit=120",
]:
    print(f"\n=== {url}\n")
    d = get(url)
    print(f"Type: {type(d)}")
    if isinstance(d, dict):
        print(f"Keys: {list(d.keys())}")
        if "data" in d:
            print(f"data type: {type(d['data'])}")
            if isinstance(d["data"], list):
                print(f"data len: {len(d['data'])}")
                if d["data"]:
                    print(f"First item type: {type(d['data'][0])}")
                    print(f"First item: {json.dumps(d['data'][0], indent=2, default=str)[:500]}")
            else:
                print(f"data: {json.dumps(d['data'], indent=2, default=str)[:500]}")
        if "attributes" in d:
            print(f"attributes: {json.dumps(d['attributes'], indent=2, default=str)[:500]}")
    print(f"Full: {json.dumps(d, indent=2, default=str)[:800]}")
    time.sleep(2)
