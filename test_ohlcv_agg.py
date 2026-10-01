import json, urllib.request, time

# GeckoTerminal v2 OHLCV — try the endpoint format that returned 3 candles,
# but request a longer history. The "/hour" path seems to be required.

pool_addr = "0x5116773e18a9c7bb03ebb961b38678e45e238923"  # DRB/WETH

# v2 format: /networks/base/pools/{address}/ohlcv/hour?aggregate={m}&limit={n}
# aggregate in minutes: 60=hour, 240=4h, 1440=1d
tests = [
    ("1h x 200", 60, 200),
    ("1h x 500", 60, 500),
    ("4h x 200", 240, 200),
    ("4h x 500", 240, 500),
    ("1d x 365", 1440, 365),
    ("1d x 730", 1440, 730),
]

for label, agg, lim in tests:
    time.sleep(5)  # generous gap to avoid 429
    url = (f"https://api.geckoterminal.com/api/v2/networks/base/"
           f"pools/{pool_addr}/ohlcv/hour?aggregate={agg}&limit={lim}")
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json"
        })
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        candles = data.get("data", [])
        if candles:
            first_ts = candles[0]["attributes"]["timestamp"]
            last_ts = candles[-1]["attributes"]["timestamp"]
            first_p = candles[0]["attributes"]["price_usd"]
            last_p = candles[-1]["attributes"]["price_usd"]
            print(f"{label}: {len(candles)} candles | "
                  f"{first_ts[:10]} to {last_ts[:10]} | "
                  f"${first_p:.6f} -> ${last_p:.6f}")
        else:
            print(f"{label}: empty response")
    except Exception as e:
        print(f"{label}: {e}")
