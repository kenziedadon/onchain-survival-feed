#!/usr/bin/env python3
"""
BACKTEST BATTERY — out-of-sample edge validation for simple_ema_v1
Pre-defined edge bar set BEFORE running. No post-hoc cherry-picking.
Paper-side, zero safety risk. Fetches 300+ candles per coin from API.
"""
import json, sys, time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from indicators import sma, rsi, roc, eth_usd_price, CANDLE_LIMIT, atr
from strategy import check_entry_momentum, check_entry_pullback, atr_stop, load_strategy_params
from strategy_v2 import atr_volatility_filter
from discovery import fetch_ohlcv

HERE = Path(__file__).resolve().parent
BACKTESTS_DIR = HERE / "backtests"
BACKTESTS_DIR.mkdir(parents=True, exist_ok=True)

# --- PRE-DEFINED EDGE BAR (set before running, not after) ---
EDGE_BAR = {
    "basket_return_pct": 0.0,
    "win_rate_min": 0.40,
    "profit_factor_min": 1.0,
    "min_trades_per_coin": 5,
    "min_coins": 5,
    "max_cost_bps": 200,
}

CANDLE_LIMIT = 300  # weeks of data for validation


def backtest(candles, fee_bps, liquidity, score=50, trade_eth=0.0005, gas_usd=0.04,
             stop_pct=None, fast=9, slow=21, symbol=""):
    closes = [c["c"] for c in candles]
    if len(closes) < slow + 5:
        return None
    eth_usd = eth_usd_price()
    actual_trade = min(trade_eth * (score / 50.0), 0.0005)
    if stop_pct is None:
        stop_pct = 0.08 if score >= 60 else 0.10
    slip_each = min(max((actual_trade * eth_usd) / liquidity * 10000 * 0.5, 1.0), 80.0)
    gas_each_bps = gas_usd / (actual_trade * eth_usd) * 10000
    enter_cost = (fee_bps + slip_each + gas_each_bps) / 10000
    exit_cost = (fee_bps + slip_each + gas_each_bps) / 10000

    pos = 0.0; entry_px = 0.0; entry_hi = 0.0; atr_val = 0.0
    cash = 1.0; peak = 1.0; maxdd = 0.0
    wins = 0; loss = 0; gw = 0.0; gl = 0.0
    params = load_strategy_params()

    for i in range(len(closes)):
        px = closes[i]
        if i < slow: continue
        sf = sma(closes[:i+1], fast); ss = sma(closes[:i+1], slow)
        r = roc(closes[:i+1], fast); rs = rsi(closes[:i+1], 14)
        if pos > 0:
            entry_hi = max(entry_hi, px)
            dd_from_peak = (px / entry_hi - 1) if entry_hi > 0 else 0
            ret = px / entry_px - 1
            exit_reason = None
            if atr_val > 0:
                triggered, stop_level, entry_hi = atr_stop(entry_px, px, atr_val, params)
                if triggered:
                    exit_reason = "ATR stop"
            elif rs > params.get("overbought", 85):
                exit_reason = "overbought"
            elif dd_from_peak < -stop_pct or ret < -stop_pct:
                exit_reason = "fixed stop"
            if exit_reason:
                before = cash
                cash = pos * (px / entry_px) * (1 - exit_cost)
                pnl = cash - before
                pos = 0.0; entry_hi = 0.0; atr_val = 0.0
                if pnl > 0: wins += 1; gw += pnl
                else: loss += 1; gl += -pnl
                peak = max(peak, cash); maxdd = max(maxdd, 1 - cash / peak)
        elif check_entry_pullback(closes[:i+1], candles[:i+1], params):
            if len(candles[:i+1]) >= 14:
                atr_val = atr(candles[:i+1], 14)
                if not atr_volatility_filter(candles[:i+1]):
                    continue
            else:
                atr_val = 0.0
            pos = cash * (1 - enter_cost)
            entry_px = px; entry_hi = px

    if pos > 0:
        before = cash
        cash = pos * (closes[-1] / entry_px) * (1 - exit_cost)
        pnl = cash - before
        if pnl > 0: wins += 1; gw += pnl
        else: loss += 1; gl += -pnl

    n = wins + loss
    net = cash - 1.0
    return {
        "net_return_pct": net * 100, "trades": n, "wins": wins, "losses": loss,
        "win_rate": (wins / n * 100) if n else 0,
        "profit_factor": (gw / gl) if gl else (9.99 if gw else 0),
        "max_drawdown_pct": maxdd * 100,
        "cost_bps_roundtrip": (enter_cost + exit_cost) * 10000,
        "final_equity": cash, "trade_eth": actual_trade,
    }


