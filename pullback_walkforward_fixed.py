#!/usr/bin/env python3
"""
PULLBACK v2 WALK-FORWARD (FIXED)
- Time-matched benchmark: compares strategy return vs holding for same # of candles
- Longer lookback (200-300 candles) for more trade signals
- More coins from discovery + early pools
- Compounds within walk-forward windows
"""
import json, time, urllib.request
from datetime import datetime, timezone
from indicators import sma, rsi, roc, eth_usd_price, CANDLE_LIMIT

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json;version=20230302"}

def get(url, timeout=25, retries=5):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r: return r.read().decode()
        except Exception as e:
            if "429" in str(e) and i < retries - 1: time.sleep(2 + i * 2); continue
            if i < retries - 1: time.sleep(1.5); continue
            raise

def discover_all():
    """Merge trending + new pools for more coins."""
    coins = {}
    # Trending
    try:
        data = json.loads(get("https://api.geckoterminal.com/api/v2/networks/base/trending_pools?page=1")).get("data", [])
        for p in data:
            a = p.get("attributes", {}); addr = p["id"].split("_")[-1]; name = a.get("name","?")
            if not ("ETH" in name.upper() or "WETH" in name.upper()): continue
            if "USDC" in name.upper() or "USDT" in name.upper() or "DAI" in name.upper(): continue
            liq = float(a.get("reserve_in_usd") or 0); v24h = float(a.get("volume_usd",{}).get("h24") or 0)
            h24 = float(a.get("price_change_percentage",{}).get("h24",0))
            if h24 <= 2 or v24h < 50000 or liq < 100000: continue
            fee = 30.0
            for tok in name.upper().replace("%", " %").split():
                if tok.replace(".","").isdigit() or "%" in tok:
                    try: fee = float(tok.replace("%","")) * 100
                    except: pass
            coins[addr] = {"name": name, "address": addr, "liquidity": liq, "fee_bps": fee, "score": 50, "source": "trending"}
    except Exception as e:
        print(f"  [warn] trending: {e}")
    # New pools (early)
    try:
        data = json.loads(get("https://api.geckoterminal.com/api/v2/networks/base/new_pools?page=1")).get("data", [])
        for p in data:
            a = p.get("attributes", {}); addr = p["id"].split("_")[-1]; name = a.get("name","?")
            if not ("ETH" in name.upper() or "WETH" in name.upper()): continue
            if "USDC" in name.upper() or "USDT" in name.upper() or "DAI" in name.upper(): continue
            liq = float(a.get("reserve_in_usd") or 0)
            if liq < 100000: continue
            if addr not in coins:
                coins[addr] = {"name": name, "address": addr, "liquidity": liq, "fee_bps": 30.0, "score": 40, "source": "new"}
    except Exception as e:
        print(f"  [warn] new_pools: {e}")
    return list(coins.values())

def fetch_ohlcv(addr, limit=300):
    url = f"https://api.geckoterminal.com/api/v2/networks/base/pools/{addr}/ohlcv/hour?aggregate=1&limit={limit}"
    rows = json.loads(get(url)).get("data",{}).get("attributes",{}).get("ohlcv_list",[])
    return sorted(({"ts":r[0],"o":float(r[1]),"h":float(r[2]),"l":float(r[3]),"c":float(r[4]),"v":float(r[5])} for r in rows), key=lambda x: x["ts"])

def backtest_pullback(candles, fee_bps, liquidity, score=50, trade_eth=0.03, gas_usd=0.04, fast=8, slow=32):
    closes=[c["c"] for c in candles]
    if len(closes) < slow + 5: return None
    eth_usd = eth_usd_price()
    actual_trade = min(trade_eth * (score / 50.0), 0.05)
    stop_pct = 0.08 if score >= 60 else 0.10
    slip = min(max((actual_trade * eth_usd) / liquidity * 10000 * 0.5, 1.0), 80.0)
    gas = gas_usd / (actual_trade * eth_usd) * 10000
    ec = (fee_bps + slip + gas) / 10000
    xc = ec
    pos = 0.0; entry_px = 0.0; entry_hi = 0.0; cash = 1.0; peak = 1.0; mdd = 0.0
    wins = 0; loss = 0; gw = 0.0; gl = 0.0
    entry_bar = 0; total_held_bars = 0; first_entry_bar = None; last_exit_bar = 0
    for i in range(len(closes)):
        px = closes[i]
        if i < slow: continue
        sf = sma(closes[:i+1], fast); ss = sma(closes[:i+1], slow)
        r = roc(closes[:i+1], fast); rs = rsi(closes[:i+1], 14)
        if pos > 0:
            entry_hi = max(entry_hi, px)
            dd = (px / entry_hi - 1) if entry_hi > 0 else 0
            ret = px / entry_px - 1
            if dd < -stop_pct or rs > 85 or ret < -stop_pct:
                before = cash; cash = pos * (px / entry_px) * (1 - xc); pnl = cash - before
                pos = 0.0; entry_hi = 0.0
                if pnl > 0: wins += 1; gw += pnl
                else: loss += 1; gl += -pnl
                peak = max(peak, cash); mdd = max(mdd, 1 - cash / peak)
                last_exit_bar = i
        elif (px > ss) and (px < sf) and (20 < rs < 55) and (-0.10 < r < 0.0):
            roc24 = roc(closes[:i+1], 24) if len(closes[:i+1]) > 24 else 0
            if roc24 > 0:
                pos = cash * (1 - ec); entry_px = px; entry_hi = px
                if first_entry_bar is None: first_entry_bar = i
                entry_bar = i
    if pos > 0:
        before = cash; cash = pos * (closes[-1] / entry_px) * (1 - xc); pnl = cash - before
        if pnl > 0: wins += 1; gw += pnl
        else: loss += 1; gl += -pnl
    n = wins + loss; net = cash - 1.0
    return {
        "net": net * 100, "trades": n, "wr": wins / n * 100 if n else 0,
        "pf": (gw / gl if gl else (9.99 if gw else 0)), "mdd": mdd * 100,
        "first_entry": first_entry_bar, "last_exit": last_exit_bar,
    }

