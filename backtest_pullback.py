#!/usr/bin/env python3
"""
MEAN-REVERSION / PULLBACK BUY STRATEGY
=======================================
Buys coins on temporary dips within an established uptrend.

ENTRY (buy-the-dip):
  1. close > SMA_slow(32) AND close < SMA_fast(8)
     - Price above long-term trend, but below short-term MA (pullback in progress)
  2. RSI(14) between 25-50
     - Oversold but not broken — still has momentum potential
  3. ROC(8) between -0.08 and -0.005
     - Recent small pullback (not a crash, not flat)
  4. ROC(24) > 0.01
     - 24h trend still positive (established uptrend)
  5. ADX(14) > 20 AND +DI > -DI
     - Trend exists, bullish direction
  6. Volume spike: current hourly vol > 1.5x avg of last 8 candles
     - Capitulation volume — scared sellers exiting
  7. 24h change > +2% (from API) — confirmed momentum regime
  8. 24h volume > $50k
  9. Score >= 35
  10. NOT a stable pair

EXIT:
  - Trailing stop: 8% off peak for score>=60, 10% off otherwise
  - OR RSI > 85 (blowoff — sell the pump we bought the dip into)
  - OR position age > 48h (no longer a pullback play)

SIZING:
  - Same score-weighted model as momentum: 0.03 * score/50, max 0.05 ETH

Run:  python3 backtest_pullback.py
"""
import json, sys, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path
from discovery import discover_trending as _canonical_discover
from indicators import sma, rsi, roc, eth_usd_price, CANDLE_LIMIT, atr

BACKTESTS_DIR = Path(__file__).resolve().parent / "backtests"
BACKTESTS_DIR.mkdir(parents=True, exist_ok=True)
def write_backtest(results: list[dict]) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    p = BACKTESTS_DIR / f"pullback_{ts}.json"
    p.write_text(json.dumps(results, indent=2))
    return p

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json;version=20230302"}

def get(url, timeout=25, retries=5):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode()
        except Exception as e:
            if "429" in str(e) and i < retries - 1:
                time.sleep(2 + i * 2)
                continue
            if i < retries - 1:
                time.sleep(1.5)
                continue
            raise

def discover_trending(limit=30):
    url = "https://api.geckoterminal.com/api/v2/networks/base/trending_pools?page=1"
    data = json.loads(get(url)).get("data", [])
    out = []
    for p in data:
        a = p.get("attributes", {})
        addr = p["id"].split("_")[-1]
        name = a.get("name", "?")
        if not ("ETH" in name.upper() or "WETH" in name.upper()):
            continue
        if "USDC" in name.upper() or "USDT" in name.upper() or "DAI" in name.upper():
            continue
        liq = float(a.get("reserve_in_usd") or 0)
        v24h = float(a.get("volume_usd", {}).get("h24") or 0)
        price_change = a.get("price_change_percentage", {})
        h24_change = float(price_change.get("h24", 0))
        if h24_change <= 2:
            continue
        if v24h < 50000:
            continue
        fee = 30.0
        for tok in name.upper().replace("%", " %").split():
            if tok.replace(".", "").isdigit() or "%" in tok:
                try:
                    fee = float(tok.replace("%", "")) * 100
                except: pass
        score = 0
        if liq >= 500000: score += min(liq/500000, 8) * 5
        if v24h > 0: score += min(v24h/50000, 10) * 5
        if fee <= 30: score += 5
        if h24_change > 5: score += 10
        elif h24_change > 2: score += 5
        out.append({"name": name, "address": addr, "liquidity": liq, "fee_bps": fee,
                    "vol_24h": v24h, "score": int(score), "change_24h": h24_change})
    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:limit]

