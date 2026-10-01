#!/usr/bin/env python3
"""Test script to check API connectivity."""
import urllib.request
import json
import time

url = 'https://api.geckoterminal.com/api/v2/networks/base/pools/0x8c86e46defb25d481f6d85aeb4c4aa30fda06e5c330afb0803f5d42cda65069c'
req = urllib.request.Request(url, headers={'Accept': 'application/json;version=20230302', 'User-Agent': 'Mozilla/5.0'})

print('Testing single API call...')
t0 = time.time()
try:
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    elapsed = time.time() - t0
    print(f'OK in {elapsed:.1f}s')
    attrs = data.get('data', {}).get('attributes', {})
    print(f'Pool: {attrs.get("name", "unknown")}')
    print(f'Liq: ${float(attrs.get("reserve_in_usd", 0)):,.0f}')
except Exception as e:
    elapsed = time.time() - t0
    print(f'Failed after {elapsed:.1f}s: {e}')
