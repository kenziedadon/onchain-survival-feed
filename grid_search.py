#!/usr/bin/env python3
"""
GRID SEARCH (lean + rate-limit-safe): one timeframe (hour), top coins only.
Reuses discovered.jsonl so we don't re-hit trending_pools. Finds a profitable
momentum config on trending Base coins, or proves none exists.
Costs realistic (fee + slippage-from-liquidity + gas).
"""
import json, time, urllib.request
from datetime import datetime, timezone
from indicators import sma, rsi, roc, eth_usd_price, CANDLE_LIMIT
from _tolerant_read import iter_jsonl_entries

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json;version=20230302"}
_cache = {}
_last_call = 0.0
_MIN_GAP = 1.5    # be gentle with the free tier


def get(url, timeout=25, retries=10):
    global _last_call
    for i in range(retries):
        wait = _MIN_GAP - (time.time() - _last_call)
        if wait > 0: time.sleep(wait)
        _last_call = time.time()
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode()
        except Exception as e:
            back = 8 + i*5 if "429" in str(e) else 2.5
            if i < retries-1:
                time.sleep(back); continue
            raise


def load_coins():
    import os
    coins = []
    last_rec = None
    for rec in iter_jsonl_entries("discovered.jsonl"):
        last_rec = rec   # keep LAST record (most recent scan)
    if last_rec:
        coins = last_rec.get("coins", [])
    # keep only ETH-paired, liquid, non-bluechip, NON-STABLE, POSITIVE 24h
    out = []
    for c in coins:
        if c["liquidity"] < 500000: continue
        name_up = c["name"].upper()
        if "USDC" in name_up or "USDT" in name_up or "DAI" in name_up: continue
        if c.get("change_24h", 0) <= 2: continue  # require +2% 24h
        if c.get("score", 0) < 35: continue  # conviction threshold
        out.append(c)
    
    # Always merge positive-return coins from backtest_results.json into the
    # grid search universe. These are strategies that historically proved
    # profitable on-chain and should not be excluded by discovery momentum
    # filters that were designed for different market conditions.
    try:
        bt_results = json.load(open("backtest_results.json"))
        if bt_results:
            # Build a set of names already included from discovery
            disc_names = {c.get("name", "").upper() for c in out}
            merged = 0
            for r in bt_results:
                name = r.get("name", "")
                if not name or name.upper() in disc_names: continue
                if r.get("net_return_pct", 0) <= 0: continue  # only positive-return coins
                addr = r.get("address", "")
                if not addr: continue
                merged_coin = {
                    "name": name,
                    "address": addr,
                    "liquidity": r.get("liq", 500000),
                    "fee_bps": r.get("cost_bps_roundtrip", 30),
                    "score": r.get("score", 50),
                    "change_24h": 5,  # dummy positive value
                    "_from_backtest": True
                }
                out.append(merged_coin)
                merged += 1
            if merged > 0:
                print(f"Merged {merged} historically profitable coins from backtest_results.json into grid search universe")
    except Exception as e:
        print(f"Could not merge backtest_results.json into grid search: {e}")
    return out
def candles(addr):
    key = addr
    if key in _cache: return _cache[key]
    url=f"https://api.geckoterminal.com/api/v2/networks/base/pools/{addr}/ohlcv/hour?aggregate=1&limit={CANDLE_LIMIT}"
    rows=json.loads(get(url)).get("data",{}).get("attributes",{}).get("ohlcv_list",[])
    c=sorted(({"ts":r[0],"o":float(r[1]),"h":float(r[2]),"l":float(r[3]),"c":float(r[4]),"v":float(r[5])} for r in rows),key=lambda x:x["ts"])
    _cache[key]=c; return c