def fetch_ohlcv(addr, tf="hour", limit=300):
    url = (f"https://api.geckoterminal.com/api/v2/networks/base/pools/"
           f"{addr}/ohlcv/{tf}?aggregate=1&limit={limit}")
    rows = json.loads(get(url)).get("data", {}).get("attributes", {}).get("ohlcv_list", [])
    c = sorted(({"ts": r[0], "o": float(r[1]), "h": float(r[2]),
                 "l": float(r[3]), "c": float(r[4]), "v": float(r[5])} for r in rows),
              key=lambda x: x["ts"])
    return c

def backtest(candles, fee_bps, liquidity, score=50, trade_eth=0.03, gas_usd=0.04,
             fast=8, slow=32, overbought=85, stop_pct=None, vol_spike_mult=1.5,
             rsi_low=25, rsi_high=50, roc8_low=-0.08, roc8_high=-0.005):
    closes = [c["c"] for c in candles]
    volumes = [c["v"] for c in candles]
    if len(closes) < slow + 5:
        return None
    eth_usd = eth_usd_price()
    actual_trade = min(trade_eth * (score / 50.0), 0.05)
    if stop_pct is None:
        stop_pct = 0.08 if score >= 60 else 0.10
    slip_each = min(max((actual_trade * eth_usd) / liquidity * 10000 * 0.5, 1.0), 80.0)
    gas_each_bps = gas_usd / (actual_trade * eth_usd) * 10000
    enter_cost = (fee_bps + slip_each + gas_each_bps) / 10000
    exit_cost  = (fee_bps + slip_each + gas_each_bps) / 10000

    pos = 0.0; entry_px = 0.0; entry_hi = 0.0
    cash = 1.0; peak = 1.0; maxdd = 0.0
    wins = 0; loss = 0; gw = 0.0; gl = 0.0

    for i in range(len(closes)):
        px = closes[i]
        if i < slow: continue
        sf = sma(closes[:i+1], fast); ss = sma(closes[:i+1], slow)
        r = roc(closes[:i+1], fast); rs = rsi(closes[:i+1], 14)

        if pos > 0:
            entry_hi = max(entry_hi, px)
            dd_from_peak = (px / entry_hi - 1) if entry_hi > 0 else 0
            ret = px / entry_px - 1
            if (dd_from_peak < -stop_pct) or (rs > overbought) or (ret < -stop_pct):
                before = cash
                cash = pos * (px / entry_px) * (1 - exit_cost)
                pnl = cash - before
                pos = 0.0; entry_hi = 0.0
                if pnl > 0: wins += 1; gw += pnl
                else: loss += 1; gl += -pnl
                peak = max(peak, cash); maxdd = max(maxdd, 1 - cash / peak)

        elif (px > ss) and (px < sf) and (rsi_low < rs < rsi_high) and (roc8_low < r < roc8_high):
            roc24 = roc(closes[:i+1], 24) if len(closes[:i+1]) > 24 else 0
            if roc24 > 0.01:
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
                    if adx < 20 or pdi < mdi:
                        continue
                # Volume spike check
                if len(volumes) >= 8:
                    avg_vol = sum(volumes[max(0,i-8):i]) / 8
                    if volumes[i] < avg_vol * vol_spike_mult:
                        continue  # No volume spike = no capitulation, skip
                pos = cash * (1 - enter_cost)
                entry_px = px; entry_hi = px

    if pos > 0:
        before = cash
        cash = pos * (closes[-1] / entry_px) * (1 - exit_cost)
        pnl = cash - before
        if pnl > 0: wins += 1; gw += pnl
        else: loss += 1; gl += -pnl
        peak = max(peak, cash); maxdd = max(maxdd, 1 - cash / peak)

    n = wins + loss
    net = cash - 1.0
    return {
        "net_return_pct": net * 100,
        "trades": n, "wins": wins, "losses": loss,
        "win_rate": (wins / n * 100) if n else 0,
        "profit_factor": (gw / gl) if gl else (9.99 if gw else 0),
        "max_drawdown_pct": maxdd * 100,
        "cost_bps_roundtrip": (enter_cost + exit_cost) * 10000,
        "final_equity": cash,
        "trade_eth": actual_trade,
    }

