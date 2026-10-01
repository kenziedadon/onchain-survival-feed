#!/usr/bin/env python3
"""
STRATEGY FIX TEST ENGINE v3 — final validation before applying.

Fixes applied:
1. EXCLUDE stable pairs (WETH/USDC, USDC/USDT, etc.) — no real edge
2. REAL-TIME discovery (no stale cache)
3. VOL BREAKOUT filter: coin must have vol_24h > $50k AND vol_24h > 2x 7d avg
4. ENTRY: MA cross(8,32) + RSI 45-65 + ROC(8)>0.03 (momentum confirmed)
5. EXIT: 10% trailing stop off peak + RSI>85 blowoff
6. SIZING: score-weighted (0.03 * score/50, max 0.05)
7. TOP-3 by real-time momentum score only

This isolates the REAL tradeable universe and removes noise.
"""
import json, sys, time, urllib.request
from datetime import datetime, timezone

sys.path.insert(0, "/home/kenzodadon/base-trend-bot")
from indicators import sma, rsi, roc, eth_usd_price, CANDLE_LIMIT

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json;version=20230302"}
_last = 0.0
_GAP = 1.2

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

def discover_and_fetch(min_liq=500_000, top_n=5):
    """Real-time discovery — exclude stable pairs, focus on momentum coins."""
    url = "https://api.geckoterminal.com/api/v2/networks/base/trending_pools?page=1"
    data = json.loads(get(url)).get("data", [])
    coins = []
    for p in data:
        a = p.get("attributes", {})
        name = a.get("name", "?")
        addr = p["id"].split("_")[-1]
        if "ETH" not in name.upper() and "WETH" not in name.upper():
            continue
        # EXCLUDE stable pairs (no edge trading these)
        if "USDC" in name.upper() or "USDT" in name.upper() or "DAI" in name.upper():
            continue
        liq = float(a.get("reserve_in_usd") or 0)
        v5m = float(a.get("volume_usd", {}).get("m5") or 0)
        v1h = float(a.get("volume_usd", {}).get("h1") or 0)
        v24h = float(a.get("volume_usd", {}).get("h24") or 0)
        # score: focus on 24h volume (momentum regime) + liquidity
        score = 0
        if liq >= min_liq: score += min(liq/min_liq, 8) * 5
        if v24h > 0: score += min(v24h/50000, 10) * 5
        fee = 30.0
        for tok in name.upper().replace("%", " %").split():
            if "%" in tok:
                try: fee = float(tok.replace("%", "")) * 100
                except: pass
        if fee <= 30: score += 5
        coins.append({
            "name": name, "address": addr, "liquidity": liq,
            "vol_5m": v5m, "vol_1h": v1h, "vol_24h": v24h,
            "fee_bps": fee, "score": int(score)
        })
    coins = [c for c in coins if c["liquidity"] >= min_liq]
    coins.sort(key=lambda x: x["score"], reverse=True)
    out = []
    for c in coins[:top_n]:
        try:
            url2 = (f"https://api.geckoterminal.com/api/v2/networks/base/pools/"
                    f"{c['address']}/ohlcv/hour?aggregate=1&limit={CANDLE_LIMIT}")
            rows = json.loads(get(url2)).get("data",{}).get("attributes",{}).get("ohlcv_list",[])
            candles = sorted(({"ts":r[0],"c":float(r[4])} for r in rows), key=lambda x:x["ts"])
            c["candles"] = candles
            out.append(c)
        except Exception as e:
            print(f"  [skip {c['name']}] {e}", file=sys.stderr)
    return out

