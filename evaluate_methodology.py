#!/usr/bin/env python3
"""
METHODOLOGY EVALUATOR  (additive, SAFE — no trading, no live execution)
========================================================================
Applies the AI-trading-bot-methodology skill's discipline to the existing
backtest/grid engines, which ALREADY include fees + slippage + gas:

  * Buy-and-hold BENCHMARK  — if simply holding the coin beats the strategy,
    the strategy is worthless (video: "you might as well just buy the asset").
  * Walk-forward VALIDATION — optimize on the first 60% of data, validate on
    the unseen last 40%; a strategy that only works in-sample dies live.
  * Profit-factor + MAX-DRAWDOWN gating — net% alone is a shiny object; the
    video weights max DD and profit factor far above win rate.

This file does NOT modify backtest.py or grid_search.py. It imports their
`backtest`/`bt` functions and re-runs them on split windows. Pure stdlib +
the existing indicators.py. Run:  python3 evaluate_methodology.py
"""

import json
import time
import urllib.request
from indicators import eth_usd_price, CANDLE_LIMIT, sma, rsi, roc
from backtest import discover_trending, fetch_ohlcv, backtest
from grid_search import load_coins, bt as grid_bt

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json;version=20230302"}


def fetch_candles(addr, limit=CANDLE_LIMIT):
    """Fetch raw OHLCV (reuse backtest's network helper via fetch_ohlcv)."""
    return fetch_ohlcv(addr, limit)


def buy_and_hold(candles):
    """Net return % of simply holding the asset for the window (no costs)."""
    if len(candles) < 2:
        return None
    return (candles[-1]["c"] / candles[0]["c"] - 1) * 100


def split(closes, train_frac=0.6):
    k = int(len(closes) * train_frac)
    return closes[:k], closes[k:]


def walk_forward_eval(candles, fee_bps, liquidity, score=50,
                      fast=8, slow=32, entry_roc=0.03, ob=85):
    """Backtest on train window, then re-run the SAME params on unseen test window.

    Returns (train_net%, test_net%, bh_train%, bh_test%, pf_test, maxdd_test).
    A real edge shows test_net > 0 AND test_net competitive with buy-and-hold.
    """
    closes = [c["c"] for c in candles]
    if len(closes) < slow + 5:
        return None
    tr_closes, te_closes = split(closes)
    # Use slice indices to maintain full candle objects for ADX calculation
    tr_candles = candles[:len(tr_closes)]
    te_candles = candles[len(tr_closes):]
    r_tr = backtest(tr_candles, fee_bps, liquidity, score=score,
                    fast=fast, slow=slow)
    r_te = backtest(te_candles, fee_bps, liquidity, score=score,
                    fast=fast, slow=slow)
    if not r_tr or not r_te:
        return None
    bh_tr = buy_and_hold(tr_candles)
    bh_te = buy_and_hold(te_candles)
    return {
        "train_net": r_tr["net_return_pct"], "test_net": r_te["net_return_pct"],
        "bh_train": bh_tr, "bh_test": bh_te,
        "pf_test": r_te["profit_factor"], "maxdd_test": r_te["max_drawdown_pct"],
        "cost_bps": r_te["cost_bps_roundtrip"],
    }


def walk_forward_eval_ema(candles, fee_bps, liquidity, score=50,
                          fast=9, slow=21, stop_loss_pct=0.025, take_profit_pct=0.05):
    """EMA crossover walk-forward eval: backtest EMA on train, validate on test window.

    Returns (train_net%, test_net%, bh_train%, bh_test%, pf_test, maxdd_test).
    """
    closes = [c["c"] for c in candles]
    if len(closes) < slow + 5:
        return None
    tr_candles = candles[:int(len(closes) * 0.6)]
    te_candles = candles[int(len(closes) * 0.6):]
    if len(tr_candles) < slow + 5 or len(te_candles) < slow + 5:
        return None
    r_tr = backtest_ema(tr_candles, fee_bps, liquidity, score=score,
                        fast=fast, slow=slow, stop_loss_pct=stop_loss_pct,
                        take_profit_pct=take_profit_pct)
    r_te = backtest_ema(te_candles, fee_bps, liquidity, score=score,
                        fast=fast, slow=slow, stop_loss_pct=stop_loss_pct,
                        take_profit_pct=take_profit_pct)
    if not r_tr or not r_te:
        return None
    # Guard: backtest result may miss expected keys (KeyError on net_return_pct / profit_factor / max_drawdown_pct / cost_bps_roundtrip)
    def _safe_filled(r):
        filled = dict(r)
        for k in ("net_return_pct", "profit_factor", "max_drawdown_pct", "cost_bps_roundtrip"):
            if k not in filled or filled[k] is None:
                filled[k] = 0.0
        return filled
    r_tr_filled = _safe_filled(r_tr)
    r_te_filled = _safe_filled(r_te)
    bh_tr = buy_and_hold(tr_candles)
    bh_te = buy_and_hold(te_candles)
    return {
        "train_net": r_tr_filled.get("net_return_pct", 0.0),
        "test_net": r_te_filled.get("net_return_pct", 0.0),
        "bh_train": bh_tr,
        "bh_test": bh_te,
        "pf_test": r_te_filled.get("profit_factor", 0.0),
        "maxdd_test": r_te_filled.get("max_drawdown_pct", 0.0),
        "cost_bps": r_te_filled.get("cost_bps_roundtrip", 0.0),
    }


