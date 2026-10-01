#!/usr/bin/env python3
"""
STRATEGY FIX TEST ENGINE — validates proposed improvements vs baseline.

Proposed fixes to test:
  1. RELAXED entry: MA cross (fast>slow) + RSI>50 + vol>$5k (drop ROC>entry_roc)
  2. TRAILING stop exit: 10% off peak + RSI>85 blowoff (no more SMA_fast whipsaw)
  3. HIGHER quality filter: liq>$500k + vol>$5k + score>50
  4. SCORE-WEIGHTED sizing: 0.03 ETH * (score/50), max 0.05
  5. TOP-3 BASKET: only the 3 highest-scored coins (not all 13)

This file does NOT modify the existing engines. It imports indicator helpers
and network helpers, runs BOTH the baseline and the fixed strategy on the
same candles, and reports the comparison. Run: python3 fix_test_engine.py
"""
import json, sys, time, urllib.request
from datetime import datetime, timezone

# Reuse existing helpers
sys.path.insert(0, "/home/kenzodadon/base-trend-bot")
from indicators import sma, rsi, roc, eth_usd_price, CANDLE_LIMIT

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json;version=20230302"}
_last = 0.0
_GAP = 1.2  # be gentle on the free tier

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

def discover_and_fetch(min_liq=500_000, min_score=40, top_n=5):
    """Discover coins, score them, return top_n with candles."""
    url = "https://api.geckoterminal.com/api/v2/networks/base/trending_pools?page=1"
    data = json.loads(get(url)).get("data", [])
    coins = []
    for p in data:
        a = p.get("attributes", {})
        name = a.get("name", "?")
        addr = p["id"].split("_")[-1]
        if "ETH" not in name.upper() and "WETH" not in name.upper():
            continue
        liq = float(a.get("reserve_in_usd") or 0)
        v5m = float(a.get("volume_usd", {}).get("m5") or 0)
        v1h = float(a.get("volume_usd", {}).get("h1") or 0)
        # score
        score = 0
        if liq >= min_liq: score += min(liq/min_liq, 8) * 5
        if v5m > 0: score += min(v5m/1000, 5) * 6
        fee = 30.0
        for tok in name.upper().replace("%", " %").split():
            if "%" in tok:
                try: fee = float(tok.replace("%", "")) * 100
                except: pass
        if fee <= 30: score += 5
        coins.append({
            "name": name, "address": addr, "liquidity": liq,
            "vol_5m": v5m, "vol_1h": v1h, "fee_bps": fee, "score": int(score)
        })
    # filter + sort
    coins = [c for c in coins if c["liquidity"] >= min_liq and c["score"] >= min_score]
    coins.sort(key=lambda x: x["score"], reverse=True)
    # fetch candles for top_n
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

# ---- BASELINE strategy (exact copy of backtest.py logic) ----
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
    trades = []
    for i in range(len(closes)):
        px = closes[i]
        if i < slow: continue
        sf = sma(closes[:i+1], fast); ss = sma(closes[:i+1], slow)
        rr = roc(closes[:i+1], fast); rs = rsi(closes[:i+1], 14)
        # EXIT
        if pos > 0:
            ret = px / entry_px - 1
            if (px < sf) or (rs > overbought) or (rr < exit_roc) or (ret < -stop_pct):
                pnl = pos * (px / entry_px) - exit_cost
                cash = pos * (px / entry_px) * (1 - exit_cost)
                pos = 0.0
                trades.append(pnl)
                if pnl > 0: wins += 1; gw += pnl
                else: loss += 1; gl += -pnl
                peak = max(peak, cash); maxdd = max(maxdd, 1 - cash / peak)
        # ENTRY
        elif (px > ss) and (px > sf) and (sf > ss) and (50 < rs < 72) and (rr > entry_roc):
            pos = (1 - enter_cost)
            entry_px = px
    if pos > 0:
        pnl = pos * (closes[-1] / entry_px) - exit_cost
        cash = pos * (closes[-1] / entry_px) * (1 - exit_cost)
        trades.append(pnl)
        if pnl > 0: wins += 1; gw += pnl
        else: loss += 1; gl += -pnl
    n = wins + loss
    return {
        "net_pct": (cash - 1) * 100,
        "trades": n, "wins": wins, "losses": loss,
        "win_rate": wins / n * 100 if n else 0,
        "pf": (gw / gl) if gl else (9.99 if gw else 0),
        "mdd_pct": maxdd * 100,
        "cost_bps_rtrip": (enter_cost + exit_cost) * 10000,
    }