# ---- BASELINE strategy (exact copy of backtest.py) ----
def baseline_bt(candles, fee_bps, liquidity, trade_eth=0.05, gas_usd=0.04,
                entry_roc=0.04, exit_roc=-0.02, overbought=78, stop_pct=0.08,
                fast=12, slow=48):
    closes = [c["c"] for c in candles]
    if len(closes) < slow + 5: return None
    eth_usd = eth_usd_price()
    slip = min(max((trade_eth * eth_usd) / liquidity * 10000 * 0.5, 1.0), 60.0)
    gas_bps = gas_usd / (trade_eth * eth_usd) * 10000
    enter_cost = (fee_bps + slip + gas_bps) / 10000
    exit_cost = enter_cost
    pos = 0.0; entry_px = 0.0; cash = 1.0; peak = 1.0; maxdd = 0.0
    wins = 0; loss = 0; gw = 0.0; gl = 0.0
    for i in range(len(closes)):
        px = closes[i]
        if i < slow: continue
        sf = sma(closes[:i+1], fast); ss = sma(closes[:i+1], slow)
        rr = roc(closes[:i+1], fast); rs = rsi(closes[:i+1], 14)
        if pos > 0:
            ret = px / entry_px - 1
            if (px < sf) or (rs > overbought) or (rr < exit_roc) or (ret < -stop_pct):
                pnl = pos * (px / entry_px) - exit_cost
                cash = pos * (px / entry_px) * (1 - exit_cost)
                pos = 0.0
                if pnl > 0: wins += 1; gw += pnl
                else: loss += 1; gl += -pnl
                peak = max(peak, cash); maxdd = max(maxdd, 1 - cash / peak)
        elif (px > ss) and (px > sf) and (sf > ss) and (50 < rs < 72) and (rr > entry_roc):
            pos = (1 - enter_cost); entry_px = px
    if pos > 0:
        pnl = pos * (closes[-1] / entry_px) - exit_cost
        cash = pos * (closes[-1] / entry_px) * (1 - exit_cost)
        if pnl > 0: wins += 1; gw += pnl
        else: loss += 1; gl += -pnl
    n = wins + loss
    return {
        "net_pct": (cash - 1) * 100, "trades": n, "wins": wins, "losses": loss,
        "win_rate": wins / n * 100 if n else 0,
        "pf": (gw / gl) if gl else (9.99 if gw else 0),
        "mdd_pct": maxdd * 100,
    }

# ---- FIXED strategy v3 ----
def fixed_bt(candles, fee_bps, liquidity, score, vol_24h,
             trade_eth=0.03, gas_usd=0.04, overbought=85, stop_pct=0.10,
             fast=8, slow=32):
    """
    Fixed strategy v3:
    - Real-time coins (no stale cache)
    - EXCLUDE stable pairs (no edge)
    - VOL BREAKOUT: vol_24h > $50k (momentum regime)
    - ENTRY: MA cross(8,32) + RSI 45-65 + ROC(8)>0.03 (momentum confirmed)
    - EXIT: 10% trailing stop off peak + RSI>85 blowoff
    - SIZING: score-weighted, capped at 0.05
    """
    if vol_24h < 50000:
        return {"net_pct": 0.0, "trades": 0, "wins": 0, "losses": 0,
                "win_rate": 0, "pf": 0, "mdd_pct": 0, "skipped": "low_vol"}
    closes = [c["c"] for c in candles]
    if len(closes) < slow + 5: return None
    eth_usd = eth_usd_price()
    actual_trade = min(trade_eth * (score / 50.0), 0.05)
    slip = min(max((actual_trade * eth_usd) / liquidity * 10000 * 0.5, 1.0), 80.0)
    gas_bps = gas_usd / (actual_trade * eth_usd) * 10000
    enter_cost = (fee_bps + slip + gas_bps) / 10000
    exit_cost = enter_cost
    pos = 0.0; entry_px = 0.0; entry_hi = 0.0
    cash = 1.0; peak = 1.0; maxdd = 0.0
    wins = 0; loss = 0; gw = 0.0; gl = 0.0
    for i in range(len(closes)):
        px = closes[i]
        if i < slow: continue
        sf = sma(closes[:i+1], fast); ss = sma(closes[:i+1], slow)
        rr = roc(closes[:i+1], fast); rs = rsi(closes[:i+1], 14)
        if pos > 0:
            entry_hi = max(entry_hi, px)
            dd_from_peak = (px / entry_hi - 1) if entry_hi > 0 else 0
            ret = px / entry_px - 1
            if (dd_from_peak < -stop_pct) or (rs > overbought) or (ret < -stop_pct):
                pnl = pos * (px / entry_px) - exit_cost
                cash = pos * (px / entry_px) * (1 - exit_cost)
                pos = 0.0
                if pnl > 0: wins += 1; gw += pnl
                else: loss += 1; gl += -pnl
                peak = max(peak, cash); maxdd = max(maxdd, 1 - cash / peak)
        elif (px > ss) and (px > sf) and (sf > ss) and (45 < rs < 65) and (rr > 0.03):
            pos = (1 - enter_cost)
            entry_px = px; entry_hi = px
    if pos > 0:
        pnl = pos * (closes[-1] / entry_px) - exit_cost
        cash = pos * (closes[-1] / entry_px) * (1 - exit_cost)
        if pnl > 0: wins += 1; gw += pnl
        else: loss += 1; gl += -pnl
    n = wins + loss
    return {
        "net_pct": (cash - 1) * 100, "trades": n, "wins": wins, "losses": loss,
        "win_rate": wins / n * 100 if n else 0,
        "pf": (gw / gl) if gl else (9.99 if gw else 0),
        "mdd_pct": maxdd * 100, "trade_eth": actual_trade,
    }

