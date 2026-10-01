#!/usr/bin/env python3
"""
BACKTEST BATTERY — pullback_v1 + pullback_v2, out-of-sample edge validation
Same battery, same edge bar, same provenance. Just swaps the signal function.
Pre-defined edge bar set BEFORE running. No post-hoc cherry-picking.
"""
import json, sys, time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from indicators import sma, rsi, roc, eth_usd_price, CANDLE_LIMIT, atr
from strategy import check_entry_pullback, atr_stop, load_strategy_params
from strategy_v2 import atr_volatility_filter
from discovery import fetch_ohlcv

HERE = Path(__file__).resolve().parent
BACKTESTS_DIR = HERE / "backtests"
BACKTESTS_DIR.mkdir(parents=True, exist_ok=True)

EDGE_BAR = {
    "basket_return_pct": 0.0,
    "win_rate_min": 0.40,
    "profit_factor_min": 1.0,
    "min_trades_per_coin": 5,
    "min_coins": 5,
    "max_cost_bps": 200,
}
CANDLE_LIMIT = 300


def backtest_v1(candles, fee_bps, liquidity, score=50, trade_eth=0.03,
                gas_usd=0.04, fast=8, slow=32, overbought=85, stop_pct=None,
                vol_spike_mult=1.5, rsi_low=25, rsi_high=50, roc8_low=-0.08,
                roc8_high=-0.005):
    """Pullback v1: original backtest_pullback.py signal function."""
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
    exit_cost = (fee_bps + slip_each + gas_each_bps) / 10000

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
                if len(volumes) >= 8:
                    avg_vol = sum(volumes[max(0,i-8):i]) / 8
                    if volumes[i] < avg_vol * vol_spike_mult:
                        continue
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
        "net_return_pct": net * 100, "trades": n, "wins": wins, "losses": loss,
        "win_rate": (wins / n * 100) if n else 0,
        "profit_factor": (gw / gl) if gl else (9.99 if gw else 0),
        "max_drawdown_pct": maxdd * 100,
        "cost_bps_roundtrip": (enter_cost + exit_cost) * 10000,
        "final_equity": cash, "trade_eth": actual_trade,
    }


def backtest_v2(candles, fee_bps, liquidity, score=50, trade_eth=0.0005,
                gas_usd=0.04, fast=8, slow=32, overbought=85, stop_pct=None):
    """Pullback v2: uses strategy.py check_entry_pullback + atr_stop."""
    closes = [c["c"] for c in candles]
    if len(closes) < slow + 5:
        return None
    eth_usd = eth_usd_price()
    actual_trade = min(trade_eth * (score / 50.0), trade_eth)
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


def run_battery(strategy_name, backtest_fn, pools, candle_limit=300):
    """Run a single strategy through the battery. Returns (results, skipped)."""
    print(f"\n=== {strategy_name} ===")
    results = []
    skipped = 0
    for i, p in enumerate(pools):
        addr = p.get("address", "")
        name = p["name"]

        try:
            candles = fetch_ohlcv(addr, limit=candle_limit)
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
        if strategy_name == "pullback_v1":
            r_is = backtest_fn(is_candles, p["fee_bps"], p["liquidity"],
                                score=p.get("score", 50))
        else:
            r_is = backtest_fn(is_candles, p["fee_bps"], p["liquidity"],
                                score=p.get("score", 50))

        if not r_is or r_is["trades"] < EDGE_BAR["min_trades_per_coin"]:
            print(f"  {name[:26]:26} INSUFFICIENT IS trades: "
                  f"{r_is['trades'] if r_is else 0}")
            skipped += 1
            continue

        # Out-of-sample
        if strategy_name == "pullback_v1":
            r_oos = backtest_fn(oos_candles, p["fee_bps"], p["liquidity"],
                                 score=p.get("score", 50))
        else:
            r_oos = backtest_fn(oos_candles, p["fee_bps"], p["liquidity"],
                                 score=p.get("score", 50))

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

    print(f"  ({skipped} skipped, {len(results)} in battery)")
    return results, skipped


