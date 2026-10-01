#!/usr/bin/env python3
import urllib.request, json

url = 'https://api.geckoterminal.com/api/v2/networks/base/trending_pools?page=1'
req = urllib.request.Request(url, headers={'Accept': 'application/json', 'User-Agent': 'Mozilla/5.0'})
with urllib.request.urlopen(req, timeout=15) as r:
    raw = r.read().decode()
data = json.loads(raw)

print("Top-level keys:", list(data.keys()))
print("Data len:", len(data.get('data', [])))
print()

p = data['data'][0]
print("Pool keys:", list(p.keys()))
print()

# Check relationships
rels = p.get('relationships', {})
for k, v in rels.items():
    print(f"relationship[{k}]:", json.dumps(v, indent=2)[:500])
print()

# Check if 'included' exists at top level
if 'included' in data:
    print(f"Included: {len(data['included'])} items")
    for item in data['included'][:3]:
        print(f"  type={item['type']}, id={item['id']}")
        print(f"  attrs keys: {list(item.get('attributes', {}).keys())[:10]}")
        if item['type'] == 'token' and 'symbol' in item.get('attributes', {}):
            print(f"  symbol={item['attributes']['symbol']}, address={item['attributes'].get('address')}")
else:
    print("No 'included' in response")
    # Try fetching a pool's tokens via the pool endpoint
    pool_addr = p['attributes']['address']
    pool_url = f'https://api.geckoterminal.com/api/v2/networks/base/pools/{pool_addr}'
    print(f"\nTrying pool detail: {pool_url}")
    req2 = urllib.request.Request(pool_url, headers={'Accept': 'application/json', 'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req2, timeout=15) as r2:
        pool_data = json.loads(r2.read().decode())
    print("Pool detail keys:", list(pool_data.keys()))
    if 'included' in pool_data:
        for item in pool_data['included'][:5]:
            print(f"  type={item['type']}, id={item['id']}")
            if item['type'] == 'token':
                print(f"    symbol={item['attributes'].get('symbol')}")
                print(f"    address={item['attributes'].get('address')}")
                print(f"    attrs={json.dumps(item['attributes'], indent=2)[:300]}")
