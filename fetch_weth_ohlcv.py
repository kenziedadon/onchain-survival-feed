import urllib.request
import urllib.error
import json
import time
from datetime import datetime, timezone

# Correct pool ID from GeckoTerminal response: base_0x6c561b446416e1a00e8e93e221854d6ea4171372
# Pool address: 0x6c561b446416e1a00e8e93e221854d6ea4171372
pool_addr = '0x6c561b446416e1a00e8e93e221854d6ea4171372'

print(f'Fetching OHLCV for WETH/USDC pool: {pool_addr}...')
for attempt in range(3):
    try:
        time.sleep(1.5)
        req = urllib.request.Request(
            f'https://api.geckoterminal.com/api/v2/networks/base/pools/{pool_addr}/ohlcv/hour?aggregate=1&limit=120',
            headers={'User-Agent': 'HermesBot/1.0'}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            ohlcv_data = json.loads(resp.read())
        break
    except urllib.error.HTTPError as e:
        if e.code == 429:
            print(f'  429, retrying in {3*(attempt+1)}s...')
            time.sleep(3*(attempt+1))
        else:
            raise
else:
    print('Failed after retries')
    exit(1)

# Format is different in GeckoTerminal.
# data -> attributes -> ohlcv_list which is a list of arrays: [timestamp, open, high, low, close, volume]
ohlcv_list = ohlcv_data.get('data', {}).get('attributes', {}).get('ohlcv_list', [])
print(f'OHLCV list size: {len(ohlcv_list)}')

if not ohlcv_list:
    print('No OHLCV list returned')
    exit(1)

# Extract closes (most recent first in GeckoTerminal)
# Each entry is [timestamp, open, high, low, close, volume]
closes = []
for entry in ohlcv_list:
    # Close is at index 4
    if len(entry) >= 5:
        close = float(entry[4])
        if close > 0:
            closes.append(close)

if len(closes) < 21:
    print(f'Not enough data: {len(closes)} points')
    exit(1)

print(f'Valid closes: {len(closes)}')
print(f'Most recent (hour ago): ${closes[0]:.4f}')
print(f'Oldest (120h ago): ${closes[-1]:.4f}')

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

print(f'\n=== EMA ANALYSIS ===')
print(f'EMA(9):  {ema9:.6f}')
print(f'EMA(21): {ema21:.6f}')
print(f'Prev EMA(9):  {prev_ema9:.6f}')
print(f'Prev EMA(21): {prev_ema21:.6f}')

print(f'\n=== CROSSOVER CHECK ===')
print(f'prev_ema9 ({prev_ema9:.6f}) <= prev_ema21 ({prev_ema21:.6f}): {prev_ema9 <= prev_ema21}')
print(f'ema9 ({ema9:.6f}) > ema21 ({ema21:.6f}): {ema9 > ema21}')

crossover = (prev_ema9 <= prev_ema21 and ema9 > ema21)
print(f'\nBullish crossover: {"YES" if crossover else "NO"}')

# Latest volume data
latest = ohlcv_list[0] if ohlcv_list else []
# [timestamp, open, high, low, close, volume]
current_price = float(latest[4]) if len(latest) >= 5 else 0.0
volume_1h = float(latest[5]) if len(latest) >= 6 else 0.0

print(f'\n=== LATEST METRICS ===')
print(f'Close price: ${current_price:.4f}')
print(f'Volume 1h: ${volume_1h:,.2f}')

# Save the full result
result = {
    "pool_address": pool_addr,
    "symbol": "WETH",
    "quote": "USDC",
    "liquidity_usd": 115464047,
    "ema9": round(ema9, 6),
    "ema21": round(ema21, 6),
    "prev_ema9": round(prev_ema9, 6),
    "prev_ema21": round(prev_ema21, 6),
    "bullish_crossover": crossover,
    "current_price": current_price,
    "volume_5m": 0.0, # Not in OHLCV list directly, default 0
    "volume_1h": volume_1h,
    "price_change_24h": 0.0, # Not in OHLCV list directly, default 0
    "ohlcv_points": len(closes)
}

output_path = '/home/kenzodadon/base-trend-bot/weth_ohlcv_result.json'
with open(output_path, 'w') as f:
    json.dump(result, f, indent=2)
print(f'\nResult saved to {output_path}')
