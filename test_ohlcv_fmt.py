import json, urllib.request, time

# Test different OHLCV URL formats
pool_addr = "0x5116773e18a9c7bb03ebb961b38678e45e238923"  # DRB/WETH
pool_id = "base_0x5116773e18a9c7bb03ebb961b38678e45e238923"

formats = [
    f"https://api.geckoterminal.com/api/v2/networks/base/pools/{pool_addr}/ohlcv?aggregate=1&limit=120",
    f"https://api.geckoterminal.com/api/v2/networks/base/pools/{pool_id}/ohlcv?aggregate=1&limit=120",
    f"https://api.geckoterminal.com/api/v2/networks/base/pools/{pool_addr}/ohlcv/hour?aggregate=1&limit=120",
]

for i, url in enumerate(formats):
    time.sleep(3)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        candles = data.get("data", [])
        print(f"Format {i}: OK — {len(candles)} candles")
        if candles:
            print(f"  Last close: {candles[-1]['attributes']['price_usd']}")
    except Exception as e:
        print(f"Format {i}: {e}")
