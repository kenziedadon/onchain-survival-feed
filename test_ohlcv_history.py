import json, urllib.request, time

pool_addr = "0x5116773e18a9c7bb03ebb961b38678e45e238923"  # DRB/WETH

# Try longer limits and different aggregates
tests = [
    ("limit=200,agg=1h", {"aggregate": 1, "limit": 200}),
    ("limit=500,agg=1h", {"aggregate": 1, "limit": 500}),
    ("limit=120,agg=4h", {"aggregate": 4, "limit": 120}),
    ("limit=200,agg=4h", {"aggregate": 4, "limit": 200}),
    ("limit=120,agg=1d", {"aggregate": 1440, "limit": 120}),
]

url_base = f"https://api.geckoterminal.com/api/v2/networks/base/pools/{pool_addr}/ohlcv/hour"

for label, params in tests:
    time.sleep(3)
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{url_base}?{qs}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        candles = data.get("data", [])
        if candles:
            first = candles[0]["attributes"]["price_usd"]
            last = candles[-1]["attributes"]["price_usd"]
            print(f"{label}: {len(candles)} candles | first=${first:.6f} last=${last:.6f}")
        else:
            print(f"{label}: empty")
    except Exception as e:
        print(f"{label}: {e}")
