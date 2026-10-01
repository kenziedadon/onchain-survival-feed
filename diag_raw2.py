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

print("Top-level keys:", list(data.keys()))
inner = data.get("data", {})
print("Inner type:", type(inner))
if isinstance(inner, dict):
    print("Inner keys:", list(inner.keys())[:20])
    # check if it's keyed by pool address
    for k, v in inner.items():
        print(f"  key={k} type={type(v).__name__}")
        if isinstance(v, list):
            print(f"    list length={len(v)}")
            if v:
                print(f"    first item keys={list(v[0].keys())}")
                print(f"    first attrs={json.dumps(v[0].get('attributes',{}), indent=2)[:500]}")
        elif isinstance(v, dict):
            print(f"    dict keys={list(v.keys())[:10]}")
        break
elif isinstance(inner, list):
    print(f"List length={len(inner)}")
    if inner:
        print(f"First item keys={list(inner[0].keys())}")
        print(f"First attrs={json.dumps(inner[0].get('attributes',{}), indent=2)[:500]}")
