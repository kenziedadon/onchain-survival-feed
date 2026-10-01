#!/usr/bin/env python3
"""Nous sentiment gate v3 — correct OHLCV path (ohlcv_list), longer rate limits."""
import json, time, urllib.request, urllib.error
from datetime import datetime, timezone

NOUS_AUTH = json.load(open("/home/kenzodadon/.hermes/shared/nous_auth.json"))
TOKEN = NOUS_AUTH["access_token"]
NOUS_URL = "https://inference-api.nousresearch.com/v1/chat/completions"
NOUS_MODEL = "upstage/solar-pro4:free"
GECKO = "https://api.geckoterminal.com/api/v2/networks/base"
OUT = "/home/kenzodadon/base-trend-bot/nous_signals.json"
RATE = 2.0  # increased to avoid 429
LIQ_MIN = 100_000
EMA_F, EMA_S = 9, 21

def get(url, t=30):
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "accept": "application/json"})
            with urllib.request.urlopen(req, timeout=t) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = RATE * (attempt + 1) * 2
                print(f"    429, waiting {wait:.1f}s...")
                time.sleep(wait)
            else:
                raise
    return None

def post(url, data, t=60):
    req = urllib.request.Request(url, data=json.dumps(data).encode(),
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=t) as r:
        return json.loads(r.read().decode())

def ema(p, s):
    if len(p) < s: return None
    k = 2.0 / (s + 1)
    out = [sum(p[:s]) / s]
    for v in p[s:]:
        out.append(v * k + out[-1] * (1 - k))
    return out

def parse_name(name):
    parts = name.split("/")
    if len(parts) >= 2:
        base = parts[0].strip()
        rest = "/".join(parts[1:]).strip()
        for q in ["WETH", "ETH", "USDC", "USD.Base", "cbBTC", "stETH"]:
            if rest.startswith(q):
                return base, q
        words = rest.split()
        for w in words:
            if len(w) >= 3:
                return base, w.rstrip("%")
    return name, "UNKNOWN"