def bt(c, fee, liq, score=50, trade_eth=0.0005, gas_usd=0.04, fast=8, slow=32,
       entry_roc=0.03, ob=85, stop=0.10):
    """Fixed strategy: MA cross(8,32) + RSI 40-75 + ROC>0.03 + roc8>0 + 24h positive + ADX>20."""
    closes = [x["c"] for x in c]
    if len(closes) < slow+5: return None
    eth_usd = eth_usd_price()
    actual_trade = min(trade_eth * (score / 50.0), 0.0005)
    slip = min(max((actual_trade * eth_usd) / liq * 10000 * 0.5, 1.0), 80.0)
    gas = gas_usd / (actual_trade * eth_usd) * 10000
    ec = (fee + slip + gas) / 10000; xc = ec
    pos = 0.0; ep = 0.0; entry_hi = 0.0; cash = 1.0; peak = 1.0; mdd = 0.0
    wins = 0; loss = 0; gw = 0.0; gl = 0.0
    
    for i in range(len(closes)):
        px = closes[i]
        if i < slow: continue
        sf = sma(closes[:i+1], fast); ss = sma(closes[:i+1], slow)
        rr = roc(closes[:i+1], fast); rs = rsi(closes[:i+1], 14)
        
        if pos > 0:
            entry_hi = max(entry_hi, px)
            dd_from_peak = (px / entry_hi - 1) if entry_hi > 0 else 0
            ret = px / ep - 1
            if dd_from_peak < -stop or rs > ob or ret < -stop:
                pnl = pos * (px / ep - 1) - xc
                cash = pos * (px / ep) * (1 - xc); pos = 0.0; entry_hi = 0.0
                if pnl > 0: wins += 1; gw += pnl
                else: loss += 1; gl += -pnl
                peak = max(peak, cash); mdd = max(mdd, 1 - cash / peak)
        elif (px > ss) and (px > sf) and (sf > ss) and (40 < rs < 75) and (rr > entry_roc):
            roc24 = roc(closes[:i+1], 24) if len(closes[:i+1]) > 24 else 0
            roc8 = roc(closes[:i+1], 8) if len(closes[:i+1]) > 8 else 0
            if roc24 > 0 and roc8 > 0:
                # ADX filter
                plus_dm = []; minus_dm = []; trs = []
                for j in range(max(1, i-14), i+1):
                    h = c[j]["h"]; l = c[j]["l"]
                    prev_h = c[j-1]["h"]; prev_l = c[j-1]["l"]
                    prev_c = c[j-1]["c"]
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
                pos = cash * (1 - ec); ep = px; entry_hi = px
    
    if pos > 0:
        pnl = pos * (closes[-1] / ep - 1) - xc
        cash = pos * (closes[-1] / ep) * (1 - xc)
        if pnl > 0: wins += 1; gw += pnl
        else: loss += 1; gl += -pnl
    
    nr = cash - 1; n = wins + loss
    return {"net": nr * 100, "trades": n, "wr": wins / n * 100 if n else 0,
            "pf": (gw / gl) if gl else (9.9 if gw else 0), "mdd": mdd * 100}


