#!/usr/bin/env python3
import urllib.request, json, time

url = "https://api.geckoterminal.com/api/v2/networks/base/pools/0x215b2146c3164aba3f5b18e8619b737c7f7dd11e5ba693646dcd9a336c5a30ba/ohlcv/hour?aggregate=1&limit=3"
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
time.sleep(1.5)
with urllib.request.urlopen(req, timeout=30) as resp:
    data = json.loads(resp.read())
print(json.dumps(data, indent=2))