def time_matched_benchmark(candles, first_entry, last_exit):
    """Return buy-and-hold return over the same bars as strategy was active."""
    if first_entry is None or last_exit is None or first_entry >= last_exit:
        return None
    if first_entry >= len(candles) or last_exit >= len(candles):
        return None
    entry_px = candles[first_entry]["c"]
    exit_px = candles[last_exit]["c"]
    return (exit_px / entry_px - 1) * 100

def walk_forward(candles, fee_bps, liquidity, score=50, fast=8, slow=32, train_frac=0.6):
    closes = [c["c"] for c in candles]
    if len(closes) < slow + 10: return None
    k = int(len(closes) * train_frac)
    if k < slow + 5 or len(closes) - k < 10: return None
    tr_candles = candles[:k]; te_candles = candles[k:]
    r_tr = backtest_pullback(tr_candles, fee_bps, liquidity, score, fast=fast, slow=slow)
    r_te = backtest_pullback(te_candles, fee_bps, liquidity, score, fast=fast, slow=slow)
    if not r_tr or not r_te: return None
    # Time-matched benchmark
    bh_te = time_matched_benchmark(te_candles, r_te["first_entry"], r_te["last_exit"])
    bh_tr = time_matched_benchmark(tr_candles, r_tr["first_entry"], r_tr["last_exit"])
    return {
        "train_net": r_tr["net"], "test_net": r_te["net"],
        "bh_train": bh_tr, "bh_test": bh_te,
        "pf_test": r_te["pf"], "maxdd_test": r_te["mdd"],
        "trades_train": r_tr["trades"], "trades_test": r_te["trades"],
    }

def main():
    print("=== PULLBACK v2 WALK-FORWARD (FIXED) ===\n")
    coins = discover_all()
    print(f"Discovered {len(coins)} coins (trending + new pools)\n")
    passed = 0; rows = []
    for c in coins:
        try: candles = fetch_ohlcv(c["address"], limit=300)
        except Exception as e: print(f"  skip {c['name']}: {e}"); continue
        if len(candles) < 80:
            print(f"  {c['name'][:20]:20} only {len(candles)} candles, skip"); continue
        time.sleep(1.5)
        e = walk_forward(candles, c["fee_bps"], c["liquidity"], score=c.get("score", 50))
        if not e: continue
        flags = []
        if e["trades_test"] == 0:
            flags.append("no trades in test window")
        else:
            if e["bh_test"] is not None and e["test_net"] <= e["bh_test"]:
                flags.append(f"FAILS time-matched BH ({e['bh_test']:+.1f}%)")
            if e["test_net"] <= 0:
                flags.append("FAILS live (<=0)")
            if e["pf_test"] < 1.1:
                flags.append(f"FAILS PF={e['pf_test']:.2f}")
            if e["maxdd_test"] > 40:
                flags.append(f"FAILS DD={e['maxdd_test']:.0f}%")
            if e["train_net"] - e["test_net"] > 30:
                flags.append("OVERFIT risk")
        v = "PASS" if not flags else "REJECT: " + "; ".join(flags)
        if v == "PASS": passed += 1
        rows.append((c["name"], e, v))
        bh_str = f"{e['bh_test']:+.1f}%" if e['bh_test'] is not None else "n/a"
        print(f"  {c['name'][:24]:24} test={e['test_net']:+7.1f}% BH={bh_str:>8} "
              f"PF={e['pf_test']:.2f} DD={e['maxdd_test']:.0f}% "
              f"trades={e['trades_train']}/{e['trades_test']} -> {v}")
    print(f"\n=== VERDICT ===")
    print(f"{passed}/{len(rows)} coins PASS all gates")
    if passed == 0:
        print("No coins clear the benchmark. Strategy needs more tuning.")
    else:
        print(f"{passed} candidate(s). Incubate 1-2+ months before sizing up.")
    artifact = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "script": "pullback_v2_walkforward_fixed",
        "strategy": "pullback_v2_rsi20-55_roc-10%-0%",
        "total": len(rows), "passed": passed, "any_passed": passed > 0,
        "rows": [{"name": n, "test_net": e["test_net"], "bh_test": e["bh_test"],
                  "pf_test": e["pf_test"], "maxdd_test": e["maxdd_test"],
                  "trades_test": e["trades_test"], "verdict": v}
                 for n, e, v in rows]
    }
    json.dump(artifact, open("methodology_eval_pullback.json", "w"), indent=2)
    print("Wrote methodology_eval_pullback.json")

if __name__ == "__main__":
    main()
