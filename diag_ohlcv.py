#!/usr/bin/env python3
"""Quick diagnostic: test ONE pool OHLCV fetch + EMA calc with verbose output."""

import urllib.request, json, time

def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={'User-Agent': 'BaseTrendBot/1.0'})
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                delay = 5 * (2 ** attempt) + 2
                print(f"  429, retry {attempt+1}/6 in {delay:.0f}s...")
                time.sleep(delay)
            else:
                raise
    raise Exception("Gave up after 6 retries")

def ema(series, span):
    k = 2 / (span + 1)
    out = [series[0]]
    for v in series[1:]:
        out.append(out[-1] * (1 - k) + v * k)
    return out

# Test with BRETT/WETH 1% — pool 0x4e829f8a5213c42535ab84aa40bd4adcce9cba02
pool_addr = "0x4e829f8a5213c42535ab84aa40bd4adcce9cba02"
pool_name = "BRETT / WETH 1%"

print(f"Testing OHLCV for {pool_name} ({pool_addr})...")
url = f"https://api.geckoterminal.com/api/v2/networks/base/pools/{pool_addr}/ohlcv/hour?aggregate=1&limit=120"
ohlcv_data = fetch(url)
ohlcv = ohlcv_data.get("data", [])
print(f"Got {len(ohlcv)} candles")

if len(ohlcv) >= 21:
    closes = [float(c["attributes"]["close_price"]) for c in ohlcv]
    print(f"\nLast 5 closes: {[f'{x:.6f}' for x in closes[-5:]]}")
    print(f"First close: {closes[0]:.6f}")
    
    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    
    print(f"\nEMA9 (last 5): {[f'{x:.6f}' for x in ema9[-5:]]}")
    print(f"EMA21 (last 5): {[f'{x:.6f}' for x in ema21[-5:]]}")
    
    # Check all windows for crossovers
    print("\nLast 10 candle crossover checks:")
    for i in range(len(closes)-10, len(closes)):
        if i < 2:
            continue
        pf, ps = ema9[i-1], ema21[i-1]
        cf, cs = ema9[i], ema21[i]
        cross = pf <= ps and cf > cs
        marker = "CROSSOVER" if cross else ""
        print(f"  candle {i}: fast={cf:.6f} slow={cs:.6f} prev_fast={pf:.6f} prev_slow={ps:.6f} {marker}")
    
    # Current state
    prev_cross = ema9[-2] <= ema21[-2]
    curr_cross = ema9[-1] > ema21[-1]
    print(f"\nPrev (candle -2): fast={ema9[-2]:.6f} <= slow={ema21[-2]:.6f} ? {prev_cross}")
    print(f"Curr (candle -1): fast={ema9[-1]:.6f} > slow={ema21[-1]:.6f} ? {curr_cross}")
    print(f"BUY crossover: {prev_cross and curr_cross}")
else:
    print(f"Only {len(ohlcv)} candles — need 21+")
