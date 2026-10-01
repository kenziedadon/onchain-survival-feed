#!/usr/bin/env python3
"""Fetch OHLCV for known candidates. Designed to run in background."""
import urllib.request, json, time, sys

UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'
BASE = 'https://api.geckoterminal.com/api/v2/networks/base'

def fetch(url, timeout=45):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())

with open('/tmp/nous_candidates.json') as f:
    candidates = json.load(f)

known = [c for c in candidates if c['token_symbol'] != '?']
print(f'Starting OHLCV fetch for {len(known)} tokens...', flush=True)

results = []
for i, c in enumerate(known):
    print(f'[{i+1}/{len(known)}] {c["token_symbol"]} ({c["pool_addr"][:10]}...)', end=' ', flush=True)
    try:
        url = f'{BASE}/pools/{c["pool_addr"]}/ohlcv/hour?aggregate=1&limit=120'
        data = fetch(url, timeout=45)
        candles = data.get('data', [])
        print(f'OK: {len(candles)} candles', flush=True)
        results.append({'symbol': c['token_symbol'], 'candles': len(candles), 'pool_addr': c['pool_addr'], 'ohlcv_raw': data})
    except Exception as e:
        print(f'ERROR: {e}', flush=True)
        results.append({'symbol': c['token_symbol'], 'error': str(e)[:80], 'pool_addr': c['pool_addr']})
    
    if i < len(known) - 1:
        print('  waiting 90s...', flush=True)
        time.sleep(90)

# Save raw OHLCV data
output = {
    'fetched_at': time.strftime('%Y-%m-%dT%H:%M:%SZ'),
    'results': results
}
with open('/tmp/nous_ohlcv_raw.json', 'w') as f:
    # Don't save raw API responses (huge) — just metadata
    slim = [{'symbol': r['symbol'], 'candles': r.get('candles'), 'error': r.get('error'), 'pool_addr': r.get('pool_addr')} for r in results]
    json.dump({'fetched_at': output['fetched_at'], 'results': slim}, f, indent=2)

print(f'\nDone. Saved to /tmp/nous_ohlcv_raw.json')
print('Results:')
for r in results:
    status = f'{r.get("candles","?")} candles' if 'candles' in r else f'ERR: {r.get("error","?")}'
    print(f'  {r["symbol"]:<10s} {status}')
