import urllib.request, json

# Dump raw first pool from trending
url = 'https://api.geckoterminal.com/api/v2/networks/base/trending_pools?page=1'
req = urllib.request.Request(url, headers={'User-Agent': 'Hermes-Dump/1.0'})
with urllib.request.urlopen(req, timeout=30) as resp:
    data = json.loads(resp.read())

pool = data['data'][0]
print("=== RAW FIRST POOL ===")
print(json.dumps(pool, indent=2)[:3000])
print("\n=== ATTRIBUTES KEYS ===")
attrs = pool.get('attributes', {})
print(list(attrs.keys()))
print("\n=== BASE TOKEN ===")
print(json.dumps(pool.get('attributes', {}).get('base_token', {}), indent=2))
print("\n=== QUOTE TOKEN ===")
print(json.dumps(pool.get('attributes', {}).get('quote_token', {}), indent=2))
