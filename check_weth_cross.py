import urllib.request
import urllib.error
import json
import time
from datetime import datetime, timezone

# Pool 7: WETH / USDC 0.3% - this has WETH as base token
pool_addr = '0x6c561b446416a044947be51fa013997c0a7a280278916170d10845d84cb17945'

print(f'Fetching OHLCV for WETH/USDC pool: {pool_addr}...')
time.sleep(1.5)

req = urllib.request.Request(
    f'https://api.geckoterminal.com/api/v2/networks/base/pools/{pool_addr}/ohlcv/hour?aggregate=1&limit=120',
    headers={'User-Agent': 'HermesBot/1.0'}
)
with urllib.request.urlopen(req, timeout=30) as resp:
    ohlcv_data = json.loads(resp.read())

data_points = ohlcv_data.get('data', [])
print(f'OHLCV data points: {len(data_points)}')

# Extract closes (GeckoTerminal returns most recent first)
closes = []
for entry in data_points:
    attrs = entry.get('attributes', {})
    close = float(attrs.get('close_price', 0) or 0)
    if close > 0:
        closes.append(close)

print(f'Valid closes: {len(closes)}')
print(f'Most recent: ${closes[-1]:.4f}')
print(f'Oldest: ${closes[0]:.4f}')

if len(closes) >= 21:
    # Reverse to chronological order (oldest first)
    closes_chrono = closes[::-1]
    
    # Compute EMA
    def compute_ema(prices, period):
        if len(prices) < period:
            return None
        k = 2 / (period + 1)
        ema = prices[0]
        for p in prices[1:]:
            ema = p * k + ema * (1 - k)
        return ema
    
    ema9 = compute_ema(closes_chrono, 9)
    ema21 = compute_ema(closes_chrono, 21)
    
    prev_ema9 = compute_ema(closes_chrono[:-1], 9)
    prev_ema21 = compute_ema(closes_chrono[:-1], 21)
    
    print(f'\nEMA(9): {ema9:.6f}')
    print(f'EMA(21): {ema21:.6f}')
    print(f'Prev EMA(9): {prev_ema9:.6f}')
    print(f'Prev EMA(21): {prev_ema21:.6f}')
    
    crossover = (prev_ema9 <= prev_ema21 and ema9 > ema21)
    print(f'\nBullish crossover: {"YES" if crossover else "NO"}')
    print(f'  prev_ema9 ({prev_ema9:.6f}) <= prev_ema21 ({prev_ema21:.6f}): {prev_ema9 <= prev_ema21}')
    print(f'  ema9 ({ema9:.6f}) > ema21 ({ema21:.6f}): {ema9 > ema21}')
