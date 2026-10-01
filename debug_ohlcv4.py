import json, urllib.request

url = "https://api.geckoterminal.com/api/v2/networks/base/pools/0x7692acc1cdd771d09ebcae3663e1843b2911bec7/ohlcv/hour?aggregate=1&limit=120"
req = urllib.request.Request(url, headers={
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; HermesAgent/1.0)",
})
with urllib.request.urlopen(req, timeout=30) as resp:
    data = json.loads(resp.read())

ohlcv_list = data["data"]["attributes"]["ohlcv_list"]
print(f"ohlcv_list: {len(ohlcv_list)} items")
print()

if ohlcv_list:
    first = ohlcv_list[0]
    last = ohlcv_list[-1]
    print(f"First candle (list): {first}")
    print(f"  len: {len(first)}")
    print(f"Last candle (list): {last}")
    print()
    print(f"Full ohlcv_list[0]: {first}")
    print(f"Full ohlcv_list[1]: {ohlcv_list[1]}")
    print(f"Full ohlcv_list[2]: {ohlcv_list[2]}")
    print()
    
    # Try to figure out the schema
    # Common: [timestamp, open, high, low, close, volume] or [timestamp, open, high, low, close_quote, volume]
    print("Trying to parse as OHLCV arrays:")
    for i, candle in enumerate(ohlcv_list[:5]):
        print(f"  [{i}]: {candle}")
    
    print()
    # Extract closes (index 4 typically, or 3)
    # Let's check: if volume is last, close is index 4
    # If no volume, close is index 3
    sample = ohlcv_list[0]
    print(f"Sample len: {len(sample)}")
    if len(sample) >= 5:
        print(f"  Index 3: {sample[3]} (could be close)")
        print(f"  Index 4: {sample[4]} (could be volume or close)")
        print(f"  Index 5: {sample[5] if len(sample) > 5 else 'N/A'}")
