#!/usr/bin/env python3
"""
signal_executor.py — read signals.json and execute trades.

Called by auto_trade.py when signals are detected.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--signals", default="signals.json")
    args = ap.parse_args()
    
    if not os.path.exists(args.signals):
        print("No signals file.")
        return 0
    
    with open(args.signals) as f:
        signals = json.load(f)
    
    if not signals:
        print("No signals.")
        return 0
    
    print(f"Found {len(signals)} signal(s).")
    
    import executor as exec_module
    
    caps = exec_module.load_caps()
    if caps.get("kill_switch") or not caps.get("live_trading"):
        print("Not armed.")
        return 0
    
    executed = 0
    
    for signal in signals:
        name = signal.get("name", "?")
        token = signal.get("token")
        
        print(f"\n--- {name} ---")
        
        holdings = exec_module._load_holdings()
        if token in holdings:
            print(f"SKIP: already held")
            continue
        
        coin = {
            "token_addr": token,
            "address": signal.get("pool"),
            "name": name,
            "source": "auto_trade",
            "liquidity": 50000,
            "vol_24h": 10000,
            "score": 60,
        }
        
        amt = 0.0005
        prop = exec_module.build_swap(coin, amt, token)
        allowed, reason = exec_module.evaluate(prop)
        
        if not allowed:
            print(f"BLOCKED: {reason}")
            continue
        
        try:
            result = exec_module.execute_swap(token, amt, 100)
            if result.get("hash"):
                print(f"SUCCESS: {result['hash'][:20]}...")
                executed += 1
            else:
                print(f"FAILED: {result}")
        except Exception as e:
            print(f"ERROR: {e}")
    
    print(f"\nExecuted {executed}/{len(signals)}")
    
    # Clear signals
    with open(args.signals, "w") as f:
        json.dump([], f)
    
    return executed


if __name__ == "__main__":
    sys.exit(main())
