#!/usr/bin/env python3
"""Diagnose why some coins win and others lose."""
import json, sys, urllib.request
from datetime import datetime, timezone

sys.path.insert(0, "/home/kenzodadon/base-trend-bot")
from indicators import sma, rsi, roc, eth_usd_price, CANDLE_LIMIT

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json;version=20230302"}
_last = 0.0
_GAP = 1.5

def get(url, timeout=25, retries=5):
    global _last
    for i in range(retries):
        wait = _GAP - (time.time() - _last)
        if wait > 0: time.sleep(wait)
        _last = time.time()
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode()
        except Exception as e:
            back = 8 + i*5 if "429" in str(e) else 2.5
            if i < retries-1: time.sleep(back); continue
            raise

import time

# Load recent backtest results to know winners/losers
results = json.load(open("backtest_results.json"))
print("=== DIAGNOSIS: winners vs losers ===\n")
print(f"{'COIN':<28} {'RESULT':>8} {'TRADES':>6} {'WIN%':>5} {'DD%':>5} {'COSTbp':>7}")
print("-" * 70)
for r in results:
    print(f"{r['name'][:26]:<28} {r['net_return_pct']:+7.1f}% {r['trades']:>6} {r['win_rate']:>4.0f}% {r['max_drawdown_pct']:>4.0f}% {r['cost_bps_roundtrip']:>6.0f}")

print("\n=== Fetching detailed trade analysis ===\n")

# For each coin, fetch candles and analyze entry signals
for r in results:
    name = r['name']
    addr = None
    # Find address from pools
    for line in open("discovered.jsonl"):
        rec = json.loads(line)
        for c in rec.get("coins", []):
            if c["name"] == name:
                addr = c["address"]
                fee = c["fee_bps"]
                liq = c["liquidity"]
                break
        if addr:
            break
    if not addr:
        print(f"  {name}: address not found")
        continue
    
    try:
        url2 = f"https://api.geckoterminal.com/api/v2/networks/base/pools/{addr}/ohlcv/hour?aggregate=1&limit={CANDLE_LIMIT}"
        rows = json.loads(get(url2)).get("data",{}).get("attributes",{}).get("ohlcv_list",[])
        candles = sorted(({"ts":r[0],"o":float(r[1]),"h":float(r[2]),"l":float(r[3]),"c":float(r[4]),"v":float(r[5])} for r in rows), key=lambda x:x["ts"])
    except Exception as e:
        print(f"  {name}: {e}")
        continue
    
    if len(candles) < 40:
        print(f"  {name}: only {len(candles)} candles")
        continue
    
    closes = [c["c"] for c in candles]
    volumes = [c["v"] for c in candles]
    
    # Analyze volume patterns
    avg_vol = sum(volumes[32:]) / max(len(volumes[32:]), 1)
    last_vol = volumes[-1] if volumes else 0
    vol_ratio = last_vol / avg_vol if avg_vol > 0 else 0
    
    # Analyze price patterns
    roc_24 = (closes[-1] / closes[-25] - 1) * 100 if len(closes) > 24 else 0
    roc_8 = (closes[-1] / closes[-9] - 1) * 100 if len(closes) > 8 else 0
    
    # Analyze trend strength: count how many closes above SMA32
    sma32 = sum(closes[-32:]) / 32
    above_sma = sum(1 for c in closes[-32:] if c > sma32)
    trend_consistency = above_sma / 32 * 100
    
    # RSI
    rs = rsi(closes, 14)
    
    # ADX proxy: directional movement
    plus_dm = []
    minus_dm = []
    trs = []
    for i in range(1, len(candles)):
        h = candles[i]["h"]
        l = candles[i]["l"]
        prev_h = candles[i-1]["h"]
        prev_l = candles[i-1]["l"]
        prev_c = candles[i-1]["c"]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
        up = h - prev_h
        down = prev_l - l
        plus_dm.append(max(up, 0) if up > down else 0)
        minus_dm.append(max(down, 0) if down > up else 0)
    
    atr_14 = sum(trs[-14:]) / 14 if len(trs) >= 14 else 0
    plus_di = sum(plus_dm[-14:]) / sum(trs[-14:]) * 100 if sum(trs[-14:]) > 0 else 0
    minus_di = sum(minus_dm[-14:]) / sum(trs[-14:]) * 100 if sum(trs[-14:]) > 0 else 0
    dx = abs(plus_di - minus_di) / (plus_di + minus_di) * 100 if (plus_di + minus_di) > 0 else 0
    adx_proxy = dx
    
    flag = "✅" if r['net_return_pct'] > 0 else "❌"
    print(f"  {flag} {name[:26]:26}")
    print(f"      roc_8={roc_8:+.1f}% roc_24={roc_24:+.1f}% RSI={rs:.0f} ADX={adx_proxy:.0f}")
    print(f"      vol_ratio={vol_ratio:.2f} trend_consistency={trend_consistency:.0f}% above_SMA32")
    print(f"      +DI={plus_di:.0f} -DI={minus_di:.0f} ATR={atr_14:.6f}")
    print()

print("\n=== ANALYSIS: What separates winners from losers? ===")
print("""
HYPOTHESIS: Winners have strong trend consistency (>60% above SMA32),
high ADX (>25), and volume expansion. Losers have choppy/sideways price action.

If ADX > 25 filter eliminates losers like Basecat without killing KEYCAT,
add it as a required entry condition.
""")
