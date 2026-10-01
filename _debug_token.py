#!/usr/bin/env python3
"""Debug token endpoint structure"""
import urllib.request, json

tok_id = 'base_0x9a26f5433671751c3276a065f57e5a02d2817973'
url = f'https://api.geckoterminal.com/api/v2/networks/base/tokens/{tok_id}'
req = urllib.request.Request(url, headers={'Accept': 'application/json', 'User-Agent': 'Mozilla/5.0'})
with urllib.request.urlopen(req, timeout=15) as r:
    data = json.loads(r.read().decode())
print('Token keys:', list(data.get('data', {}).keys()))
print('Attrs:', list(data['data'].get('attributes', {}).keys())[:10])
print('Symbol:', data['data']['attributes'].get('symbol'))
print('Name:', data['data']['attributes'].get('name'))
print('Address:', data['data']['attributes'].get('address'))
