import json, urllib.request, time

# Wait for rate limit to reset, then try DRB pool with different aggregate interpretations
pool_addr = "0x5116773e18a9c7bb03ebb961b38678e45e238923"  # DRB/WETH

# aggregate might be in minutes (not aggregator ID)
# Try: 1=1m bars? 60=1h bars? 240=4h? 1440=1d?
tests = [
    ("agg=1 limit=120", 1, 120),
    ("agg=5 limit=120", 5, 120),
    ("agg=15 limit=120", 15, 120),
    ("agg=60 limit=120", 60, 120),
    ("agg=60 limit=200", 60, 200),
]

for label, agg, lim in tests:
    time.sleep(10)
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