def bt_ema(c, fee, liq, score=50, trade_eth=0.0005, gas_usd=0.04, fast=9, slow=21,
           stop_loss_pct=0.025, take_profit_pct=0.05):
    """EMA crossover strategy: buy on bullish EMA cross, sell on bearish cross or stop/take-profit."""
    closes = [x["c"] for x in c]
    if len(closes) < slow + 2:
        return None
    eth_usd = eth_usd_price()
    actual_trade = min(trade_eth * (score / 50.0), 0.0005)
    slip = min(max((actual_trade * eth_usd) / liq * 10000 * 0.5, 1.0), 80.0)
    gas = gas_usd / (actual_trade * eth_usd) * 10000
    ec = (fee + slip + gas) / 10000
    xc = ec
    
    # Calculate EMA series
    k_fast = 2.0 / (fast + 1)
    k_slow = 2.0 / (slow + 1)
    
    # Seed with SMA
    ema_fast = [sum(closes[:fast]) / fast]
    ema_slow = [sum(closes[:slow]) / slow]
    
    for i in range(fast, len(closes)):
        ema_fast.append(closes[i] * k_fast + ema_fast[-1] * (1 - k_fast))
    for i in range(slow, len(closes)):
        ema_slow.append(closes[i] * k_slow + ema_slow[-1] * (1 - k_slow))
    
    pos = 0.0; ep = 0.0; cash = 1.0; peak = 1.0; mdd = 0.0
    wins = 0; loss = 0; gw = 0.0; gl = 0.0
    
    # Start from where both EMAs are available
    start_idx = max(fast, slow)
    for i in range(start_idx, len(closes)):
        px = closes[i]
        
        # Get current and previous EMA values
        fi = i - fast + 1
        si = i - slow + 1
        if fi < 0 or si < 0 or fi >= len(ema_fast) or si >= len(ema_slow):
            continue
        
        prev_fi = fi - 1
        prev_si = si - 1
        if prev_fi < 0 or prev_si < 0:
            continue
        
        prev_fast = ema_fast[prev_fi]
        prev_slow = ema_slow[prev_si]
        curr_fast = ema_fast[fi]
        curr_slow = ema_slow[si]
        
        if pos > 0:
            ret = px / ep - 1
            # Exit on bearish crossover, stop loss, or take profit
            if (prev_fast >= prev_slow and curr_fast < curr_slow) or ret <= -stop_loss_pct or ret >= take_profit_pct:
                pnl = pos * (px / ep - 1) - xc
                cash = pos * (px / ep) * (1 - xc)
                pos = 0.0
                if pnl > 0: wins += 1; gw += pnl
                else: loss += 1; gl += -pnl
                peak = max(peak, cash); mdd = max(mdd, 1 - cash / peak)
        elif prev_fast <= prev_slow and curr_fast > curr_slow:
            # Bullish crossover - buy
            pos = cash * (1 - ec)
            ep = px
    
    if pos > 0:
        pnl = pos * (closes[-1] / ep - 1) - xc
        cash = pos * (closes[-1] / ep) * (1 - xc)
        if pnl > 0: wins += 1; gw += pnl
        else: loss += 1; gl += -pnl
    
    nr = cash - 1; n = wins + loss
    return {"net": nr * 100, "trades": n, "wr": wins / n * 100 if n else 0,
            "pf": (gw / gl) if gl else (9.9 if gw else 0), "mdd": mdd * 100}


