#!/usr/bin/env python3
"""
signal_writer.py — poll GeckoTerminal for EMA crossovers, write signals to file.

Usage: python3 signal_writer.py
"""
import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from ema_engine import EmaEngine
from pool_scanner import PoolScanner

# Pre-discovered pools for bluechips
POOLS = [
    ("0x99cf3E8bfB02c300312c53Aac5D0B082e3D5975C", "LAPTOP"),
    ("0x85f1Aa3a70fEdd1C52705c15BAEd143e675CD626", "NOCK"),
    ("0xb87d3CB5E3fAfeCd35B7C63e7c74441d8151DfB4", "MUTATIO"),
    ("0x8b27f626ab668197000BC722A1012022CAeD10E2", "MSTRc"),
    ("0x8c86e46defb25d481f6d85aeb4c4aa30fda06e5c330afb0803f5d42cda65069c", "STONKFLY"),
]


def main():
    print(f"=== Signal Writer @ {datetime.now(timezone.utc).isoformat()} ===")
    
    engine = EmaEngine()
    ps = PoolScanner()
    signals = []
    
    for pool, name in POOLS:
        try:
            result = engine.check_crossover(pool, require_activity=True)
            
            if result["crossover"] and result["direction"] == "bullish":
                signals.append({
                    "name": name,
                    "pool": pool,
                    "direction": result["direction"],
                    "price": result["price"],
                    "ts": datetime.now(timezone.utc).isoformat(),
                })
                print(f"  SIGNAL: {name} bullish @ ${result['price']:.6f}")
        except Exception as e:
            print(f"  ERROR {name}: {e}")
    
    if not signals:
        print("No signals.")
        return 0
    
    signals_file = os.path.join(HERE, "signals.json")
    with open(signals_file, "w") as f:
        json.dump(signals, f, indent=2)
    
    print(f"Wrote {len(signals)} signals.")
    return len(signals)


if __name__ == "__main__":
    main()
