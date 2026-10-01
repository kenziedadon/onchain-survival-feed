#!/usr/bin/env python3
"""Debug OHLCV endpoint."""
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
            print(f"  HTTP {e.code}")
            if e.code == 429:
                time.sleep(5)
                continue
            return {"error": e.code}
        except Exception as e:
            return {"error": str(e)}
    return {"error": "exhausted"}

addr = "0x6cdcb1c4a4d1c3c6d054b27ac5b77e89eafb971d"
url = f"{BASE}/networks/base/pools/{addr}/ohlcv/hour?aggregate=1&limit=120"
print(f"URL: {url}\n")

for attempt in range(5):
    print(f"Attempt {attempt+1}...")
    d = get(url)
    if "error" not in d and "data" in d:
        print(f"  SUCCESS: {len(d['data'])} candles")
        for c in d["data"][:3]:
            print(f"  {json.dumps(c, indent=2)[:300]}")
        if len(d["data"]) > 3:
            print(f"  ...")
            print(f"  Last: {json.dumps(d['data'][-1], indent=2)[:300]}")
        break
    print(f"  Response: {json.dumps(d, indent=2)[:300]}")
    time.sleep(3)
