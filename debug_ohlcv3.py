#!/usr/bin/env python3
"""Debug OHLCV - try different URL formats."""
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
            body = e.read().decode()
            print(f"  HTTP {e.code}: {body[:150]}")
            if e.code == 429:
                time.sleep(5)
                continue
            return {"error": e.code, "body": body}
        except Exception as e:
            return {"error": str(e)}
    return {"error": "exhausted"}

addr = "0x6cdcb1c4a4d1c3c6d054b27ac5b77e89eafb971d"

urls_to_try = [
    f"{BASE}/networks/base/pools/{addr}/ohlcv?aggregate=1h&limit=120",
    f"{BASE}/networks/base/pools/{addr}/ohlcv?limit=120",
    f"{BASE}/networks/base/pools/{addr}/ohlcv/hour?limit=120",
    f"{BASE}/networks/base/pools/{addr}/ohlcv/hour?aggregate=1h&limit=120",
]

for url in urls_to_try:
    print(f"\n=== {url}\n")
    d = get(url)
    if isinstance(d, dict):
        print(f"Keys: {list(d.keys())}")
        if "data" in d:
            data = d["data"]
            if isinstance(data, list):
                print(f"List: {len(data)} items")
                if data:
                    print(f"First: {json.dumps(data[0], indent=2, default=str)[:400]}")
            else:
                print(f"Dict: {json.dumps(data, indent=2, default=str)[:400]}")
        elif "error" in d:
            print(f"Error: {d['error']}")
        else:
            print(f"Content: {json.dumps(d, indent=2, default=str)[:400]}")
    time.sleep(2)
