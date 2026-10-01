#!/usr/bin/env python3
"""Inspect GeckoTerminal trending pools — raw dump for debugging filters."""
import urllib.request, json, sys

url = "https://api.geckoterminal.com/api/v2/networks/base/trending_pools?page=1"
req = urllib.request.Request(url, headers={"User-Agent": "Hermes/1.0"})
with urllib.request.urlopen(req, timeout=30) as resp:
    data = json.loads(resp.read())

pools = data.get("data", [])
print(f"Total pools: {len(pools)}")
print(f"Keys in first pool: {list(pools[0].keys()) if pools else 'N/A'}")

for i, p in enumerate(pools):
    a = p.get("attributes", {})
    addr = a.get("address", "???")
    base = a.get("base_token", {})
    base_sym = base.get("symbol", "???") if isinstance(base, dict) else str(base)
    quote = a.get("quote_token", {})
    quote_sym = quote.get("symbol", "???") if isinstance(quote, dict) else str(quote)
    tvl = a.get("total_value_locked_usd", 0)
    try:
        tvl = float(tvl)
    except:
        tvl = 0
    ticker = p.get("ticker", {})
    vol1h = ticker.get("volume_1h", 0) if isinstance(ticker, dict) else "N/A"
    pc24h = ticker.get("price_change_24h", 0) if isinstance(ticker, dict) else "N/A"

    # Also print raw keys for first pool
    if i == 0:
        print(f"\nFull first pool keys: {list(p.keys())}")
        print(f"Attributes keys: {list(a.keys())}")
        print(f"Ticker: {ticker}")

    print(f"{i:2d}. {addr[:12]}...  symbol={base_sym}/{quote_sym:6s}  tvl={tvl:>12,.0f}  vol1h={vol1h}  pc24h={pc24h}")