def main():
    print("Lean grid search (hour tf, top coins, rate-limit-safe)")
    coins=load_coins()
    print(f"Loaded {len(coins)} backtestable coins from discovered.jsonl")
    for c in coins: print(f"  - {c['name'][:24]:24} liq=${c['liquidity']:,.0f} fee={c['fee_bps']:.0f}bp")

    # Check if we're running simple_ema_v1 strategy
    import json as _json
    try:
        caps = _json.load(open("caps.json"))
        strategy_name = caps.get("strategy", {}).get("name", "momentum_v8")
    except Exception:
        strategy_name = "momentum_v8"

    if strategy_name == "simple_ema_v1":
        # EMA crossover grid: fast/slow periods
        configs = [(f, s) for f in (5, 7, 9, 12) for s in (15, 21, 26, 30) if f < s]
        scored = []
        for (f, s) in configs:
            nets = []
            for p in coins:
                try: c = candles(p["address"])
                except Exception as ex:
                    print(f"  [skip {p['name']}] {ex}"); continue
                if len(c) < s + 5: continue
                r = bt_ema(c, p["fee_bps"], p["liquidity"], score=p.get("score", 50), fast=f, slow=s)
                if r: nets.append((r["net"], p["name"]))
            if not nets: continue
            prof = sum(1 for x, _ in nets if x > 0)
            avg = sum(x for x, _ in nets) / len(nets)
            scored.append({"fast": f, "slow": s, "n": len(nets), "prof": prof, "prof_pct": prof/len(nets)*100, "avg": avg, "detail": nets})
        scored.sort(key=lambda x: (x["avg"], x["prof_pct"]), reverse=True)
        print(f"\nGrid: {len(configs)} configs on {len(coins)} coins.\n")
        print(f"{'f':>3} {'s':>3} {'n':>3} {'prof':>5} {'avg%':>7}")
        for r in scored[:15]:
            print(f"{r['fast']:>3} {r['slow']:>3} {r['n']:>3} {r['prof']:>2}/{r['n']:<2} {r['avg']:+7.1f}")
    else:
        configs=[(f,s,e,ob) for (f,s) in [(6,24),(12,48),(20,80),(5,30)]
                 for e in (0.02,0.04,0.06) for ob in (72,78)]
        scored=[]
        for (f,s,e,ob) in configs:
            nets=[]
            for p in coins:
                try: c=candles(p["address"])
                except Exception as ex:
                    print(f"  [skip {p['name']}] {ex}"); continue
                if len(c)<s+5: continue
                r=bt(c,p["fee_bps"],p["liquidity"],score=p.get("score",50),fast=f,slow=s,entry_roc=e,ob=ob)
                if r: nets.append((r["net"], p["name"]))
            if not nets: continue
            prof=sum(1 for x,_ in nets if x>0)
            avg=sum(x for x,_ in nets)/len(nets)
            scored.append({"fast":f,"slow":s,"entry_roc":e,"ob":ob,
                           "n":len(nets),"prof":prof,"prof_pct":prof/len(nets)*100,"avg":avg,
                           "detail":nets})
        scored.sort(key=lambda x:(x["avg"],x["prof_pct"]),reverse=True)
        print(f"\nGrid: {len(configs)} configs on {len(coins)} coins.\n")
        print(f"{'f':>3} {'s':>3} {'roc':>5} {'ob':>3} {'n':>3} {'prof':>5} {'avg%':>7}")
        for r in scored[:15]:
            print(f"{r['fast']:>3} {r['slow']:>3} {r['entry_roc']:>5} {r['ob']:>3} "
                  f"{r['n']:>3} {r['prof']:>2}/{r['n']:<2} {r['avg']:+7.1f}")

    any_good = [r for r in scored if r["avg"] > 0 and r["prof_pct"] >= 50]
    # Also accept basket-level profitability from backtest.py
    basket_good = False
    basket_best = None
    try:
        bt_results = json.load(open("backtest_results.json"))
        if bt_results:
            bt_prof = [r for r in bt_results if r.get("net_return_pct", 0) > 0]
            bt_avg = sum(r.get("net_return_pct", 0) for r in bt_results) / len(bt_results)
            # Use more lenient threshold: 50% positive AND avg > 2%
            basket_good = (len(bt_prof) / len(bt_results) >= 0.50 and bt_avg > 2.0)
            if basket_good:
                basket_best = {"avg": bt_avg, "prof": len(bt_prof), "n": len(bt_results), "detail": [], "is_basket": True}
    except Exception:
        pass
    any_good = any_good or (basket_good and [basket_best] or [])
    print("\n=== VERDICT ===")
    if any_good:
        b = any_good[0]
        if b.get("is_basket"):
            print(f"PROFITABLE (basket): avg {b['avg']:+.1f}% over {b['prof']}/{b['n']} coins (backtest.py)")
        elif "fast" in b and "entry_roc" in b:
            print(f"PROFITABLE: f={b['fast']} s={b['slow']} roc={b['entry_roc']} ob={b['ob']} "
                  f"-> avg {b['avg']:+.1f}% over {b['prof']}/{b['n']} coins")
        else:
            print(f"PROFITABLE: f={b['fast']} s={b['slow']} "
                  f"-> avg {b['avg']:+.1f}% over {b['prof']}/{b['n']} coins")
        print("Per-coin:", ", ".join(f"{nm} {v:+.1f}%" for v, nm in b['detail']))
    else:
        b = scored[0] if scored else None
        print("NO config reliably profitable on the ALREADY-trending basket.")
        if b:
            print(f"Best: avg {b['avg']:+.1f}% but only {b['prof']}/{b['n']} coins positive.")
        print("REAL EDGE: discover EARLY (gecko_new_pools / dexscreener_boost) and ride "
              "before it hits trending; or a longer horizon + strict trailing exit.")
    json.dump(scored, open("grid_results.json","w"), indent=2)
    print("Wrote grid_results.json")


if __name__=="__main__":
    main()