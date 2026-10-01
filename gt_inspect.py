import urllib.request, json
url = 'https://api.geckoterminal.com/api/v2/networks/base/trending_pools?page=1'
req = urllib.request.Request(url, headers={'User-Agent': 'Hermes-Inspect/1.0'})
with urllib.request.urlopen(req, timeout=30) as resp:
    data = json.loads(resp.read())
pools = data.get('data', [])
print(f'Total pools: {len(pools)}')
for i, pool in enumerate(pools):
    a = pool.get('attributes', {})
    bt = a.get('base_token', {})
    qt = a.get('quote_token', {})
    bs = bt.get('symbol', '?')
    qs = qt.get('symbol', '?')
    liq = a.get('liquidity_usd')
    liq_str = f'{liq:,.0f}' if liq else 'None'
    print(f'{i+1}. {bs}/{qs} liq={liq_str} fee={a.get("fee_tier")} addr={pool.get("id","")[:30]}')
