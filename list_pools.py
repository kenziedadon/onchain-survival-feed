import json

with open('/home/kenzodadon/base-trend-bot/raw_trending.json') as f:
    data = json.load(f)

for i, p in enumerate(data['data']):
    a = p.get('attributes', {})
    pool_id = p.get('id', '')[:12]
    name = a.get('name', '?')
    addr = a.get('address', '')
    liq = a.get('reserve_in_usd', 0) or 0
    fdv = a.get('fdv_usd', 0) or 0
    mcap = a.get('market_cap_usd', 0) or 0
    vol = a.get('volume_usd', {})
    vol1h = vol.get('1h', 0) if isinstance(vol, dict) else 0
    vol5m = vol.get('5m', 0) if isinstance(vol, dict) else 0
    price_chg = a.get('price_change_percentage', {})
    chg1h = price_chg.get('1h', 0) if isinstance(price_chg, dict) else 0
    chg24h = price_chg.get('24h', 0) if isinstance(price_chg, dict) else 0
    tx = a.get('transactions', {})
    tx1h = tx.get('1h', 0) if isinstance(tx, dict) else 0
    base_price = a.get('base_token_price_usd', 0) or 0
    quote_price = a.get('quote_token_price_usd', 0) or 0
    try:
        liq = float(liq)
    except:
        liq = 0
    try:
        fdv = float(fdv)
    except:
        fdv = 0
    try:
        mcap = float(mcap)
    except:
        mcap = 0
    # Derive base/quote symbol from name: "Basecat / WETH 1%"
    parts = name.split(' / ')
    base_sym = parts[0] if len(parts) > 0 else '?'
    quote_sym = parts[1].split(' ')[0] if len(parts) > 1 else '?'
    
    print(f"{i+1}. {pool_id} | {name}")
    print(f"   addr={addr} base_sym={base_sym} quote_sym={quote_sym}")
    print(f"   liq=${liq:,.0f} fdv=${fdv:,.0f} mcap=${mcap:,.0f}")
    print(f"   vol1h=${vol1h:,.0f} vol5m=${vol5m:,.0f} tx1h={tx1h}")
    print(f"   p1h={chg1h:.2f}% p24h={chg24h:.2f}%")
    print(f"   basePrice=${base_price:.6f} quotePrice=${quote_price:.4f}")
    print()