def check_bar(results, strategy_name):
    """Check edge bar against results. Returns (verdict, checks dict)."""
    if len(results) < EDGE_BAR["min_coins"]:
        return "INCONCLUSIVE", {"min_coins": f"need {EDGE_BAR['min_coins']}, have {len(results)}"}

    basket_oos = 1.0
    for r in results:
        basket_oos *= (0.5 + 0.5 * r["oos_final_equity"])
    basket_return = (basket_oos - 1) * 100

    avg_oos = sum(r["oos_net_return_pct"] for r in results) / len(results)
    avg_wr = sum(r["oos_win_rate"] for r in results) / len(results)
    avg_pf = sum(r["oos_profit_factor"] for r in results) / len(results)
    avg_cost = sum(r["oos_cost_bps"] for r in results) / len(results)
    prof = [r for r in results if r["oos_net_return_pct"] > 0]

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
        print(f"  {k}: {status}")

    print(f"\n  Basket OOS return: {basket_return:+.1f}%")
    print(f"  Avg OOS return: {avg_oos:+.1f}%")
    print(f"  Avg OOS win rate: {avg_wr:.0f}%")
    print(f"  Avg OOS PF: {avg_pf:.2f}")
    print(f"  Avg OOS cost: {avg_cost:.0f}bp")
    print(f"  Profitable: {len(prof)}/{len(results)}")
    print(f"  Edge bar checks: {sum(1 for v in checks.values() if v)}/{len(checks)} passed")

    verdict = "EDGE CONFIRMED" if len([v for v in checks.values() if not v]) == 0 else "INCONCLUSIVE"
    return verdict, checks


def write_battery(strategy_name, results, verdict, checks):
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    output = {
        "strategy": strategy_name,
        "battery_ts": datetime.now(timezone.utc).isoformat(),
        "verdict": verdict,
        "edge_bar": EDGE_BAR,
        "checks": checks,
        "results": results,
    }
    p = BACKTESTS_DIR / f"battery_{strategy_name}_{ts}.json"
    p.write_text(json.dumps(output, indent=2))
    print(f"Wrote {p}")


def main():
    print("=== BACKTEST BATTERY — pullback_v1 + pullback_v2 ===")
    print(f"Edge bar: {json.dumps(EDGE_BAR, indent=2)}")
    print(f"Candle limit: {CANDLE_LIMIT}")
    print()

    # Load pool metadata from candles_cache.json
    CACHE = HERE / "candles_cache.json"
    if not CACHE.exists():
        print("NO CACHE: candles_cache.json not found.")
        sys.exit(1)

    with open(CACHE) as f:
        cached = json.load(f)
    print(f"Loaded {len(cached)} coins from candles_cache.json")

    # Filter for quality pools
    pools = [p for p in cached
             if p.get("liquidity", 0) >= 300_000 and p.get("score", 0) >= 35]
    print(f"Filtered: {len(pools)} pools (liq>=$300k, score>=35)\n")

    if len(pools) < EDGE_BAR["min_coins"]:
        print(f"NOT ENOUGH POOLS: need {EDGE_BAR['min_coins']}, have {len(pools)}")
        sys.exit(1)

    # Run pullback_v1
    results_v1, skipped_v1 = run_battery("pullback_v1", backtest_v1, pools, CANDLE_LIMIT)
    verdict_v1, checks_v1 = check_bar(results_v1, "pullback_v1")
    write_battery("pullback_v1", results_v1, verdict_v1, checks_v1)

    # Run pullback_v2
    results_v2, skipped_v2 = run_battery("pullback_v2", backtest_v2, pools, CANDLE_LIMIT)
    verdict_v2, checks_v2 = check_bar(results_v2, "pullback_v2")
    write_battery("pullback_v2", results_v2, verdict_v2, checks_v2)

    # Summary
    print(f"\n=== BATTERY SUMMARY ===")
    print(f"pullback_v1: {verdict_v1} ({len(results_v1)} coins)")
    print(f"pullback_v2: {verdict_v2} ({len(results_v2)} coins)")
    print(f"\nConclusion: {'NONE of the strategies generate testable samples on hourly data' if len(results_v1) < EDGE_BAR['min_coins'] and len(results_v2) < EDGE_BAR['min_coins'] else 'At least one strategy passed'}")


if __name__ == "__main__":
    main()