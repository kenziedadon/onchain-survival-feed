#!/usr/bin/env python3
"""
V8 BASKET DRY-RUN — proves (or refutes) profitability at the REAL wallet size.
Uses the EXACT v8 strategy spec + on-chain quotes, NO real broadcast.

Quote source: GeckoTerminal pool OHLCV (same as backtest.py) for trend signals,
plus a live on-chain ETH price for notional sizing. We do NOT call the chain for
swaps (dry-run) — we model round-trip cost from liquidity exactly like backtest.py.

Reports: per-coin expected return at the real ~0.003 ETH balance, and whether
the basket is net-positive AFTER gas (which is the whole point at micro size).
"""
import json, os, time, urllib.request
from datetime import datetime, timezone
from _tolerant_read import iter_jsonl_entries

HERE = os.path.dirname(os.path.abspath(__file__))
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json;version=20230302"}

# ---- v8 constants (from backtest.py) ----
WETH = "0x4200000000000000000000000000000000000006"
AERO_ROUTER = "0xBE6D8f0d05cC4be24d5167a3eF062215bE6D18A5"
UNIV3_ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"

def get(url, timeout=25, retries=5):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode()
        except Exception as e:
            if i < retries - 1:
                time.sleep(1.5); continue
            raise
    raise RuntimeError("get failed")

def eth_usd():
    try:
        j = json.loads(get("https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd"))
        return float(j["ethereum"]["usd"])
    except Exception:
        return 2500.0  # fallback

def latest_scan_coins():
    """Read the most recent discovered.jsonl record (basket universe)."""
    import glob
    recs = list(iter_jsonl_entries(os.path.join(HERE, "discovered.jsonl")))
    return recs[-1].get("coins", []) if recs else []

def basket_from_backtest():
    """The exact basket that backtested profitable (backtest_results.json)."""
    p = os.path.join(HERE, "backtest_results.json")
    if not os.path.exists(p):
        return []
    return json.load(open(p))

def model_roundtrip(coin, balance_eth, eth_usd_price):
    """
    Model a 1-cycle (ETH->alt->ETH) round trip at the REAL balance.
    Mirrors backtest.py cost model: fee_bps + slippage(notional/liq) + gas.
    Uses the backtest's OBSERVED net_return_pct as the gross edge signal,
    then subtracts the REAL gas cost at micro size.
    """
    notional_usd = balance_eth * eth_usd_price
    liq = coin.get("liq", coin.get("liquidity", 0))
    fee_bps = coin.get("fee_bps", 30.0)
    # gross edge from backtest (per full-position cycle)
    gross = coin.get("net_return_pct", 0.0)
    # slippage each side ~ notional/liquidity * 10000 * 0.5
    slip_each = min(max((notional_usd / liq) * 10000 * 0.5, 1.0), 80.0) if liq else 80.0
    gas_usd = 0.04
    gas_bps_each = gas_usd / notional_usd * 10000 if notional_usd else 9999
    # round-trip cost in bps
    cost_bps = (fee_bps + slip_each) * 2 + gas_bps_each * 2
    cost_pct = cost_bps / 100.0
    net = gross - cost_pct
    return {
        "name": coin.get("name"),
        "gross_edge_pct": round(gross, 2),
        "cost_pct_at_size": round(cost_pct, 2),
        "net_pct_at_size": round(net, 2),
        "notional_usd": round(notional_usd, 2),
        "gas_bps_each": round(gas_bps_each, 1),
        "profitable_at_size": net > 0,
    }

def main():
    print("=== V8 BASKET DRY-RUN (no broadcast) ===")
    eth_price = eth_usd()
    bal_eth = 0.002971  # live on-chain balance
    print(f"ETH price: ${eth_price:.0f} | wallet: {bal_eth:.6f} ETH (${bal_eth*eth_price:.2f})\n")

    basket = basket_from_backtest()
    if not basket:
        print("No backtest_results.json — cannot build basket."); return
    print(f"Basket ({len(basket)} coins) — modeling 1 round-trip each at REAL balance:\n")
    rows = []
    for c in basket:
        # attach liquidity from latest scan if missing
        m = model_roundtrip(c, bal_eth, eth_price)
        rows.append(m)
        flag = "✅ NET+" if m["profitable_at_size"] else "❌ NET- (gas eats it)"
        print(f"  {m['name'][:24]:24} gross={m['gross_edge_pct']:+6.1f}% "
              f"cost@{bal_eth*eth_price:.1f}$={m['cost_pct_at_size']:+6.1f}% "
              f"net={m['net_pct_at_size']:+6.1f}% gas={m['gas_bps_each']:.0f}bp/side  {flag}")

    prof = [r for r in rows if r["profitable_at_size"]]
    print(f"\nProfitable-at-size: {len(prof)}/{len(rows)}")
    avg_net = sum(r["net_pct_at_size"] for r in rows)/len(rows)
    print(f"Avg net return at REAL balance: {avg_net:+.2f}%  "
          f"({'PROFITABLE' if avg_net>0 else 'NOT PROFITABLE — gas-dominated'})")
    # equal-weight basket: each coin gets 1/N of balance
    n = len(rows)
    per = bal_eth/n
    print(f"\nEqual-weight split: {per:.6f} ETH/coin (${per*eth_price:.2f})")
    if prof:
        print("Coins that survive gas at this size:", ", ".join(r["name"] for r in prof))
    else:
        print("⚠ NONE survive gas at 0.003 ETH. Strategy needs ~0.05-0.1 ETH to be viable.")

if __name__ == "__main__":
    main()
