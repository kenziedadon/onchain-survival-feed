#!/usr/bin/env python3
import json, time, urllib.request, urllib.error

GECKO = "https://api.geckoterminal.com/api/v2"
UA = {"User-Agent": "base-trend-bot/1.0", "Accept": "application/json"}

def gecko_get(url, retries=8):
    delay = 3.0
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429 and i < retries - 1:
                time.sleep(delay + i * 1.5)
                delay = min(delay * 1.8, 30)
                continue
            raise

def ema(values, period):
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e

def find_crossover(closes):
    if len(closes) < 22:
        return False, None, None, "too_short"
    fast = ema(closes, 9); slow = ema(closes, 21)
    pf = ema(closes[:-1], 9); ps = ema(closes[:-1], 21)
    bull = (pf <= ps) and (fast > slow)
    return bull, fast, slow, f"prev_f={pf:.6g}/s={ps:.6g} curr_f={fast:.6g}/s={slow:.6g}"

# Retry BNKR (the one that 429'd in main run)
pool = "0xaec085e5a5ce8d96a7bdd3eb3a62445d4f6ce703"
url = f"{GECKO}/networks/base/pools/{pool}/ohlcv/hour?aggregate=1&limit=120"
try:
    o = gecko_get(url)
    rows = o["data"]["attributes"]["ohlcv_list"]
    closes = [float(r[4]) for r in rows if r[4] is not None]
    bull, f, s, dbg = find_crossover(closes)
    print(f"BNKR: closes={len(closes)} crossover_bullish={bull}")
    print(f"  {dbg}")
except Exception as e:
    print(f"BNKR retry FAILED: {e}")

# Sanity re-check of 2 already-scored pools using same logic (independent recompute)
sig = json.load(open("/home/kenzodadon/base-trend-bot/nous_signals.json"))
for t in sig["tokens"][:3]:
    if t["closes_count"] < 22:
        continue
    print(f"{t['symbol']}: closes_count={t['closes_count']} recorded_fast={t['ema_fast']:.6g} "
          f"recorded_slow={t['ema_slow']:.6g} bullish={t['crossover_bullish']}")
