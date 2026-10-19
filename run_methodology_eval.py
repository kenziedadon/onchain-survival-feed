#!/usr/bin/env python3
"""Run methodology eval on coins from discovered.jsonl (same source as grid_search)."""
import json, sys, time, urllib.request
from datetime import datetime, timezone
sys.path.insert(0, '.')

from indicators import CANDLE_LIMIT, sma, rsi, roc, eth_usd_price
from backtest import fetch_ohlcv, backtest
from backtest import fetch_ohlcv, backtest

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json;version=20230302"}
_last=0.0; GAP=4.0
def get(url, timeout=25, retries=8):
    global _last
    for i in range(retries):
        w = GAP-(time.time()-_last)
        if w>0: time.sleep(w)
        _last=time.time()
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode()
        except Exception as e:
            back=8+i*5 if "429" in str(e) else 2.5
            if i<retries-1: time.sleep(back); continue
            raise

def fetch_candles(addr, limit=500):
    url=f"https://api.geckoterminal.com/api/v2/networks/base/pools/{addr}/ohlcv/hour?aggregate=1&limit={limit}"
    rows=json.loads(get(url)).get("data",{}).get("attributes",{}).get("ohlcv_list",[])
    return sorted(({"ts":r[0],"o":float(r[1]),"h":float(r[2]),"l":float(r[3]),"c":float(r[4]),"v":float(r[5])} for r in rows),key=lambda x:x["ts"])

def buy_and_hold(candles):
    if len(candles) < 2: return None
    return (candles[-1]["c"] / candles[0]["c"] - 1) * 100

def split(closes, train_frac=0.6):
    k = int(len(closes) * train_frac)
    return closes[:k], closes[k:]

def walk_forward_eval(candles, fee_bps, liquidity, score=50,
                      fast=12, slow=48, stop_pct=0.08):
    closes = [c["c"] for c in candles]
    if len(closes) < slow + 5: return None
    tr_closes, te_closes = split(closes)
    tr_candles = candles[:len(tr_closes)]
    te_candles = candles[len(tr_closes):]
    r_tr = backtest(tr_candles, fee_bps, liquidity, score=score,
                    trade_eth=0.0005, gas_usd=0.04, stop_pct=stop_pct,
                    fast=fast, slow=slow)
    r_te = backtest(te_candles, fee_bps, liquidity, score=score,
                    trade_eth=0.0005, gas_usd=0.04, stop_pct=stop_pct,
                    fast=fast, slow=slow)
    if not r_tr or not r_te: return None
    bh_tr = buy_and_hold(tr_candles)
    bh_te = buy_and_hold(te_candles)
    return {
        "train_net": r_tr["net_return_pct"], "test_net": r_te["net_return_pct"],
        "bh_train": bh_tr, "bh_test": bh_te,
        "pf_test": r_te["profit_factor"], "maxdd_test": r_te["max_drawdown_pct"],
    }

def verdict(e):
    flags = []
    if e["test_net"] <= e["bh_test"]:
        flags.append("FAILS benchmark: buy-and-hold beat strategy on unseen data")
    if e["test_net"] <= 0:
        flags.append("FAILS live: negative on unseen data")
    if e["pf_test"] < 1.1:
        flags.append(f"FAILS profit factor: PF={e['pf_test']:.2f} (<1.1)")
    if e["maxdd_test"] > 40:
        flags.append(f"FAILS DD: max drawdown {e['maxdd_test']:.0f}% too high")
    if e["train_net"] - e["test_net"] > 30:
        flags.append(f"OVERFIT risk: train {e['train_net']:+.0f}% vs test {e['test_net']:+.0f}%")
    return "PASS" if not flags else "REJECT: " + "; ".join(flags)

# Load coins same way as grid_search.load_coins()
import os
from _tolerant_read import iter_jsonl_entries
coins = []
last_rec = None
last_rec = None
for rec in iter_jsonl_entries("discovered.jsonl"):
    last_rec = rec
if last_rec:
    coins = last_rec.get("coins", [])
out = []
for c in coins:
    if c["liquidity"] < 500000: continue
    name_up = c["name"].upper()
    if "USDC" in name_up or "USDT" in name_up or "DAI" in name_up: continue
    if c.get("change_24h", 0) <= 2: continue
    if c.get("score", 0) < 35: continue
    out.append(c)

print(f"=== METHODOLOGY EVAL ({len(out)} backtestable coins) ===")
for c in out:
    print(f"  {c['name'][:24]:24} liq=${c['liquidity']:>12,.0f} chg24h={c['change_24h']:+.2f}%")

passed = 0
rows = []
for c in out:
    try:
        candles = fetch_candles(c["address"])
    except Exception as ex:
        print(f"  [skip {c['name']}] {ex}")
        continue
    time.sleep(1.2)
    e = walk_forward_eval(candles, c["fee_bps"], c["liquidity"], score=c.get("score",50))
    if not e:
        print(f"  {c['name'][:24]:24} SKIP (not enough data)")
        continue
    v = verdict(e)
    if v == "PASS": passed += 1
    rows.append((c["name"], e, v))
    print(f"  {c['name'][:24]:24} train={e['train_net']:+6.1f}% test={e['test_net']:+6.1f}% | BH_test={e['bh_test']:+6.1f}% PF={e['pf_test']:.2f} DD={e['maxdd_test']:.0f}% -> {v}")

print(f"\n=== VERDICT ===")
print(f"{passed}/{len(rows)} coins PASS all gates.")
if passed == 0:
    print("CONSISTENT WITH PRIOR: no coin survives walk-forward + benchmark + PF/DD.")
else:
    print(f"{passed} candidate(s) survived methodology gates.")

artifact = {
    "ts": datetime.now(timezone.utc).isoformat(),
    "script": "evaluate_methodology.py (fresh run)",
    "total": len(rows),
    "passed": passed,
    "any_passed": passed > 0,
    "rows": [
        {"name": n, "test_net": e["test_net"], "bh_test": e["bh_test"],
         "pf_test": e["pf_test"], "maxdd_test": e["maxdd_test"], "verdict": v}
        for n, e, v in rows
    ],
}
json.dump(artifact, open("methodology_eval.json","w"), indent=2)
print("Wrote methodology_eval.json")
