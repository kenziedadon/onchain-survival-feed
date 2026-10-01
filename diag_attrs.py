#!/usr/bin/env python3
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

print(json.dumps(data.get("data", {}).get("attributes", {}), indent=2))
