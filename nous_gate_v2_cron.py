#!/usr/bin/env python3
"""Nous sentiment gate v2 — works with cached trending + candle data.
GeckoTerminal live API rate-limited (429), so we use cached data."""
import urllib.request, urllib.error, json, os, glob, re, time
from datetime import datetime, timezone

NOUS_BASE = "https://inference-api.nousresearch.com/v1"
NOUS_MODEL = "upstage/solar-pro4:free"
OUT = "/home/kenzodadon/base-trend-bot/nous_signals.json"
CANDLE_CACHE = "/home/kenzodadon/base-trend-bot/candle_cache"
TRENDING_CACHE = "/home/kenzodadon/base-trend-bot/current_trending.json"
MIN_RESERVE_USD = 100_000  # approximate liquidity floor
SLEEP = 1.5

# --- auth ---
with open("/home/kenzodadon/.hermes/shared/nous_auth.json") as f:
    auth = json.load(f)
token = auth["access_token"]
print(f"[1] Nous token loaded, expires={auth['expires_at']}")

# --- load cached trending ---
with open(TRENDING_CACHE) as f:
    tdata = json.load(f)
pools_raw = tdata.get("data", [])
print(f"[2] Loaded {len(pools_raw)} cached trending pools")

# Build candle cache lookup: pool_address -> candles list
candle_lookup = {}
for cf in glob.glob(os.path.join(CANDLE_CACHE, "*.json")):
    with open(cf) as f:
        cdata = json.load(f)
    key = cdata.get("key", "")
    # key format: "address:count" e.g. "0xaec0...:500"
    if ":" in key:
        addr = key.split(":")[0]
    else:
        addr = key
    candles = cdata.get("data", [])
    if candles:
        candle_lookup[addr.lower()] = candles

print(f"[2b] Loaded {len(candle_lookup)} candle caches")

def parse_pair_name(name):
    """Extract base/quote symbols from pool name like 'DRB / WETH 1%'."""
    if not name:
        return None, None
    m = re.match(r'^\s*([^/]+?)\s*/\s*([^/\s]+)', name.strip())
    if not m:
        return None, None
    base = m.group(1).strip().upper()
    quote = m.group(2).strip().upper()
    return base, quote

# Filter pools: ETH or WETH quote, and sufficient reserve
candidates = []
for p in pools_raw:
    a = p.get("attributes", {})
    name = a.get("name", "")
    base_sym, quote_sym = parse_pair_name(name)
    if not base_sym or not quote_sym:
        continue
    if quote_sym not in ("ETH", "WETH"):
        continue
    reserve = float(a.get("reserve_in_usd", "0") or 0)
    vol24 = float(a.get("volume_usd", {}).get("h24", "0") or 0)
    if reserve < MIN_RESERVE_USD and vol24 < MIN_RESERVE_USD:
        continue
    addr = a.get("address", "")
    pool = {
        "address": addr,
        "name": name,
        "base_symbol": base_sym,
        "quote_symbol": quote_sym,
        "reserve_usd": reserve,
        "volume_24h_usd": vol24,
        "pool_created_at": a.get("pool_created_at", ""),
        "market_cap_usd": float(a.get("market_cap_usd", "0") or 0),
        "price_change_24h": a.get("price_change_percentage", {}).get("h24", None),
    }
    candidates.append(pool)

print(f"[2c] ETH/WETH pairs with reserve>$100k or vol24>$100k: {len(candidates)}")
for c in candidates:
    clk = "CACHED" if c["address"].lower() in candle_lookup else "NO-CACHE"
    print(f"    {c['address'][:10]}... {c['name']:<30} reserve=${c['reserve_usd']:,.0f} vol24=${c['volume_24h_usd']:,.0f} [{clk}]")

# EMA calc
def ema_calc(prices, period):
    if len(prices) < period:
        return None
    k = 2.0 / (period + 1)
    e = prices[0]
    for p in prices[1:]:
        e = (p - e) * k + e
    return e

signals = []
alerts = []

