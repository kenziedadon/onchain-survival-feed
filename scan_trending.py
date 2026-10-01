import json, urllib.request

UA = {'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json;version=20230302'}
url = 'https://api.geckoterminal.com/api/v2/networks/base/trending_pools?page=1'
req = urllib.request.Request(url, headers=UA)
with urllib.request.urlopen(req, timeout=20) as resp:
    data = json.loads(resp.read())

for p in data['data']:
    a = p['attributes']
    names = a.get('token_names', {})
    bt = names.get('base_token', '?')
    qt = names.get('quote_token', '?')
    liq = float(a.get('liquidity_usd') or 0)
    chg = a.get('price_change_percentage', 0) or 0
    weth = 'WETH' in qt
    mark = '<<< WETH' if weth else ''
    print(f'{bt} / {qt}  liq=${liq:,.0f}  chg24h={chg:.1f}%  {mark}')
