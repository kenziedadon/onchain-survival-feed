import json, urllib.request, time

pool_addr = "0x5116773e18a9c7bb03ebb961b38678e45e238923"  # DRB/WETH

print("Waiting 60s for rate limit reset...")
time.sleep(60)

# Exact params that worked before: aggregate=1, limit=120
url = (f"https://api.geckoterminal.com/api/v2/networks/base/"
       f"pools/{pool_addr}/ohlcv/hour?aggregate=1&limit=120")
req = urllib.request.Request(url, headers={
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json"
})
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    candles = data.get("data", [])
    print(f"Got {len(candles)} candles")
    if candles:
        for c in candles[-5:]:
            ts = c["attributes"]["timestamp"]
            p = c["attributes"]["price_usd"]
            print(f"  {ts[:10]} {ts[11:19]} | ${p:.8f}")
    else:
        print("Empty")
except urllib.error.HTTPError as e:
    print(f"HTTP {e.code}: {e.reason}")
    print(f"Headers: {dict(e.headers)}")
except Exception as e:
    print(f"Error: {e}")