def main():
    ts = datetime.now(timezone.utc).isoformat()
    print(f"[{ts}] Starting Nous sentiment gate v3")
    print(f"[*] Auth token loaded, expires_at={NOUS_AUTH.get('expires_at')}")
    
    # Step 2: fresh trending fetch
    print("[*] Fetching GeckoTerminal trending pools (fresh)...")
    try:
        trend = get(f"{GECKO}/trending_pools?page=1")
        pools = trend.get("data", [])
        print(f"[*] Got {len(pools)} trending pools (fresh)")
    except Exception as e:
        print(f"[*] Fresh fetch failed ({e}), using cache")
        pools = json.load(open("/home/kenzodadon/base-trend-bot/raw_trending.json")).get("data", [])
        print(f"[*] Got {len(pools)} pools from cache")
    
    candidates = []
    for i, pool in enumerate(pools):
        a = pool.get("attributes", {})
        pid = pool.get("id", "")
        paddr = pid.replace("base_", "") if pid.startswith("base_") else pid
        
        raw_name = a.get("name", "")
        base_sym, quote_sym = parse_name(raw_name)
        
        liq_str = a.get("reserve_in_usd") or "0"
        try:
            liq = float(liq_str)
        except:
            liq = 0
        
        vol_h1 = float(a.get("volume_usd", {}).get("h1") or 0)
        vol_5m = float(a.get("volume_usd", {}).get("m5") or 0)
        pc_24h = float(a.get("price_change_percentage", {}).get("h24") or 0)
        pc_1h = float(a.get("price_change_percentage", {}).get("h1") or 0)
        
        if quote_sym not in ("ETH", "WETH"):
            print(f"  [{i+1:2d}/{len(pools)}] SKIP {pid[:16]}... {base_sym}/{quote_sym} liq=${liq:,.0f}")
            continue
        if liq < LIQ_MIN:
            print(f"  [{i+1:2d}/{len(pools)}] SKIP {pid[:16]}... {base_sym}/{quote_sym} liq=${liq:,.0f} (<$100k)")
            continue
        
        print(f"  [{i+1:2d}/{len(pools)}] CHECK {pid[:16]}... {base_sym}/{quote_sym} liq=${liq:,.0f} vol1h=${vol_h1:,.0f} — fetching OHLCV")
        
        ohlcv_data = None
        for attempt in range(3):
            try:
                time.sleep(RATE)
                ohlcv_data = get(f"{GECKO}/pools/{paddr}/ohlcv/hour?aggregate=1&limit=120")
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    print(f"    429 on attempt {attempt+1}, retrying...")
                    time.sleep(RATE * (attempt + 1) * 2)
                else:
                    raise
            except Exception as e:
                print(f"    Error on attempt {attempt+1}: {e}")
                time.sleep(RATE * 2)
        
        if ohlcv_data is None:
            print(f"    FAIL: could not fetch OHLCV after retries")
            continue
        
        # Correct path: data -> attributes -> ohlcv_list (list of [ts, open, high, low, close, vol])
        try:
            ohlcv_list = ohlcv_data["data"]["attributes"]["ohlcv_list"]
        except (KeyError, TypeError) as e:
            print(f"    FAIL: unexpected OHLCV structure: {e}")
            print(f"    Got: {json.dumps(ohlcv_data, indent=2)[:300]}")
            continue
        
        if len(ohlcv_list) < EMA_S + 2:
            print(f"    WARN: only {len(ohlcv_list)} bars (need {EMA_S + 2}+)")
            continue
        
        # ohlcv_list items: [timestamp, open, high, low, close, volume]
        closes = [float(row[4]) for row in ohlcv_list]
        volumes = [float(row[5]) for row in ohlcv_list]
        
        ef = ema(closes, EMA_F)
        es = ema(closes, EMA_S)
        if not ef or not es:
            continue
        
        prev_f, curr_f = ef[-2], ef[-1]
        prev_s, curr_s = es[-2], es[-1]
        crossed = prev_f <= prev_s and curr_f > curr_s
        
        print(f"    EMA{EMA_F}={curr_f:.6f} EMA{EMA_S}={curr_s:.6f} prev_f={prev_f:.6f} prev_s={prev_s:.6f} cross={crossed} price=${closes[-1]:.6f}")
        
        if crossed:
            candidates.append({
                "pool_address": paddr,
                "token_address": paddr,
                "symbol": base_sym,
                "quote_symbol": quote_sym,
                "liquidity_usd": liq,
                "volume_5m": vol_5m,
                "volume_1h": vol_h1,
                "price_change_24h": pc_24h,
                "price_change_1h": pc_1h,
                "ema_crossover": True,
                "ema_fast": round(curr_f, 8),
                "ema_slow": round(curr_s, 8),
                "prev_ema_fast": round(prev_f, 8),
                "prev_ema_slow": round(prev_s, 8),
                "current_price": round(closes[-1], 8),
                "ohlcv_bars": len(ohlcv_list),
            })
        
        time.sleep(RATE)
    
    print(f"\n[*] Crossover candidates: {len(candidates)}")
    
    # Step 4+5: Score with Nous
    results = []
    alerts = []
    for idx, c in enumerate(candidates):
        print(f"\n[*] Scoring {c['symbol']} via Nous...")
        prompt = (
            "You are a crypto trading signal analyst. Score this token 0-100 for a BUY decision.\n\n"
            f"Token: {c['symbol']} (pool: {c['pool_address'][:20]}...)\n"
            f"Liquidity: ${c['liquidity_usd']:,.0f}\n"
            f"Volume 5m: ${c['volume_5m']:,.0f} | Volume 1h: ${c['volume_1h']:,.0f}\n"
            f"24h change: {c['price_change_24h']:+.2f}% | 1h change: {c.get('price_change_1h', 0):+.2f}%\n"
            f"EMA crossover: YES (fast EMA{EMA_F}={c['ema_fast']:.6f} crossed above slow EMA{EMA_S}={c['ema_slow']:.6f})\n"
            f"Prev: fast={c['prev_ema_fast']:.6f} slow={c['prev_ema_slow']:.6f}\n"
            f"Current price: ${c['current_price']:.6f}\n"
            f"OHLCV bars analyzed: {c['ohlcv_bars']}\n\n"
            "Respond with ONLY valid JSON: {\"score\": <int 0-100>, \"decision\": \"BUY\"/\"NO_GO\", \"reason\": \"<1-2 sentences>\"}\n"
            "Be conservative. Score >= 80 with BUY = strong signal. Consider liquidity, volume, momentum, and risk."
        )
        payload = {
            "model": NOUS_MODEL,
            "messages": [
                {"role": "system", "content": "You output ONLY valid JSON with exactly these keys: score (int 0-100), decision (BUY or NO_GO), reason (string). No other text, no markdown."},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 256,
            "temperature": 0.1,
        }
        scored = None
        for attempt in range(3):
            try:
                resp = post(NOUS_URL, payload)
                content = resp["choices"][0]["message"]["content"].strip()
                s = content.find("{")
                e = content.rfind("}") + 1
                if s >= 0 and e > s:
                    scored = json.loads(content[s:e])
                else:
                    scored = {"score": 50, "decision": "NO_GO", "reason": f"parse fail: {content[:60]}"}
                break
            except Exception as e:
                print(f"    Attempt {attempt+1} failed: {e}")
                time.sleep(2)
        
        if scored is None:
            scored = {"score": 0, "decision": "NO_GO", "reason": "Nous API unreachable after retries"}
        
        # Merge telemetry
        for k in ["symbol", "pool_address", "token_address", "liquidity_usd",
                   "volume_5m", "volume_1h", "price_change_24h", "price_change_1h",
                   "ema_crossover", "ema_fast", "ema_slow", "prev_ema_fast", "prev_ema_slow",
                   "current_price", "ohlcv_bars"]:
            scored[k] = c.get(k)
        
        print(f"    -> score={scored.get('score')} decision={scored.get('decision')} reason={scored.get('reason','')[:80]}")
        results.append(scored)
        
        if scored.get("score", 0) >= 80 and scored.get("decision") == "BUY":
            alerts.append(scored)
        
        time.sleep(1.5)
    
    # Step 5: Write
    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": NOUS_MODEL,
        "pools_scanned": len(pools),
        "candidates_with_crossover": len(candidates),
        "signals": results,
    }
    with open(OUT, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n[*] Wrote {len(results)} signals to {OUT}")
    
    # Step 6: ALERT
    if alerts:
        print(f"\n{'='*60}")
        print(f"🔔 ALERT — {len(alerts)} token(s) scored >=80 BUY:")
        print(f"{'='*60}")
        for a in alerts:
            print(f"  {a['symbol']}: score={a['score']} liq=${a['liquidity_usd']:,.0f}")
            print(f"    reason: {a.get('reason','')}\n")
    else:
        print(f"\n[*] No tokens met ALERT threshold (>=80 + BUY).")
    
    # Step 7: Compact summary
    summary = {
        "timestamp": output["timestamp"],
        "pools_scanned": len(pools),
        "crossovers_found": len(candidates),
        "signals": [
            {"symbol": r["symbol"], "score": r.get("score", 0),
             "decision": r.get("decision", "NO_GO"),
             "liq_usd": r.get("liquidity_usd", 0),
             "ema_fast": r.get("ema_fast"), "ema_slow": r.get("ema_slow")}
            for r in results
        ],
        "alerts": len(alerts),
    }
    print("\n" + json.dumps(summary, indent=2))

if __name__ == "__main__":
    main()
