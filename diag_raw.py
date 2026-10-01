#!/usr/bin/env python3
"""Inspect raw OHLCV response structure."""

import urllib.request, json, time

pool_addr = "0x4e829f8a5213c42535ab84aa40bd4adcce9cba02"

url = f"https://api.geckoterminal.com/api/v2/networks/base/pools/{pool_addr}/ohlcv/hour?aggregate=1&limit=120"
req = urllib.request.Request(url, headers={'User-Agent': 'BaseTrendBot/1.0'})
for attempt in range(4):
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        break
    except Exception as e:
        print(f"Attempt {attempt+1}: {e}")
        time.sleep(5)

print("Top-level keys:", list(data.keys()))
print("Data type:", type(data.get("data")))
if data.get("data"):
    print("First item keys:", list(data["data"][0].keys()))
    print("First item attributes:", json.dumps(data["data"][0].get("attributes", {}), indent=2))
    print(f"\nTotal items in data array: {len(data['data'])}")
    for i, item in enumerate(data["data"][:5]):
        attrs = item.get("attributes", {})
        print(f"  [{i}] ts={attrs.get('started_at')} close={attrs.get('close_price')} high={attrs.get('high_price')} low={attrs.get('low_price')} vol={attrs.get('volume')}")
