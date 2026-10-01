#!/usr/bin/env python3
import urllib.request, json

url = "https://api.geckoterminal.com/api/v2/networks/base/trending_pools?page=1"
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
with urllib.request.urlopen(req, timeout=30) as resp:
    data = json.loads(resp.read())

pools = data.get("data", [])
print(f"Total pools: {len(pools)}")
for i, p in enumerate(pools):
    a = p.get("attributes", {})
    liq = a.get("liquidity_usd", 0)
    base = p.get("base_token", {})
    quote = p.get("quote_token", {})
    bsym = base.get("symbol", "?")
    qsym = quote.get("symbol", "?")
    paddr = base.get("address", "?")
    print(
        f"{i+1}. {bsym}/{qsym} | liq=${liq:,.0f} | "
        f"base_addr={paddr[:10]}... | "
        f"vol5m=${a.get('volume_usd_5m',0):,.0f} | "
        f"vol24h=${a.get('volume_usd_24h',0):,.0f} | "
        f"price_chg_24h={a.get('price_change_24h',0):.2f}%"
    )
