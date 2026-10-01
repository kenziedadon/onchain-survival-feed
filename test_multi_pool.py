import json, urllib.request, time

# Try different pools to see if 429 is pool-specific or general
pools_to_try = [
    ("0x6cdcb169d5c6d5c6b4a12e3c0b0f0a0b0c0d0e0f", "AERO/USDC"),  # fake addr just to test
    ("0xb2cc22b3314d2950c9c0742972436e8ed2c3bf20", "WETH/USDC"),
    ("0xf79478d5a6bae4546f7e489e80b2fc690b558944", "Basecat/WETH"),
]

# Wait before retrying
time.sleep(15)

for addr, label in pools_to_try:
    url = (f"https://api.geckoterminal.com/api/v2/networks/base/"
           f"pools/{addr}/ohlcv/hour?aggregate=4&limit=120")
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json"
        })
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        candles = data.get("data", [])
        print(f"{label} ({addr[:8]}): {len(candles)} candles")
        if candles:
            print(f"  First: {candles[0]['attributes']['price_usd']}, Last: {candles[-1]['attributes']['price_usd']}")
    except urllib.error.HTTPError as e:
        print(f"{label}: HTTP {e.code} — {e.reason}")
    except Exception as e:
        print(f"{label}: {e}")
    time.sleep(5)
