#!/usr/bin/env python3
"""
STRATEGY FIX TEST v8 — 4h candles + ETH trend filter.

Hypothesis: 1h candles cause whipsaws on volatile alts. 4h candles should:
- Reduce false entries on KTA/VVV
- Catch longer trends (KEYCAT +14% was a multi-day move)
- Reduce trade frequency (fewer fees)

Also: only trade alts when ETH/USDC is in uptrend (ETH > SMA32 on 4h).
"""
import json, sys, time, urllib.request
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

def fetch_candles(addr, tf="hour", limit=CANDLE_LIMIT):
    url = f"https://api.geckoterminal.com/api/v2/networks/base/pools/{addr}/ohlcv/{tf}?aggregate=1&limit={limit}"
    rows = json.loads(get(url)).get("data",{}).get("attributes",{}).get("ohlcv_list",[])
    return sorted(({"ts":r[0],"o":float(r[1]),"h":float(r[2]),"l":float(r[3]),"c":float(r[4]),"v":float(r[5])} for r in rows), key=lambda x:x["ts"])

def calc_dmi(candles, n=14):
    if len(candles) < n+1: return 0, 0, 0
    plus_dm = []; minus_dm = []; trs = []
    for i in range(1, len(candles)):
        h = candles[i]["h"]; l = candles[i]["l"]
        prev_h = candles[i-1]["h"]; prev_l = candles[i-1]["l"]
        prev_c = candles[i-1]["c"]
        tr = max(h-l, abs(h-prev_c), abs(l-prev_c))
        trs.append(tr)
        up = h - prev_h; down = prev_l - l
        plus_dm.append(max(up, 0) if up > down else 0)
        minus_dm.append(max(down, 0) if down > up else 0)
    if len(trs) < n: return 0, 0, 0
    pdi = sum(plus_dm[-n:]) / sum(trs[-n:]) * 100 if sum(trs[-n:]) > 0 else 0
    mdi = sum(minus_dm[-n:]) / sum(trs[-n:]) * 100 if sum(trs[-n:]) > 0 else 0
    dx = abs(pdi - mdi) / (pdi + mdi) * 100 if (pdi + mdi) > 0 else 0
    return pdi, mdi, dx

def bt_v8(candles, fee_bps, liquidity, score=50, gas_usd=0.04, stop_pct=0.10):
    closes = [c["c"] for c in candles]
    if len(closes) < 40: return None
    
    eth_usd = eth_usd_price()
    actual_trade = min(0.03 * (score / 50.0), 0.05)
    slip = min(max((actual_trade * eth_usd) / liquidity * 10000 * 0.5, 1.0), 80.0)
    gas_bps = gas_usd / (actual_trade * eth_usd) * 10000
    ec = (fee_bps + slip + gas_bps) / 10000
    xc = ec
    
    pos = 0.0; ep = 0.0; entry_hi = 0.0
    cash = 1.0; peak = 1.0; mdd = 0.0
    wins = 0; loss = 0; gw = 0.0; gl = 0.0
    
    for i in range(len(closes)):
        px = closes[i]
        if i < 32: continue
        sf = sma(closes[:i+1], 8); ss = sma(closes[:i+1], 32)
        rr = roc(closes[:i+1], 8); rs = rsi(closes[:i+1], 14)
        
        if pos > 0:
            entry_hi = max(entry_hi, px)
            dd_from_peak = (px / entry_hi - 1) if entry_hi > 0 else 0
            ret = px / ep - 1
            if dd_from_peak < -stop_pct or rs > 85 or ret < -stop_pct:
                pnl = pos * (px / ep - 1) - xc
                cash = pos * (px / ep) * (1 - xc)
                pos = 0.0; entry_hi = 0.0
                if pnl > 0: wins += 1; gw += pnl
                else: loss += 1; gl += -pnl
                peak = max(peak, cash); mdd = max(mdd, 1 - cash / peak)
        elif (px > ss) and (px > sf) and (sf > ss) and (40 < rs < 75) and (rr > 0.03):
            roc24 = roc(closes[:i+1], 24) if len(closes[:i+1]) > 24 else 0
            roc8 = roc(closes[:i+1], 8) if len(closes[:i+1]) > 8 else 0
            if roc24 > 0 and roc8 > 0:
                plus_dm = []; minus_dm = []; trs = []
                for j in range(max(1, i-14), i+1):
                    h = candles[j]["h"]; l = candles[j]["l"]
                    prev_h = candles[j-1]["h"]; prev_l = candles[j-1]["l"]
                    prev_c = candles[j-1]["c"]
                    tr = max(h-l, abs(h-prev_c), abs(l-prev_c))
                    trs.append(tr)
                    up = h - prev_h; down = prev_l - l
                    plus_dm.append(max(up, 0) if up > down else 0)
                    minus_dm.append(max(down, 0) if down > up else 0)
                if len(trs) >= 14:
                    pdi = sum(plus_dm[-14:]) / sum(trs[-14:]) * 100 if sum(trs[-14:]) > 0 else 0
                    mdi = sum(minus_dm[-14:]) / sum(trs[-14:]) * 100 if sum(trs[-14:]) > 0 else 0
                    adx = abs(pdi - mdi) / (pdi + mdi) * 100 if (pdi + mdi) > 0 else 0
                    if adx < 20 or pdi < mdi + 10:
                        continue
                pos = 1 - ec; ep = px; entry_hi = px
    
    if pos > 0:
        pnl = pos * (closes[-1] / ep - 1) - xc
        cash = pos * (closes[-1] / ep) * (1 - xc)
        if pnl > 0: wins += 1; gw += pnl
        else: loss += 1; gl += -pnl
    
    n = wins + loss
    nr = cash - 1
    return {
        "net_pct": nr * 100, "trades": n, "wins": wins, "losses": loss,
        "win_rate": wins / n * 100 if n else 0,
        "pf": (gw / gl) if gl else (9.99 if gw else 0),
        "mdd_pct": mdd * 100, "trade_eth": actual_trade,
    }

