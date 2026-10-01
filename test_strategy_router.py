#!/usr/bin/env python3
"""Test strategy router against live market data."""
import json
import urllib.request
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import strategy_router

# Test pools (active on Base)
POOLS = [
    ("0x5116773e18a9c7bb03ebb961b38678e45e238923", "DRB / WETH 1%"),
    ("0x7692acc1cdd771d09ebcae3663e1843b2911bec7", "STONKEX / WETH 1%"),
    ("0x01784ef301d79e4b2df3a21ad9a536d4cf09a5ce", "VVV / WETH"),
    ("0x3f0296bf652e19bca772ec3df08b32732f93014a", "VIRTUAL / WETH"),
    ("0x8901ea1cc5b1e67af720121ceb3c07291f5b875d", "KTA / WETH"),
    ("0x940181a94a35a4569e4529a3cdfb74e38fd98631", "AERO / WETH"),
]

print("=" * 70)
print("STRATEGY ROUTIER — LIVE MARKET TEST")
print("=" * 70)

signals_found = 0

for pool_addr, name in POOLS:
    url = f"https://api.geckoterminal.com/api/v2/networks/base/pools/{pool_addr}/ohlcv/hour?aggregate=1&limit=120"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        
        ohlcv = data.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
        candles = sorted([
            {"ts": r[0], "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]), "v": float(r[5])}
            for r in ohlcv
        ], key=lambda x: x["ts"])
        
        if len(candles) < 30:
            print(f"\n{name}: {len(candles)} candles (insufficient)")
            continue
        
        regime, conf, details = strategy_router.detect_regime(candles)
        sig, _ = strategy_router.get_best_signal(candles, regime)
        
        print(f"\n{name}")
        print(f"  Price: ${candles[-1]['c']:.6f}")
        print(f"  Regime: {regime} (conf={conf:.2f})")
        
        if sig:
            signals_found += 1
            print(f"  >>> SIGNAL: {sig['strategy']} (conf={sig['confidence']:.2f})")
            print(f"  >>> Reason: {sig['reason']}")
        else:
            # Show which strategies were tried
            print(f"  No signal. Strategy results:")
            for strat_name, strat_fn in [
                ("ema_crossover", strategy_router.signal_ema_crossover),
                ("rsi_meanrev", strategy_router.signal_rsi_meanrev),
                ("momentum_breakout", strategy_router.signal_momentum_breakout),
                ("volume_spike", strategy_router.signal_volume_spike),
                ("pullback", strategy_router.signal_pullback),
            ]:
                result = strat_fn(candles)
                if result:
                    print(f"    {strat_name}: conf={result['confidence']:.2f} — {result['reason'][:50]}")
                else:
                    print(f"    {strat_name}: no signal")
    except Exception as e:
        print(f"\n{name}: ERROR {e}")

print(f"\n{'=' * 70}")
print(f"Total signals found: {signals_found}/{len(POOLS)}")
print(f"{'=' * 70}")