# ---- FIXED strategy ----
def fixed_bt(candles, fee_bps, liquidity, score,
             trade_eth=0.03, gas_usd=0.04, overbought=85, stop_pct=0.10,
             fast=8, slow=32):
    """
    Fixed strategy:
    - RELAXED entry: MA cross + RSI>50 only (no ROC filter, wider RSI band)
    - TRAILING stop exit: 10% off peak price + RSI>85 blowoff
    - SCORE-WEIGHTED sizing: trade_eth * (score/50), capped at 0.05
    - Tighter MA windows (8/32 instead of 12/48) for faster signal on alts
    """
    closes = [c["c"] for c in candles]
    if len(closes) < slow + 5: return None
    eth_usd = eth_usd_price()
    # Scale trade size by score
    actual_trade = min(trade_eth * (score / 50.0), 0.05)
    slip = min(max((actual_trade * eth_usd) / liquidity * 10000 * 0.5, 1.0), 80.0)
    gas_bps = gas_usd / (actual_trade * eth_usd) * 10000
    enter_cost = (fee_bps + slip + gas_bps) / 10000
    exit_cost = enter_cost
    pos = 0.0; entry_px = 0.0; entry_hi = 0.0
    cash = 1.0; peak = 1.0; maxdd = 0.0
    wins = 0; loss = 0; gw = 0.0; gl = 0.0
    trades = []
    for i in range(len(closes)):
        px = closes[i]
        if i < slow: continue
        sf = sma(closes[:i+1], fast); ss = sma(closes[:i+1], slow)
        rs = rsi(closes[:i+1], 14)
        # EXIT: trailing stop 10% off peak OR RSI>85 blowoff
        if pos > 0:
            entry_hi = max(entry_hi, px)
            dd_from_peak = (px / entry_hi - 1) if entry_hi > 0 else 0
            ret = px / entry_px - 1
            if (dd_from_peak < -stop_pct) or (rs > overbought) or (ret < -stop_pct):
                pnl = pos * (px / entry_px) - exit_cost
                cash = pos * (px / entry_px) * (1 - exit_cost)
                pos = 0.0
                trades.append(pnl)
                if pnl > 0: wins += 1; gw += pnl
                else: loss += 1; gl += -pnl
                peak = max(peak, cash); maxdd = max(maxdd, 1 - cash / peak)
        # ENTRY: relaxed — MA cross + RSI>50 (no ROC requirement)
        elif (px > ss) and (px > sf) and (sf > ss) and (rs > 50):
            pos = (1 - enter_cost)
            entry_px = px
            entry_hi = px
    if pos > 0:
        pnl = pos * (closes[-1] / entry_px) - exit_cost
        cash = pos * (closes[-1] / entry_px) * (1 - exit_cost)
        trades.append(pnl)
        if pnl > 0: wins += 1; gw += pnl
        else: loss += 1; gl += -pnl
    n = wins + loss
    return {
        "net_pct": (cash - 1) * 100,
        "trades": n, "wins": wins, "losses": loss,
        "win_rate": wins / n * 100 if n else 0,
        "pf": (gw / gl) if gl else (9.99 if gw else 0),
        "mdd_pct": maxdd * 100,
        "cost_bps_rtrip": (enter_cost + exit_cost) * 10000,
        "trade_eth": actual_trade,
    }

def main():
    print("=== STRATEGY FIX TEST: baseline vs fixed ===\n")
    
    # Discover and fetch (same coins for both strategies)
    coins = discover_and_fetch(min_liq=500_000, min_score=40, top_n=5)
    if not coins:
        print("No qualifying coins — try lowering filters.")
        return
    
    print(f"Testing on top {len(coins)} discovered coins (liq>$500k, vol>$5k, score>=50):\n")
    for c in coins:
        print(f"  - {c['name'][:30]:30} score={c['score']:>3} liq=${c['liquidity']:>10,.0f} v5m=${c['vol_5m']:>8,.0f}")
    
    print(f"\n{'COIN':<28} | {'BASELINE':>10} {'TRD':>4} {'WIN%':>5} {'PF':>5} {'DD%':>5} || {'FIXED':>10} {'TRD':>4} {'WIN%':>5} {'PF':>5} {'DD%':>5} {'ETH':>5}")
    print("-" * 120)
    
    baseline_basket = 1.0
    fixed_basket = 1.0
    
    for c in coins:
        candles = c["candles"]
        
        # Baseline
        b = baseline_bt(candles, c["fee_bps"], c["liquidity"])
        # Fixed
        f = fixed_bt(candles, c["fee_bps"], c["liquidity"], c["score"])
        
        if b and f:
            b_eq = 1 + b["net_pct"]/100
            f_eq = 1 + f["net_pct"]/100
            # 50/50 split basket compounding
            baseline_basket *= (0.5 + 0.5 * b_eq)
            fixed_basket *= (0.5 + 0.5 * f_eq)
            name = c["name"][:26]
            print(f"{name:<28} | {b['net_pct']:+9.1f}% {b['trades']:>4} {b['win_rate']:>4.0f}% {b['pf']:>5.2f} {b['mdd_pct']:>4.0f}% || {f['net_pct']:+9.1f}% {f['trades']:>4} {f['win_rate']:>4.0f}% {f['pf']:>5.2f} {f['mdd_pct']:>4.0f}% {f['trade_eth']:>5.3f}")
        elif b:
            name = c["name"][:26]
            print(f"{name:<28} | {b['net_pct']:+9.1f}% {b['trades']:>4} {b['win_rate']:>4.0f}% {b['pf']:>5.2f} {b['mdd_pct']:>4.0f}% || (no fixed result)")
        elif f:
            name = c["name"][:26]
            print(f"{name:<28} | (no baseline)                  || {f['net_pct']:+9.1f}% {f['trades']:>4} {f['win_rate']:>4.0f}% {f['pf']:>5.2f} {f['mdd_pct']:>4.0f}%")
        else:
            name = c["name"][:26]
            print(f"{name:<28} | (no results)")
    
    print(f"\n=== BASKET SUMMARY (50/50 split, {len(coins)} coins) ===")
    print(f"Baseline basket: {(baseline_basket-1)*100:+.1f}%")
    print(f"Fixed basket:    {(fixed_basket-1)*100:+.1f}%")
    
    # Save results for analysis
    results = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "note": "Single-coin comparison only — basket is illustrative",
        "coins": []
    }
    for c in coins:
        candles = c["candles"]
        b = baseline_bt(candles, c["fee_bps"], c["liquidity"]) or {}
        f = fixed_bt(candles, c["fee_bps"], c["liquidity"], c["score"]) or {}
        results["coins"].append({
            "name": c["name"], "score": c["score"],
            "baseline": b, "fixed": f,
        })
    json.dump(results, open("fix_test_results.json", "w"), indent=2)
    print("\nWrote fix_test_results.json")

if __name__ == "__main__":
    main()
