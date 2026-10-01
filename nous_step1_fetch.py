#!/usr/bin/env python3
"""Nous sentiment gate — fetches trending pools from DEXScreener (GeckoTerminal rate-limited)."""
import urllib.request, json, time, sys

UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'

def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())

def num(v):
    if v is None: return 0.0
    if isinstance(v, (int, float)): return float(v)
    try: return float(v)
    except: return 0.0

# Step 1: Fetch trending pairs from DEXScreener
print('=== STEP 1: DEXScreener trending ===')
try:
    data = fetch('https://api.dexscreener.com/latest/dex/trending')
    pairs = data.get('pairs', [])
    print(f'Got {len(pairs)} total pairs')
except Exception as e:
    print(f'DEXScreener failed: {e}')
    sys.exit(1)

base_pairs = [p for p in pairs if p.get('chainId') == 'base']
print(f'Base chain: {len(base_pairs)} pairs')

# Show top pairs
for p in base_pairs[:10]:
    b = p.get('baseToken', {}).get('symbol', '?')
    q = p.get('quoteToken', {}).get('symbol', '?')
    liq = num(p.get('liquidity', {}).get('usd', 0))
    vol24 = num(p.get('volume', {}).get('h24', 0))
    addrs = p.get('pairAddrs', [])
    print(f'  {b}/{q}  liq=${liq:>12,.0f}  vol24h=${vol24:>12,.0f}  {addrs[0][:10] if addrs else "?"}...')

# Filter: ETH/WETH quote, >$100k liq
candidates = []
for p in base_pairs:
    q = p.get('quoteToken', {}).get('symbol', '').upper()
    liq = num(p.get('liquidity', {}).get('usd', 0))
    if q in ('ETH', 'WETH') and liq > 100000:
        candidates.append({
            'base_symbol': p['baseToken']['symbol'],
            'quote_symbol': q,
            'pair_addr': (p.get('pairAddrs') or [''])[0],
            'liquidity_usd': liq,
            'vol_h24': num(p.get('volume', {}).get('h24', 0)),
            'vol_h1': num(p.get('volume', {}).get('h1', 0)),
            'vol_m5': num(p.get('volume', {}).get('m5', 0)),
            'price_change_h24': num(p.get('priceChange', {}).get('h24', 0)),
            'base_addr': p['baseToken'].get('address', ''),
            'quote_addr': p['quoteToken'].get('address', ''),
        })

print(f'\n=== {len(candidates)} ETH/WETH candidates with >$100k liq ===')
for c in candidates:
    print(f'  {c["base_symbol"]} liq=${c["liquidity_usd"]:,.0f} vol24h=${c["vol_h24"]:,.0f}')

if not candidates:
    print('\n[SILENT] — no qualifying candidates')
    sys.exit(0)

with open('/tmp/nous_candidates.json', 'w') as f:
    json.dump(candidates, f, indent=2)
print('Saved /tmp/nous_candidates.json')