def split_candles(candles, split_ratio=0.7):
    n = len(candles)
    split_idx = int(n * split_ratio)
    return candles[:split_idx], candles[split_idx:]


def main():
    print("=== BACKTEST BATTERY — out-of-sample edge validation ===")
    print(f"Edge bar: {json.dumps(EDGE_BAR, indent=2)}")
    print(f"Candle limit: {CANDLE_LIMIT} (300+ for validation)")
    print()

    # 1. Load from candles_cache.json (cached pool metadata, no API for candles)
    CACHE = HERE / "candles_cache.json"
    if not CACHE.exists():
        print("NO CACHE: candles_cache.json not found. Cannot run battery.")
        sys.exit(1)

    with open(CACHE) as f:
        cached = json.load(f)
    print(f"Loaded {len(cached)} coins from candles_cache.json (metadata)")

    # 2. Filter for quality pools (match backtest.py thresholds)
    pools = [p for p in cached
             if p.get("liquidity", 0) >= 300_000 and p.get("score", 0) >= 35]
    print(f"Filtered: {len(pools)} pools (liq>=$300k, score>=35)\n")

    if len(pools) < EDGE_BAR["min_coins"]:
        print(f"NOT ENOUGH POOLS: need {EDGE_BAR['min_coins']}, have {len(pools)}")
        print("Battery cannot run — insufficient candidates.")
        sys.exit(1)

    # 3. Fetch OHLCV for each pool (300+ candles), split 70/30 OOS
    results = []
    skipped = 0
    for i, p in enumerate(pools):
        addr = p.get("address", "")
        name = p["name"]
        
        # Fetch fresh OHLCV from API (300+ candles)
        try:
            candles = fetch_ohlcv(addr, limit=CANDLE_LIMIT)
            time.sleep(1.5)
        except Exception as e:
            print(f"  {name[:26]:26} OHLCV ERROR: {e}")
            skipped += 1
            continue

        if len(candles) < 100:
            print(f"  {name[:26]:26} too few candles: {len(candles)}")
            skipped += 1
            continue

        is_candles, oos_candles = split_candles(candles, 0.7)

        # In-sample
        r_is = backtest(is_candles, p["fee_bps"], p["liquidity"],
                        score=p.get("score", 50), symbol=name)
        if not r_is or r_is["trades"] < EDGE_BAR["min_trades_per_coin"]:
            print(f"  {name[:26]:26} INSUFFICIENT IS trades: "
                  f"{r_is['trades'] if r_is else 0}")
            skipped += 1
            continue

        # Out-of-sample (same strategy params, same stop logic)
        r_oos = backtest(oos_candles, p["fee_bps"], p["liquidity"],
                         score=p.get("score", 50), symbol=name)
        if not r_oos or r_oos["trades"] < EDGE_BAR["min_trades_per_coin"]:
            print(f"  {name[:26]:26} INSUFFICIENT OOS trades: "
                  f"{r_oos['trades'] if r_oos else 0}")
            skipped += 1
            continue

        result = {
            "name": name,
            "liq": p["liquidity"],
            "score": p.get("score", 50),
            "is_net_return_pct": r_is["net_return_pct"],
            "is_trades": r_is["trades"],
            "is_win_rate": r_is["win_rate"],
            "is_profit_factor": r_is["profit_factor"],
            "is_cost_bps": r_is["cost_bps_roundtrip"],
            "oos_net_return_pct": r_oos["net_return_pct"],
            "oos_trades": r_oos["trades"],
            "oos_win_rate": r_oos["win_rate"],
            "oos_profit_factor": r_oos["profit_factor"],
            "oos_cost_bps": r_oos["cost_bps_roundtrip"],
            "oos_final_equity": r_oos["final_equity"],
            "oos_trade_eth": r_oos["trade_eth"],
        }
        results.append(result)
        flag = "PASS" if r_oos["net_return_pct"] > 0 else "FAIL"
        print(f"  {name[:26]:26} IS={r_is['net_return_pct']:+6.1f}% "
              f"OOS={r_oos['net_return_pct']:+6.1f}% {flag}")

    print(f"\n  ({skipped} skipped, {len(results)} in battery)")

    if len(results) < EDGE_BAR["min_coins"]:
        print(f"\nBATTERY FAILED EDGE BAR: need {EDGE_BAR['min_coins']} coins, "
              f"have {len(results)}")
        write_battery(results, "FAIL_INSUFFICIENT_COINS")
        sys.exit(0)

    # 4. Check edge bar
    basket_oos = 1.0
    for r in results:
        basket_oos *= (0.5 + 0.5 * r["oos_final_equity"])
    basket_return = (basket_oos - 1) * 100

    avg_oos = sum(r["oos_net_return_pct"] for r in results) / len(results)
    avg_wr = sum(r["oos_win_rate"] for r in results) / len(results)
    avg_pf = sum(r["oos_profit_factor"] for r in results) / len(results)
    avg_cost = sum(r["oos_cost_bps"] for r in results) / len(results)
    prof = [r for r in results if r["oos_net_return_pct"] > 0]

    passes = []
    fails = []
    checks = {
        "basket_return_pct": basket_return > EDGE_BAR["basket_return_pct"],
        "win_rate_min": avg_wr / 100 > EDGE_BAR["win_rate_min"],
        "profit_factor_min": avg_pf > EDGE_BAR["profit_factor_min"],
        "min_trades_per_coin": all(
            r["oos_trades"] >= EDGE_BAR["min_trades_per_coin"] for r in results),
        "min_coins": len(results) >= EDGE_BAR["min_coins"],
        "max_cost_bps": avg_cost <= EDGE_BAR["max_cost_bps"],
    }

    for k, v in checks.items():
        status = "PASS" if v else "FAIL"
        (passes if v else fails).append(k)
        print(f"  {k}: {status}")

    print(f"\n=== BATTERY SUMMARY ({len(results)} coins, OOS) ===")
    print(f"Basket OOS return: {basket_return:+.1f}%")
    print(f"Avg OOS return: {avg_oos:+.1f}%")
    print(f"Avg OOS win rate: {avg_wr:.0f}%")
    print(f"Avg OOS PF: {avg_pf:.2f}")
    print(f"Avg OOS cost: {avg_cost:.0f}bp")
    print(f"Profitable: {len(prof)}/{len(results)}")
    print(f"Edge bar checks: {len(passes)}/{len(checks)} passed")

    verdict = "EDGE CONFIRMED" if len(fails) == 0 else "NO EDGE — battery failed bar"
    print(f"\n=== VERDICT: {verdict} ===")

    write_battery(results, verdict, checks)


def write_battery(results, verdict, checks=None):
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    output = {
        "battery_ts": datetime.now(timezone.utc).isoformat(),
        "verdict": verdict,
        "edge_bar": EDGE_BAR,
        "checks": checks or {},
        "results": results,
    }
    p = BACKTESTS_DIR / f"battery_{ts}.json"
    p.write_text(json.dumps(output, indent=2))
    print(f"Wrote battery results to {p}")


if __name__ == "__main__":
    main()
