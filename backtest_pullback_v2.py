#!/usr/bin/env python3
"""
PULLBACK v2 — looser entry conditions to actually capture some trades.

Uses discovery.py's canonical discover_trending + fetch_ohlcv for discovery.
Uses the unified size_atr (volatility-scaled) from strategy.py for sizing.
Trade size at live cap: 0.0005 ETH.
"""
import json, sys, time
from datetime import datetime, timezone
from pathlib import Path
from indicators import sma, rsi, roc, eth_usd_price, CANDLE_LIMIT, atr
from strategy import check_entry_pullback, atr_stop, size_atr, load_strategy_params
from strategy_v2 import atr_volatility_filter, enhanced_stats
from discovery import discover_trending, fetch_ohlcv

BACKTESTS_DIR = Path(__file__).resolve().parent / "backtests"
BACKTESTS_DIR.mkdir(parents=True, exist_ok=True)

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json;version=20230302"}


def get(url, timeout=25, retries=5):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode()
        except Exception as e:
            if "429" in str(e) and i < retries - 1:
                time.sleep(2 + i * 2); continue
            if i < retries - 1: time.sleep(1.5); continue
            raise


def backtest_pullback(candles, fee_bps, liquidity, score=50, trade_eth=0.0005, gas_usd=0.04,
                      fast=8, slow=32, overbought=85, stop_pct=None):
    """Pullback buy: close > slow MA but < fast MA (brief dip in uptrend).
    Uses ATR-based exits and volatility filter."""
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
    exit_cost  = (fee_bps + slip_each + gas_each_bps) / 10000

    pos = 0.0; entry_px = 0.0; entry_hi = 0.0; atr_val = 0.0; entry_atr = 0.0
    cash = 1.0; peak = 1.0; maxdd = 0.0
    wins = 0; loss = 0; gw = 0.0; gl = 0.0
    params = load_strategy_params()

    for i in range(len(closes)):
        px = closes[i]
        if i < slow:
            continue
        sf = sma(closes[:i+1], fast)
        ss = sma(closes[:i+1], slow)
        r = roc(closes[:i+1], fast)
        rs = rsi(closes[:i+1], 14)

        if pos > 0:
            entry_hi = max(entry_hi, px)
            dd_from_peak = (px / entry_hi - 1) if entry_hi > 0 else 0
            ret = px / entry_px - 1
            exit_reason = None
            if entry_atr > 0:
                triggered, stop_level, entry_hi = atr_stop(entry_px, px, entry_atr, params)
                if triggered:
                    exit_reason = "ATR stop"
            elif rs > overbought:
                exit_reason = "overbought"
            elif dd_from_peak < -stop_pct or ret < -stop_pct:
                exit_reason = "fixed stop"
            if exit_reason:
                before = cash
                cash = pos * (px / entry_px) * (1 - exit_cost)
                pnl = cash - before
                pos = 0.0; entry_hi = 0.0; atr_val = 0.0; entry_atr = 0.0
                if pnl > 0:
                    wins += 1; gw += pnl
                else:
                    loss += 1; gl += -pnl
                peak = max(peak, cash)
                maxdd = max(maxdd, 1 - cash / peak)

        elif (px > ss) and (px < sf) and (20 < rs < 55) and (-0.10 < r < 0.0):
            roc24 = roc(closes[:i+1], 24) if len(closes[:i+1]) > 24 else 0
            if roc24 > 0:
                if not atr_volatility_filter(candles[:i+1]):
                    continue
                plus_dm = []; minus_dm = []; trs = []
                for j in range(max(1, i - 14), i + 1):
                    h = candles[j]["h"]; l = candles[j]["l"]
                    prev_h = candles[j - 1]["h"]; prev_l = candles[j - 1]["l"]
                    prev_c = candles[j - 1]["c"]
                    tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
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
                entry_atr = atr(candles[:i+1], 14) if len(candles[:i+1]) >= 14 else 0.0
                pos = cash * (1 - enter_cost)
                entry_px = px; entry_hi = px

    if pos > 0:
        before = cash
        cash = pos * (closes[-1] / entry_px) * (1 - exit_cost)
        pnl = cash - before
        if pnl > 0:
            wins += 1; gw += pnl
        else:
            loss += 1; gl += -pnl

    n = wins + loss
    net = cash - 1.0
    return {
        "net_return_pct": net * 100, "trades": n,
        "wins": wins, "losses": loss,
        "win_rate": (wins / n * 100) if n else 0,
        "profit_factor": (gw / gl) if gl else (9.99 if gw else 0),
        "max_drawdown_pct": maxdd * 100,
        "cost_bps_roundtrip": (enter_cost + exit_cost) * 10000,
        "final_equity": cash, "trade_eth": actual_trade,
    }


def main():
    print("=== PULLBACK v2 (looser RSI 20-55, ROC -10% to 0%) ===\n")
    pools = discover_trending(30)
    print(f"Found {len(pools)} ETH/WETH-paired trending pools\n")

    results = []
    skipped_low_score = 0
    for p in pools:
        if p["liquidity"] < 300_000:
            continue
        if p.get("score", 0) < 35:
            skipped_low_score += 1; continue
        try:
            candles = fetch_ohlcv(p["address"], "hour", CANDLE_LIMIT)
            time.sleep(2.0)
        except Exception as e:
            print(f"  {p['name'][:28]:28} OHLCV ERROR: {e}"); continue
        if len(candles) < 60:
            continue
        r = backtest_pullback(candles, p["fee_bps"], p["liquidity"], score=p.get("score", 50))
        if not r:
            continue
        r["name"] = p["name"]; r["liq"] = p["liquidity"]; r["score"] = p.get("score", 50)
        results.append(r)
        flag = "PROFIT" if r["net_return_pct"] > 0 else "LOSS"
        print(f"  {p['name'][:26]:26} {flag:6} net={r['net_return_pct']:+7.1f}% "
              f"trades={r['trades']:2} win={r['win_rate']:4.0f}% "
              f"PF={r['profit_factor']:.2f} DD={r['max_drawdown_pct']:.1f}%")

    print(f"\n(skipped {skipped_low_score} coins below score<35)")
    if not results:
        print("NO BACKTESTABLE POOLS"); sys.exit(1)

    prof = [r for r in results if r["net_return_pct"] > 0]
    print(f"\n=== BASKET ({len(results)} coins) ===")
    print(f"Profitable: {len(prof)}/{len(results)} ({len(prof)/len(results)*100:.0f}%)")
    avg = sum(r['net_return_pct'] for r in results) / len(results)
    print(f"Avg per-coin: {avg:+.1f}%")
    if results:
        best = max(results, key=lambda x: x['net_return_pct'])
        worst = min(results, key=lambda x: x['net_return_pct'])
        print(f"Best:  {best['name']} {best['net_return_pct']:+.1f}%")
        print(f"Worst: {worst['name']} {worst['net_return_pct']:+.1f}%")

    stats = enhanced_stats(results)
    print(f"\n=== ENHANCED METRICS ===")
    print(f"Sharpe ratio: {stats['sharpe']}")
    print(f"Sortino ratio: {stats['sortino']}")
    print(f"Calmar ratio: {stats['calmar']}")
    print(f"Max drawdown: {stats['max_dd']:.1f}%")

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    out_path = BACKTESTS_DIR / f"pullback_v2_{ts}.json"
    out_path.write_text(json.dumps(results, indent=2))
    print(f"\nWrote {len(results)} results to {out_path}")


if __name__ == "__main__":
    main()
