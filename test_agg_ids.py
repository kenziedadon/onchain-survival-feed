import json, urllib.request, time

pool_addr = "0x5116773e18a9c7bb03ebb961b38678e45e238923"  # DRB/WETH

# Different aggregate IDs — not minutes, but aggregator IDs
# Try common IDs: 1, 2, 3, 4, 5, 10, 1440
tests = [
    ("agg=1", 1, 120),
    ("agg=2", 2, 120),
    ("agg=3", 3, 120),
    ("agg=4", 4, 120),
    ("agg=5", 5, 120),
    ("agg=10", 10, 120),
]

for label, agg, lim in tests:
    time.sleep(8)
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
                  f"{first_ts[:10]} -> {last_ts[:10]} | "
                  f"${first_p:.8f} -> ${last_p:.8f}")
        else:
            print(f"{label}: empty")
    except urllib.error.HTTPError as e:
        print(f"{label}: HTTP {e.code}")
    except Exception as e:
        print(f"{label}: {e}")
