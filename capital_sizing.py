#!/usr/bin/env python3
"""Show current wallet balance and capital-aware trade sizing."""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from risk import _live_balance, capital_band, load_caps

def main():
    bal = _live_balance()
    if bal is None:
        print("ERROR: Could not read wallet balance")
        return
    
    caps = load_caps()
    band_min, band_max = capital_band(bal)
    
    # Get ETH price
    import urllib.request
    UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json;version=20230302"}
    try:
        r = urllib.request.Request("https://api.geckoterminal.com/api/v2/networks/base/pools?page=1", headers=UA)
        with urllib.request.urlopen(r) as resp:
            data = json.loads(resp.read())
            for p in data["data"]:
                a = p.get("attributes", {})
                name = a.get("name", "")
                if "WETH" in name.upper() and "USDC" in name.upper():
                    eth_usd = float(a.get("base_token_price_usd", 0))
                    break
    except Exception:
        eth_usd = 0
    
    print("=== CAPITAL-AWARE SIZING ===\n")
    print(f"Balance: {bal:.6f} ETH (${bal * eth_usd:.2f} USD)")
    print(f"Capital band: {band_min:.6f} - {band_max:.6f} ETH")
    print(f"Max single trade: {band_max:.6f} ETH (${band_max * eth_usd:.2f} USD)")
    print(f"Min trade: {band_min:.6f} ETH (${band_min * eth_usd:.4f} USD)")
    
    # Show what happens at different balance levels
    print("\n=== SIZING AT DIFFERENT BALANCES ===")
    test_balances = [0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 10.0]
    print(f"{'Balance':>10} {'Min Trade':>12} {'Max Trade':>12} {'Max $':>10}")
    print("-" * 50)
    for b in test_balances:
        bmin, bmax = capital_band(b)
        print(f"{b:>10.4f} {bmin:>12.6f} {bmax:>12.6f} ${bmax * eth_usd:>9.2f}")
    
    # Show current caps
    print(f"\n=== CURRENT CAPS ===")
    print(f"live_trading: {caps['live_trading']}")
    print(f"kill_switch: {caps['kill_switch']}")
    print(f"max_trade_eth (static): {caps.get('max_trade_eth', 'not set')}")
    print(f"min_score: {caps.get('min_score', 40)}")

if __name__ == "__main__":
    main()