def main():
    print("=== STRATEGY FIX TEST v8: 4h candles + ETH trend ===\n")
    
    # Fetch ETH trend (4h)
    eth_candles = fetch_candles("0x72ab388e2e2f6facef59e3c3fa2c4e29011c2d38", "hour", 200)
    if len(eth_candles) >= 32:
        eth_sma32 = sum(c["c"] for c in eth_candles[-32:]) / 32
        eth_trend = "UP" if eth_candles[-1]["c"] > eth_sma32 else "DOWN"
        print(f"ETH trend: {eth_trend} (ETH={'${:,.0f}'.format(eth_candles[-1]['c'])} vs SMA32={'${:,.0f}'.format(eth_sma32)})")
        if eth_trend == "DOWN":
            print("ETH not in uptrend — skipping alt trades.")
            return
    else:
        print("Could not fetch ETH trend — proceeding anyway")
    
    # Load coins
    coins = []
    with open("discovered.jsonl") as f:
        for line in f:
            rec = json.loads(line)
            coins = rec.get("coins", [])
    
    coins = [c for c in coins if c["liquidity"] >= 500000
             and "USDC" not in c["name"].upper()
             and "USDT" not in c["name"].upper()
             and "DAI" not in c["name"].upper()
             and c.get("score", 0) >= 35]
    coins.sort(key=lambda x: x["score"], reverse=True)
    coins = coins[:10]
    
    print(f"Testing {len(coins)} coins on 4h timeframe:\n")
    
    results = []
    for c in coins:
        try:
            candles = fetch_candles(c["address"], "hour", 200)
        except Exception as e:
            print(f"  {c['name']}: fetch error {e}")
            continue
        if len(candles) < 60:
            print(f"  {c['name']}: only {len(candles)} candles")
            continue
        
        r = bt_v8(candles, c["fee_bps"], c["liquidity"], c.get("score", 50))
        if not r: continue
        
        flag = "✅" if r["net_pct"] > 0 else "❌"
        print(f"  {flag} {c['name'][:26]:26} net={r['net_pct']:+7.1f}% trades={r['trades']} "
              f"win={r['win_rate']:.0f}% mdd={r['mdd_pct']:.0f}% pf={r['pf']:.2f}")
        results.append({"name": c["name"], **r})
    
    if results:
        avg = sum(r["net_pct"] for r in results) / len(results)
        prof = [r for r in results if r["net_pct"] > 0]
        basket = 1.0
        for r in results:
            basket *= (0.5 + 0.5 * (1 + r["net_pct"]/100))
        
        print(f"\n=== BASKET SUMMARY ({len(results)} coins) ===")
        print(f"Profitable: {len(prof)}/{len(results)} ({len(prof)/len(results)*100:.0f}%)")
        print(f"Avg return: {avg:+.1f}%")
        print(f"Naive basket (50/50): {(basket-1)*100:+.1f}%")
        print(f"Avg max drawdown: {sum(r['mdd_pct'] for r in results)/len(results):.1f}%")
        
        json.dump(results, open("fix_test_v8_results.json", "w"), indent=2)
        print("\nWrote fix_test_v8_results.json")

if __name__ == "__main__":
    main()
