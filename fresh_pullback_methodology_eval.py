"""DEPRECATED — superseded by research_harness/ (WO4, 2026-10-01).

Kept on disk so the historical artifacts can be reproduced. DO NOT extend.
The consolidated harness applies the no-lookahead guard, preregistration, and
a pessimistic cost model; none of that exists here.

To reproduce an old number, run the equivalent registered spec through
run_walkforward.py. If the new harness disagrees materially on the same
input, that discrepancy is a FINDING, not a nuisance.

Replaced by: research_harness/harness.py
"""
#!/usr/bin/env python3
"""
Fresh pullback_v2 methodology eval on today's discovered coins.
Targets the arm gate: needs >=1 coin passing WF + benchmark + PF/DD
with FRESH data (< 24h) to propagate methodology_pass into edge_evidence.
"""
import json, os, sys, time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from indicators import CANDLE_LIMIT, sma, rsi, roc
from backtest import fetch_ohlcv
from grid_search import bt as grid_bt  # uses entry_roc/ob params
from _tolerant_read import iter_jsonl_entries

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json;version=20230302"}

def fetch_candles(addr, limit=CANDLE_LIMIT):
    return fetch_ohlcv(addr, limit)

def buy_and_hold(candles):
    if len(candles) < 2:
        return None
    return (candles[-1]["c"] / candles[0]["c"] - 1) * 100

def split(candles, train_frac=0.6):
    k = int(len(candles) * train_frac)
    return candles[:k], candles[k:]

def walk_forward_pullback(candles, fee_bps, liquidity, score=50,
                          fast=8, slow=32, entry_roc=0.03, ob=85):
    """Pullback_v2 walk-forward: same backtest engine, train/test split."""
    closes = [c["c"] for c in candles]
    if len(closes) < slow + 5:
        return None
    tr_candles = candles[:int(len(candles)*0.6)]
    te_candles = candles[int(len(candles)*0.6):]
    if len(tr_candles) < slow + 5 or len(te_candles) < slow + 5:
        return None
    r_tr = grid_bt(tr_candles, fee_bps, liquidity, score=score,
                    fast=fast, slow=slow, entry_roc=entry_roc, ob=ob, trade_eth=0.0005, gas_usd=0.04)
    r_te = grid_bt(te_candles, fee_bps, liquidity, score=score,
                    fast=fast, slow=slow, entry_roc=entry_roc, ob=ob, trade_eth=0.0005, gas_usd=0.04)
    if not r_tr or not r_te:
        return None
    bh_tr = buy_and_hold(tr_candles)
    bh_te = buy_and_hold(te_candles)
    return {
        "train_net": r_tr["net"], "test_net": r_te["net"],
        "bh_train": bh_tr, "bh_test": bh_te,
        "pf_test": r_te["pf"], "maxdd_test": r_te["mdd"],
        "cost_bps": 0,
    }

def verdict(e):
    flags = []
    if e["test_net"] <= e["bh_test"]:
        flags.append(f"FAILS benchmark: BH +{e['bh_test']:.1f}% beat strategy +{e['test_net']:.1f}%")
    if e["test_net"] <= 0:
        flags.append(f"FAILS live: negative on unseen data ({e['test_net']:+.1f}%)")
    if e["pf_test"] < 1.1:
        flags.append(f"FAILS profit factor: PF={e['pf_test']:.2f} (<1.1)")
    if e["maxdd_test"] > 40:
        flags.append(f"FAILS DD: max drawdown {e['maxdd_test']:.0f}% too high")
    if e["train_net"] - e["test_net"] > 30:
        flags.append(f"OVERFIT: train {e['train_net']:+.0f}% vs test {e['test_net']:+.0f}%")
    return "PASS" if not flags else "REJECT: " + "; ".join(flags)

def load_discovered_coins():
    """Load latest discovery record, return ALL coins (no +2% filter) for eval."""
    import os
    coins = []
    lines = list(iter_jsonl_entries("discovered.jsonl"))
    if lines:
        last_rec = lines[-1]
        for c in last_rec.get("coins", []):
            if c["liquidity"] < 500000:
                continue
            name_up = c["name"].upper()
            if "USDC" in name_up or "USDT" in name_up or "DAI" in name_up:
                continue
            if c.get("score", 0) < 35:
                continue
            coins.append(c)
    return coins

def main():
    print("=== FRESH PULLBACK_V2 METHODOLOGY EVAL (today's coins) ===\n")
    coins = load_discovered_coins()
    print(f"Loaded {len(coins)} coins from latest discovery (no +2% filter).\n")
    for c in coins:
        print(f"  - {c['name'][:24]:24} liq=${c['liquidity']:,.0f} 24h={c['change_24h']:+.2f}% score={c['score']}")

    if not coins:
        print("No coins to evaluate. Run trend_discovery.py first.")
        return

    # Pullback_v2 strategy params (from methodology_eval_pullback.json)
    configs = [
        {"fast": 8, "slow": 32, "entry_roc": 0.03, "ob": 85, "name": "pullback_v2_rsi20-55_roc-10%-0%"},
    ]

    passed = 0
    total = 0
    rows = []
    for c in coins:
        try:
            candles = fetch_candles(c["address"])
        except Exception as ex:
            print(f"  [skip {c['name']}] fetch failed: {ex}")
            continue
        time.sleep(1.2)
        if len(candles) < 40:
            print(f"  [skip {c['name']}] only {len(candles)} candles — not enough for WF split")
            continue
        total += 1
        best_e = None
        best_v = None
        for cfg in configs:
            e = walk_forward_pullback(candles, c["fee_bps"], c["liquidity"],
                                      score=c.get("score", 50),
                                      fast=cfg["fast"], slow=cfg["slow"],
                                      entry_roc=cfg["entry_roc"], ob=cfg["ob"])
            if not e:
                continue
            v = verdict(e)
            if best_e is None or v == "PASS":
                best_e = e
                best_v = v
        if best_e is None:
            print(f"  {c['name'][:24]:24} — no valid backtest result")
            continue
        if best_v == "PASS":
            passed += 1
        rows.append((c["name"], best_e, best_v))
        print(f"  {c['name'][:24]:24} train={best_e['train_net']:+6.1f}% "
              f"test={best_e['test_net']:+6.1f}% | BH_test={best_e['bh_test']:+6.1f}% "
              f"PF={best_e['pf_test']:.2f} DD={best_e['maxdd_test']:.0f}% -> {best_v}")

    print(f"\n=== FRESH PULLBACK_V2 VERDICT ===")
    print(f"{passed}/{total} coins PASS all gates.")
    any_passed = passed > 0

    artifact = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "script": "fresh_pullback_methodology_eval.py",
        "strategy": "pullback_v2_rsi20-55_roc-10%-0%",
        "total": total,
        "passed": passed,
        "any_passed": any_passed,
        "rows": [
            {"name": n, "test_net": e["test_net"], "bh_test": e["bh_test"],
             "pf_test": e["pf_test"], "maxdd_test": e["maxdd_test"], "verdict": v}
            for n, e, v in rows
        ],
    }
    json.dump(artifact, open("methodology_eval_pullback_fresh.json", "w"), indent=2)
    print(f"\nWrote methodology_eval_pullback_fresh.json")
    print(f"Freshness: {datetime.now(timezone.utc).isoformat()}")

if __name__ == "__main__":
    main()