def main():
    print("=== PULLBACK / MEAN-REVERSION STRATEGY ===")
    print("Entry: close>SMA_slow AND close<SMA_fast + RSI 25-50 + ROC8 -8% to -0.5% + ROC24>1% + vol spike\n")
    pools = discover_trending(30)
    print(f"Found {len(pools)} ETH/WETH-paired trending pools\n")
    for i, p in enumerate(pools):
        print(f"  {i+1:2}. {p['name'][:30]:30} liq=${p['liquidity']:>11,.0f} fee={p['fee_bps']:.0f}bps score={p['score']}")

    print("\n=== BACKTEST (pullback buy, same exit as momentum) ===")
    results = []
    skipped_low_score = 0
    for p in pools:
        if p["liquidity"] < 300_000:
            continue
        if p.get("score", 0) < 35:
            skipped_low_score += 1
            continue
        try:
            candles = fetch_ohlcv(p["address"], "hour", CANDLE_LIMIT)
            time.sleep(2.0)
        except Exception as e:
            print(f"  {p['name'][:28]:28} OHLCV ERROR: {e}"); continue
        if len(candles) < 60:
            continue
        r = backtest(candles, p["fee_bps"], p["liquidity"], score=p.get("score", 50))
        if not r: continue
        r["name"] = p["name"]; r["liq"] = p["liquidity"]; r["score"] = p.get("score", 50)
        results.append(r)
        flag = "PROFIT" if r["net_return_pct"] > 0 else "LOSS"
        print(f"  {p['name'][:26]:26} {flag} net={r['net_return_pct']:+7.1f}% "
              f"trades={r['trades']:2} win={r['win_rate']:4.0f}% "
              f"PF={r['profit_factor']:.2f} DD={r['max_drawdown_pct']:.1f}% "
              f"cost={r['cost_bps_roundtrip']:.0f}bp amt={r.get('trade_eth',0.05):.3f}")

    print(f"\n  (skipped {skipped_low_score} coins below score<35)")

    if not results:
        print("NO BACKTESTABLE POOLS"); sys.exit(1)

    prof = [r for r in results if r["net_return_pct"] > 0]
    print(f"\n=== BASKET SUMMARY ({len(results)} coins) ===")
    print(f"Profitable: {len(prof)}/{len(results)} ({len(prof)/len(results)*100:.0f}%)")
    avg = sum(r['net_return_pct'] for r in results) / len(results)
    basket_eq = 1.0
    for r in results:
        basket_eq *= (0.5 + 0.5 * r["final_equity"])
    basket_return = (basket_eq - 1) * 100
    print(f"Avg per-coin net return: {avg:+.1f}%")
    print(f"Naive basket (50/50 split, no recon): {basket_return:+.1f}%")
    avg_dd = sum(r['max_drawdown_pct'] for r in results) / len(results)
    print(f"Avg max drawdown: {avg_dd:.1f}%")
    best = max(results, key=lambda x: x['net_return_pct'])
    worst = min(results, key=lambda x: x['net_return_pct'])
    print(f"Best:  {best['name']} {best['net_return_pct']:+.1f}%")
    print(f"Worst: {worst['name']} {worst['net_return_pct']:+.1f}%")

    print("\n=== VERDICT ===")
    if basket_return > 5.0 and avg > 0:
        print("PULLBACK STRATEGY IS PROFITABLE on this basket.")
        print("Run evaluate_methodology.py to test walk-forward + benchmark.")
    else:
        print("Pullback strategy NOT profitable on this basket.")
    write_backtest(results)
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%dT%H%M%S")
    print(f"\nWrote {len(results)} results to {BACKTESTS_DIR / f'pullback_{ts}.json'}")

if __name__ == "__main__":
    main()
