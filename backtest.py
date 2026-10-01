#!/usr/bin/env python3
"""
BASE TREND BACKTEST ENGINE (v9 — ATR-based exits + unified strategy)
Proves/disproves momentum/pullback profitability on Base with realistic costs.
"""
import json, sys, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path
from indicators import sma, rsi, roc, eth_usd_price, CANDLE_LIMIT, atr
from strategy import check_entry_momentum, check_entry_pullback, atr_stop, load_strategy_params
from strategy_v2 import atr_volatility_filter, size_atr, enhanced_stats

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


# Re-exported from discovery — single source of truth for pool discovery.
from discovery import discover_trending, fetch_ohlcv


def backtest(candles, fee_bps, liquidity, score=50, trade_eth=0.0005, gas_usd=0.04,
             stop_pct=None, fast=8, slow=32, symbol=""):
    print(f"  {symbol:<26s} candles={len(candles)}", file=sys.stderr, flush=True)
    closes = [c["c"] for c in candles]
    if len(closes) < slow + 5:
        return None
    # --- provenance: record what the backtest actually used ---
    _now = datetime.now(timezone.utc)
    _eth_usd = eth_usd_price()
    _quote_source = "fallback" if _eth_usd == 2500.0 else "geckoterminal_api"
    eth_usd = _eth_usd
    actual_trade = min(trade_eth * (score / 50.0), 0.0005)
    if stop_pct is None:
        stop_pct = 0.08 if score >= 60 else 0.10
    slip_each = min(max((actual_trade * eth_usd) / liquidity * 10000 * 0.5, 1.0), 80.0)
    gas_each_bps = gas_usd / (actual_trade * eth_usd) * 10000
    enter_cost = (fee_bps + slip_each + gas_each_bps) / 10000
    exit_cost  = (fee_bps + slip_each + gas_each_bps) / 10000

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

        elif check_entry_momentum(closes[:i+1], candles[:i+1], params) or \
             (params.get("use_dip_buy") and check_entry_pullback(closes[:i+1], candles[:i+1], params)):
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
        # provenance
        "backtest_ts": _now.isoformat(),
        "eth_usd_used": _eth_usd,
        "quote_source": _quote_source,
        "liquidity_at_test": liquidity,
        "net_return_pct": net * 100, "trades": n, "wins": wins, "losses": loss,
        "win_rate": (wins / n * 100) if n else 0,
        "profit_factor": (gw / gl) if gl else (9.99 if gw else 0),
        "max_drawdown_pct": maxdd * 100,
        "cost_bps_roundtrip": (enter_cost + exit_cost) * 10000,
        "final_equity": cash, "trade_eth": actual_trade,
    }


