#!/usr/bin/env python3
"""
STRATEGY FIX TEST v5 — fix coin selection, not entry timing.

Insight: v4's tighter entry filters DELAYED entry until after pumps, causing
buying the top. The fix: use ADX/volume/trend filters at DISCOVERY time to
avoid losers, not at entry time to delay.

v5 approach:
- DISCOVERY: only trade coins with ADX>20, vol_ratio>0.5, trend_consistency>50%
- ENTRY: simpler MA cross + RSI 40-75 (like v3)
- EXIT: trailing stop (like v3)
- SIZING: score-weighted (like v3)
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

def fetch_candles(addr):
    url = f"https://api.geckoterminal.com/api/v2/networks/base/pools/{addr}/ohlcv/hour?aggregate=1&limit={CANDLE_LIMIT}"
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
    atr = sum(trs[-n:]) / n
    if atr == 0: return 0, 0, 0
    pdi = sum(plus_dm[-n:]) / sum(trs[-n:]) * 100
    mdi = sum(minus_dm[-n:]) / sum(trs[-n:]) * 100
    dx = abs(pdi - mdi) / (pdi + mdi) * 100 if (pdi + mdi) > 0 else 0
    return pdi, mdi, dx

def selection_filter(candles):
    """Apply filters at discovery time. Returns (pass, reason)."""
    closes = [c["c"] for c in candles]
    volumes = [c["v"] for c in candles]
    if len(closes) < 40: return False, "too_few"
    
    # ADX > 20 (trending, not choppy)
    pdi, mdi, adx = calc_dmi(candles, 14)
    if adx < 20: return False, f"adx={adx:.0f}"
    
    # +DI > -DI + 10 (bullish direction)
    if pdi < mdi + 10: return False, f"pdi={pdi:.0f} mdi={mdi:.0f}"
    
    # Volume expansion: recent 24h vol > 50% of avg
    if len(volumes) >= 24:
        avg_vol = sum(volumes[-48:-24]) / 24 if len(volumes) >= 48 else sum(volumes[:24]) / 24
        recent_vol = sum(volumes[-24:]) / 24
        vol_ratio = recent_vol / avg_vol if avg_vol > 0 else 0
        if vol_ratio < 0.5: return False, f"vol_ratio={vol_ratio:.2f}"
    
    # Trend consistency: >50% above SMA32
    sma32 = sum(closes[-32:]) / 32
    above = sum(1 for c in closes[-32:] if c > sma32)
    consistency = above / 32
    if consistency < 0.5: return False, f"consistency={consistency:.0f}%"
    
    # 24h change positive
    if len(closes) >= 24:
        change_24h = (closes[-1] / closes[-25] - 1) * 100
        if change_24h <= 2: return False, f"change_24h={change_24h:.1f}%"
    
    return True, "pass"

def bt_v5(candles, fee_bps, liquidity, score=50, gas_usd=0.04):
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
            if dd_from_peak < -0.10 or rs > 85 or ret < -0.10:
                pnl = pos * (px / ep - 1) - xc
                cash = pos * (px / ep) * (1 - xc)
                pos = 0.0; entry_hi = 0.0
                if pnl > 0: wins += 1; gw += pnl
                else: loss += 1; gl += -pnl
                peak = max(peak, cash); mdd = max(mdd, 1 - cash / peak)
        elif (px > ss) and (px > sf) and (sf > ss) and (40 < rs < 75) and (rr > 0.03):
            # 24h trend positive (avoid falling knives)
            roc24 = roc(closes[:i+1], 24) if len(closes[:i+1]) > 24 else 0
            if roc24 > 0:
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
    print("=== STRATEGY FIX TEST v5: selection filters ===\n")
    
    # Load coins
    coins = []
    with open("discovered.jsonl") as f:
        for line in f:
            rec = json.loads(line)
            coins = rec.get("coins", [])
    
    # Base filters
    coins = [c for c in coins if c["liquidity"] >= 500000
             and "USDC" not in c["name"].upper()
             and "USDT" not in c["name"].upper()
             and "DAI" not in c["name"].upper()]
    coins.sort(key=lambda x: x["score"], reverse=True)
    coins = coins[:15]
    
    print(f"Testing {len(coins)} coins (pre-selection):\n")
    
    results = []
    blocked = []
    for c in coins:
        try:
            candles = fetch_candles(c["address"])
        except Exception as e:
            continue
        if len(candles) < 40:
            continue
        
        # Apply selection filter (DISCOVERY time)
        pass_filter, reason = selection_filter(candles)
        if not pass_filter:
            blocked.append((c['name'], reason))
            flag = "⛔"
            print(f"  {flag} {c['name'][:26]:26} BLOCKED: {reason}")
            continue
        
        r = bt_v5(candles, c["fee_bps"], c["liquidity"], c.get("score", 50))
        if not r: continue
        
        flag = "✅" if r["net_pct"] > 0 else "❌"
        print(f"  {flag} {c['name'][:26]:26} net={r['net_pct']:+7.1f}% trades={r['trades']} "
              f"win={r['win_rate']:.0f}% mdd={r['mdd_pct']:.0f}% pf={r['pf']:.2f}")
        results.append({"name": c["name"], **r})
    
    print(f"\n  Blocked {len(blocked)} coins at discovery:")
    for name, reason in blocked:
        print(f"    - {name[:26]:26} {reason}")
    
    if results:
        avg = sum(r["net_pct"] for r in results) / len(results)
        prof = [r for r in results if r["net_pct"] > 0]
        basket = 1.0
        for r in results:
            basket *= (0.5 + 0.5 * (1 + r["net_pct"]/100))
        
        print(f"\n=== BASKET SUMMARY ({len(results)} coins traded) ===")
        print(f"Profitable: {len(prof)}/{len(results)} ({len(prof)/len(results)*100:.0f}%)")
        print(f"Avg return: {avg:+.1f}%")
        print(f"Naive basket (50/50): {(basket-1)*100:+.1f}%")
        print(f"Avg max drawdown: {sum(r['mdd_pct'] for r in results)/len(results):.1f}%")
        
        json.dump(results, open("fix_test_v5_results.json", "w"), indent=2)
        print("\nWrote fix_test_v5_results.json")

if __name__ == "__main__":
    main()