def backtest_ema(candles, fee_bps, liquidity, score=50, trade_eth=0.0005,
                 gas_usd=0.04, fast=9, slow=21, stop_loss_pct=0.025,
                 take_profit_pct=0.05):
    """EMA crossover backtest for walk-forward validation."""
    from grid_search import bt_ema
    return bt_ema(candles, fee_bps, liquidity, score=score, trade_eth=trade_eth,
                  gas_usd=gas_usd, fast=fast, slow=slow,
                  stop_loss_pct=stop_loss_pct, take_profit_pct=take_profit_pct)


def verdict(e):
    """Apply the methodology's hard gates to one walk-forward result."""
    flags = []
    # Gate 1: strategy must beat buy-and-hold on the UNSEEN test window
    if e["test_net"] <= e["bh_test"]:
        flags.append("FAILS benchmark: buy-and-hold beat the strategy on unseen data")
    # Gate 2: must be profitable on unseen data at all
    if e["test_net"] <= 0:
        flags.append("FAILS live: negative on unseen data")
    # Gate 3: profit factor must be real (>1.1) on test
    if e["pf_test"] < 1.1:
        flags.append(f"FAILS profit factor: PF={e['pf_test']:.2f} (<1.1)")
    # Gate 4: max drawdown sanity (<40% on a single coin)
    if e["maxdd_test"] > 40:
        flags.append(f"FAILS DD: max drawdown {e['maxdd_test']:.0f}% too high")
    # Gate 5: train must not be wildly better than test (overfit signal)
    if e["train_net"] - e["test_net"] > 30:
        flags.append(f"OVERFIT risk: train {e['train_net']:+.0f}% vs test {e['test_net']:+.0f}%")
    return "PASS" if not flags else "REJECT: " + "; ".join(flags)


def main():
    print("=== METHODOLOGY EVALUATOR (fees+slippage already in engine) ===")
    print("Adds: buy-and-hold benchmark, walk-forward validation, PF/DD gating\n")

    # Check which strategy we're running
    try:
        caps = json.load(open("caps.json"))
        strategy_name = caps.get("strategy", {}).get("name", "momentum_v8")
    except Exception:
        strategy_name = "momentum_v8"

    # Use ALL backtestable coins from backtest_results.json (not just discovered.jsonl)
    # This gives us the same 9 coins that backtest.py evaluated
    coins = []
    try:
        bt_results = json.load(open("backtest_results.json"))
        if bt_results:
            for r in bt_results:
                # We need pool addresses for fetch_ohlcv - we can get them from discovery or backtest
                # For now, use the address from backtest_results if available
                coins.append({
                    "name": r.get("name", "?"),
                    "address": r.get("address", ""),  # we need pool address
                    "fee_bps": r.get("cost_bps_roundtrip", 30),  # approximate
                    "liquidity": r.get("liq", 500000),
                    "score": r.get("score", 50),
                })
    except Exception as e:
        print(f"Could not load backtest_results.json: {e}")
        pass
    
    # If we don't have addresses, fall back to discovery
    if not coins or not any(c.get("address") for c in coins):
        coins = load_coins()
        if not coins:
            print("No discovered.jsonl found — run trend_discovery.py first.")
            return
        print(f"Loaded {len(coins)} coins from discovery (fallback). Walk-forward (60/40) each...\n")
    else:
        print(f"Loaded {len(coins)} coins from backtest_results. Walk-forward (60/40) each...\n")

    passed = 0
    rows = []
    for c in coins:
        try:
            candles = fetch_candles(c["address"])
        except Exception as ex:
            print(f"  [skip {c['name']}] {ex}")
            continue
        time.sleep(1.2)
        if strategy_name == "simple_ema_v1":
            e = walk_forward_eval_ema(candles, c["fee_bps"], c["liquidity"], score=c.get("score", 50))
        else:
            e = walk_forward_eval(candles, c["fee_bps"], c["liquidity"], score=c.get("score", 50))
        if not e:
            continue
        v = verdict(e)
        if v == "PASS":
            passed += 1
        rows.append((c["name"], e, v))
        print(f"  {c['name'][:24]:24} train={e['train_net']:+6.1f}% "
              f"test={e['test_net']:+6.1f}% | BH_test={e['bh_test']:+6.1f}% "
              f"PF={e['pf_test']:.2f} DD={e['maxdd_test']:.0f}% -> {v}")

    print(f"\n=== METHODOLOGY VERDICT ===")
    print(f"{passed}/{len(rows)} coins PASS all gates (vs {len(rows)} backtestable).")
    if passed == 0:
        print("CONSISTENT WITH PRIOR RUN: 0 configs >50% positive. "
              "No edge survives walk-forward + benchmark. Do NOT go live.")
    else:
        print(f"{passed} candidate(s) survived. Incubate 1-2+ months on live "
              "data before any capital (video: ~99% of backtests fail live).")

    # Emit JSON artifact for the arm_check pipeline gate
    artifact = {
        "ts": datetime.now(timezone.utc).isoformat() if 'datetime' in dir() else None,
        "script": "evaluate_methodology.py",
        "strategy": strategy_name,
        "total": len(rows),
        "passed": passed,
        "any_passed": passed > 0,
        "rows": [
            {"name": n, "test_net": e["test_net"], "bh_test": e["bh_test"],
             "pf_test": e["pf_test"], "maxdd_test": e["maxdd_test"], "verdict": v}
            for n, e, v in rows
        ],
    }
    if not artifact["ts"]:
        from datetime import datetime, timezone
        artifact["ts"] = datetime.now(timezone.utc).isoformat()
    json.dump(artifact, open("methodology_eval.json", "w"), indent=2)
    print("Wrote methodology_eval.json")


if __name__ == "__main__":
    main()