for i, pool in enumerate(candidates):
    addr = pool["address"]
    sym = pool["base_symbol"]
    qsym = pool["quote_symbol"]
    print(f"\n[{i+1}/{len(candidates)}] {sym}/{qsym} {addr[:10]}... name='{pool['name']}'")

    candles = candle_lookup.get(addr.lower(), [])
    if not candles:
        print(f"    No OHLCV cache — scoring from name/volume only")
        # Still call Nous with what we have
        has_candles = False
    else:
        has_candles = True
        print(f"    {len(candles)} candles cached")

    # Extract closing prices
    closes = []
    for c in candles:
        cv = c.get("c") or c.get("close") or 0
        if cv:
            closes.append(float(cv))

    entry = {
        "token_address": addr,
        "symbol": sym,
        "quote_symbol": qsym,
        "pool_name": pool["name"],
        "pool_address": addr,
        "liquidity_usd": pool["reserve_usd"],
        "volume_24h_usd": pool["volume_24h_usd"],
        "volume_5m_usd": 0,
        "volume_1h_usd": round(pool["volume_24h_usd"] / 24.0, 4) if pool["volume_24h_usd"] else 0,
        "price_change_24h_pct": pool["price_change_24h"],
        "market_cap_usd": pool["market_cap_usd"],
        "candles_available": len(closes),
    }

    crossover = False
    ema9 = ema21 = pe9 = pe21 = None
    
    if has_candles and len(closes) >= 21:
        e9 = ema_calc(closes, 9)
        e21 = ema_calc(closes, 21)
        if e9 and e21:
            ema9 = round(e9, 6)
            ema21 = round(e21, 6)
            entry["ema9"] = ema9
            entry["ema21"] = ema21
            entry["current_price"] = round(closes[-1], 8)
            
            prev_closes = closes[:-1]
            pe9 = ema_calc(prev_closes, 9)
            pe21 = ema_calc(prev_closes, 21)
            if pe9 and pe21:
                pe9v = round(pe9, 6)
                pe21v = round(pe21, 6)
                entry["prev_ema9"] = pe9v
                entry["prev_ema21"] = pe21v
                crossover = (pe9 <= pe21) and (e9 > e21)
                entry["crossover"] = crossover
                print(f"    EMA9={ema9} EMA21={ema21} PREV9={pe9v} PREV21={pe21v} CROSS={crossover}")
            else:
                print(f"    EMA calc: prev EMAs None")
        else:
            print(f"    EMA calc: None")
    else:
        print(f"    Insufficient candles ({len(closes)}) for EMA(21)")
        entry["crossover"] = False

    # --- Nous scoring ---
    # Always call Nous for any promising pool (even without candles if volume is decent)
    do_score = crossover or (pool["volume_24h_usd"] > 50000) or (pool["reserve_usd"] > 500000)
    
    if not do_score:
        print(f"    Skipping Nous (low volume/reserve)")
        signals.append(entry)
        continue

    prompt = (
        f'You are a Base chain crypto sentiment analyst. Return ONLY valid JSON:\n'
        f'{{"score":0-100,"decision":"BUY" or "NO_GO","reason":"1-2 sentences"}}\n\n'
        f'POOL: {pool["name"]}\n'
        f'TOKEN: {sym} ({addr[:10]}...)\n'
        f'PAIR: {sym}/{qsym}\n'
        f'LIQUIDITY/RESERVE: ${pool["reserve_usd"]:,.0f}\n'
        f'VOLUME 24H: ${pool["volume_24h_usd"]:,.0f}\n'
        f'MARKET CAP: ${pool["market_cap_usd"]:,.0f}\n'
        f'PRICE CHANGE 24H: {pool["price_change_24h"]}%\n'
    )
    if has_candles and len(closes) >= 2:
        chg = ((closes[-1] - closes[0]) / closes[0]) * 100 if closes[0] else 0
        prompt += f'CACHED CANDLES: {len(closes)} hourly candles, price change (full period): {chg:.2f}%\n'
        if ema9 and ema21:
            prompt += f'EMA(9): {ema9} EMA(21): {ema21}\n'
            if crossover:
                prompt += f'CMA CROSSOVER: BULLISH — EMA(9) crossed above EMA(21)\n'
    prompt += f'\nAssess if this token shows genuine momentum or is a false signal/rug candidate.'

    try:
        body = json.dumps({
            "model": NOUS_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 512,
            "temperature": 0.2
        }).encode()
        req = urllib.request.Request(
            f"{NOUS_BASE}/chat/completions",
            data=body,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            rdata = json.loads(resp.read().decode())
        content = rdata["choices"][0]["message"]["content"]
        try:
            nr = json.loads(content)
        except:
            m = re.search(r'\{[^{}]+"score"[^{}]+\}', content)
            nr = json.loads(m.group()) if m else {"score": 50, "decision": "NO_GO", "reason": "parse fail"}
        
        entry["nous_score"] = nr.get("score", 0)
        entry["nous_decision"] = nr.get("decision", "NO_GO")
        entry["nous_reason"] = nr.get("reason", "")
        print(f"    Nous: score={nr.get('score')} dec={nr.get('decision')} | {nr.get('reason','')[:100]}")
        
        if nr.get("score", 0) >= 80 and nr.get("decision") == "BUY":
            a = (f"🚨 ALERT: {sym}/{qsym} ({addr[:10]}...) — Nous {nr['score']}/100 BUY. "
                 f"Pool: {pool['name']} | Liq=${pool['reserve_usd']:,.0f} | Vol24=${pool['volume_24h_usd']:,.0f}")
            alerts.append(a)
            print(f"    >>> {a}")
        time.sleep(1.0)
    except Exception as e:
        print(f"    Nous ERROR: {e}")
        entry["nous_score"] = None
        entry["nous_decision"] = "API_ERR"
        entry["nous_reason"] = str(e)
    
    time.sleep(SLEEP)
    signals.append(entry)

# --- write ---
out = {
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "source": "cached_trending+" + ("cached_candles" if candle_lookup else "no_candles"),
    "scanned_pools": len(candidates),
    "candidates": [{
        "address": c["address"],
        "name": c["name"],
        "base_token": c["base_symbol"],
        "quote_token": c["quote_symbol"],
        "reserve_usd": c["reserve_usd"],
        "volume_24h_usd": c["volume_24h_usd"]
    } for c in candidates],
    "signals": signals,
    "alerts": alerts
}
with open(OUT, "w") as f:
    json.dump(out, f, indent=2)
print(f"\n[5] Written {OUT}")

# --- summary ---
scored = [s for s in signals if s.get("nous_score") is not None]
buys = [s for s in signals if s.get("nous_decision") == "BUY" and s.get("nous_score", 0) >= 80]
crosses = [s for s in signals if s.get("crossover")]

print(f"\n=== SUMMARY ===")
print(f"Timestamp: {out['timestamp']}")
print(f"ETH/WETH pools scanned: {len(candidates)}")
print(f"  With candle cache: {len([c for c in candidates if c['address'].lower() in candle_lookup])}")
print(f"EMA crossovers detected: {len(crosses)}")
print(f"Tokens scored by Nous: {len(scored)}")
print(f"BUY signals (score>=80): {len(buys)}")
print(f"Alerts: {len(alerts)}")
if alerts:
    for a in alerts:
        print(f"  {a}")
else:
    print("No high-conviction alerts this cycle.")
print(f"\nCompact JSON summary:")
print(json.dumps({
    "ts": out["timestamp"],
    "scanned": len(candidates),
    "candles_available": len(candle_lookup),
    "crossovers": len(crosses),
    "scored": len(scored),
    "buys": len(buys),
    "alerts": len(alerts),
    "pools": [
        {
            "symbol": s.get("symbol", "?"),
            "pair": s.get("pool_name", "?"),
            "reserve": s.get("liquidity_usd", 0),
            "vol24": s.get("volume_24h_usd", 0),
            "candles": s.get("candles_available", 0),
            "crossover": s.get("crossover", False),
            "nous_score": s.get("nous_score"),
            "nous_decision": s.get("nous_decision"),
            "nous_reason": (s.get("nous_reason") or "")[:120]
        }
        for s in signals
    ]
}, indent=2))