def main():
    import json, os
    from datetime import datetime

    # Prefer the freshest live discovery record (trend_discovery.py applies the
    # live pace gate) over the stale discover_trending() call below.
    DISCOVERY_CACHE = "discovered.jsonl"
    live_pools = []
    _discovery_ts = None
    if os.path.exists(DISCOVERY_CACHE):
        try:
            with open(DISCOVERY_CACHE) as f:
                lines = [l.strip() for l in f if l.strip()]
            if lines:
                freshest = json.loads(lines[-1])
                _discovery_ts = datetime.fromisoformat(freshest.get("ts", "").replace("Z", "+00:00"))
                live_pools = freshest.get("coins", [])
                ts = freshest.get("ts", "?")
                print(f"=== DISCOVER TRENDING ETH-PAIRED BASE COINS (v9 — ATR exits) ===")
                print(f"Filters: stable pairs excluded, vol>$50k momentum regime\n")
                print(f"(live discovery from {ts} — {len(live_pools)} coins with live momentum)\n")
                for i, p in enumerate(live_pools[:30]):
                    print(f"  {i+1:2}. {p['name'][:30]:30} liq=${p['liquidity']:>11,.0f} "
                          f"fee={p['fee_bps']:.0f}bps score={p['score']}")
                print()
        except Exception as e:
            print(f"  [warn] could not read {DISCOVERY_CACHE}: {e}", file=sys.stderr)

    # Fall back to canonical discover if the cache had nothing.
    if not live_pools:
        print("=== DISCOVER TRENDING ETH-PAIRED BASE COINS (v9 — ATR exits) ===")
        print("Filters: stable pairs excluded, vol>$50k momentum regime\\n")
        _discovery_ts = datetime.now(timezone.utc)
        pools_raw = discover_trending(30)
        print(f"Found {len(pools_raw)} ETH/WETH-paired trending pools\n")
        for i, p in enumerate(pools_raw):
            print(f"  {i+1:2}. {p['name'][:30]:30} liq=${p['liquidity']:>11,.0f} fee={p['fee_bps']:.0f}bps score={p['score']}")
        print()
        live_pools = pools_raw
    pools = live_pools

    print("\\n=== BACKTEST (ATR-based trailing stop) ===")
    results = []
    skipped_low_score = 0
    skipped_stale = 0
    for p in pools:
        if p["liquidity"] < 300_000:
            continue
        # trend_discovery.py marks stale (dead 5m tape) pools with score==0 and a
        # "stale:" reason. Skip those here so we don't backtest dead momentum.
        note = p.get("live_note", "")
        if p.get("reasons", []) and p["reasons"][0].startswith("stale:"):
            skipped_stale += 1
            print(f"  [skipped stale] {p['name'][:26]:26} {note}", file=sys.stderr)
            continue
        if p.get("score", 0) < 35:
            skipped_low_score += 1
            continue
        try:
            candles = fetch_ohlcv(p["address"])
            time.sleep(2.0)
        except Exception as e:
            print(f"  {p['name'][:28]:28} OHLCV ERROR: {e}"); continue
        if len(candles) < 60:
            continue
        r = backtest(candles, p["fee_bps"], p["liquidity"],
                     score=p.get("score", 50), symbol=p["name"])
        if not r: continue
        r["discovery_ts"] = _discovery_ts.isoformat() if _discovery_ts else None
        r["name"] = p["name"]; r["liq"] = p["liquidity"]; r["score"] = p.get("score", 50)
        results.append(r)
        flag = "PROFIT" if r["net_return_pct"] > 0 else "LOSS"
        print(f"  {p['name'][:26]:26} {flag:6} net={r['net_return_pct']:+7.1f}% "
              f"trades={r['trades']:2} win={r['win_rate']:4.0f}% "
              f"PF={r['profit_factor']:.2f} DD={r['max_drawdown_pct']:.1f}% "
              f"cost={r['cost_bps_roundtrip']:.0f}bp amt={r.get('trade_eth',0.0005):.4f}ETH")

    if skipped_stale:
        print(f"\\n  (skipped {skipped_stale} stale-pool discoveries + {skipped_low_score} below score<35)")
    else:
        print(f"\\n  (skipped {skipped_low_score} coins below score<35 conviction threshold)")

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

    stats = enhanced_stats(results)
    print(f"\n=== ENHANCED METRICS ===")
    print(f"Sharpe ratio: {stats['sharpe']}")
    print(f"Sortino ratio: {stats['sortino']}")
    print(f"Calmar ratio: {stats['calmar']}")
    print(f"Max drawdown: {stats['max_dd']:.1f}%")

    print("\n=== VERDICT ===")
    # Relaxed threshold for small baskets (1-2 coins): require positive avg + profitable majority
    if len(results) <= 2:
        if avg > 0 and len(prof) >= len(results) / 2:
            print("STRATEGY IS PROFITABLE in expectation on this basket of trending Base coins.")
            print("Deploy with risk caps (max 0.0005 ETH/trade, daily loss circuit breaker).")
        else:
            print("Strategy NOT reliably profitable on this basket — tune params or hold.")
    elif basket_return > 5.0 and avg > 0 and stats["sharpe"] > 0.5:
        print("STRATEGY IS PROFITABLE in expectation on a basket of trending Base coins.")
        print("Deploy with risk caps (max 0.0005 ETH/trade, daily loss circuit breaker).")
    else:
        print("Strategy NOT reliably profitable on this basket — tune params or hold.")
    write_backtest(results)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    print(f"\nWrote {len(results)} results to {BACKTESTS_DIR / f'backtest_{ts}.json'}")


def write_backtest(results: list[dict]) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    p = BACKTESTS_DIR / f"backtest_{ts}.json"
    p.write_text(json.dumps(results, indent=2))
    return p


if __name__ == "__main__":
    main()