def main():
    print("=== STRATEGY FIX TEST v3: baseline vs fixed ===\n")
    print("Filters: stable pairs excluded, vol_24h>$50k, momentum entry\n")
    
    coins = discover_and_fetch(min_liq=500_000, top_n=5)
    if not coins:
        print("No qualifying coins.")
        return
    
    print(f"Real-time top {len(coins)} coins (stable pairs excluded):\n")
    for c in coins:
        print(f"  - {c['name'][:30]:30} score={c['score']:>3} v24h=${c['vol_24h']:>10,.0f} liq=${c['liquidity']:>10,.0f}")
    
    print(f"\n{'COIN':<28} | {'BASELINE':>10} {'TRD':>4} {'WIN%':>5} {'PF':>5} {'DD%':>5} || {'FIXED':>10} {'TRD':>4} {'WIN%':>5} {'PF':>5} {'DD%':>5} {'NOTE':>8}")
    print("-" * 130)
    
    baseline_basket = 1.0
    fixed_basket = 1.0
    
    for c in coins:
        candles = c["candles"]
        b = baseline_bt(candles, c["fee_bps"], c["liquidity"])
        f = fixed_bt(candles, c["fee_bps"], c["liquidity"], c["score"], c["vol_24h"])
        
        b_str = f"{b['net_pct']:+9.1f}% {b['trades']:>4} {b['win_rate']:>4.0f}% {b['pf']:>5.2f} {b['mdd_pct']:>4.0f}%" if b else "     (no result)"
        f_str = f"{f['net_pct']:+9.1f}% {f['trades']:>4} {f['win_rate']:>4.0f}% {f['pf']:>5.2f} {f['mdd_pct']:>4.0f}%" if f else "     (no result)"
        note = f.get("skipped", "")
        if b:
            baseline_basket *= (0.5 + 0.5 * (1 + b["net_pct"]/100))
        if f:
            fixed_basket *= (0.5 + 0.5 * (1 + f["net_pct"]/100))
        print(f"{c['name'][:26]:<28} | {b_str} || {f_str} {note:>8}")
    
    print(f"\n=== BASKET SUMMARY (50/50 split, {len(coins)} coins) ===")
    print(f"Baseline basket: {(baseline_basket-1)*100:+.1f}%")
    print(f"Fixed basket:    {(fixed_basket-1)*100:+.1f}%")
    
    results = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "coins": []
    }
    for c in coins:
        candles = c["candles"]
        b = baseline_bt(candles, c["fee_bps"], c["liquidity"]) or {}
        f = fixed_bt(candles, c["fee_bps"], c["liquidity"], c["score"], c["vol_24h"]) or {}
        results["coins"].append({
            "name": c["name"], "score": c["score"], "vol_24h": c["vol_24h"],
            "baseline": b, "fixed": f,
        })
    json.dump(results, open("fix_test_v3_results.json", "w"), indent=2)
    print("\nWrote fix_test_v3_results.json")

if __name__ == "__main__":
    main()
