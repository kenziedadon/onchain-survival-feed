#!/usr/bin/env python3
"""Nous sentiment gate - stdlib only. Scans GeckoTerminal trending pools for EMA crossovers, scores with Nous."""
import json, time, urllib.request, sys
from datetime import datetime, timezone

NOUS_AUTH_PATH = "/home/kenzodadon/.hermes/shared/nous_auth.json"
NOUS_MODEL = "upstage/solar-pro4:free"
NOUS_URL = "https://inference-api.nousresearch.com/v1/chat/completions"
GECKO_BASE = "https://api.geckoterminal.com/api/v2/networks/base"
OUTPUT_PATH = "/home/kenzodadon/base-trend-bot/nous_signals.json"
RATE_LIMIT = 1.5
LIQ_MIN = 100_000
EMA_FAST, EMA_SLOW = 9, 21

def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"accept": "application/json", "User-Agent": "HermesBot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def fetch_post(url, data, timeout=60):
    token = json.load(open(NOUS_AUTH_PATH))["access_token"]
    req = urllib.request.Request(url, data=json.dumps(data).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def calc_ema(prices, span):
    if len(prices) < span: return None
    k = 2.0 / (span + 1)
    s = sum(prices[:span]) / span
    out = [s]
    for p in prices[span:]:
        s = p * k + s * (1 - k)
        out.append(s)
    return out

def detect_cross(fast, slow):
    if len(fast) < 2 or len(slow) < 2: return False
    return fast[-2] <= slow[-2] and fast[-1] > slow[-1]

def main():
    print(f"[{datetime.now(timezone.utc).isoformat()}] Starting Nous sentiment gate")
    auth = json.load(open(NOUS_AUTH_PATH))
    print(f"[*] Auth token loaded, expires_at={auth.get('expires_at')}")
    
    print("[*] Fetching GeckoTerminal trending pools...")
    trending = fetch(f"{GECKO_BASE}/trending_pools?page=1")
    pools = trending.get("data", [])
    print(f"[*] Got {len(pools)} trending pools")
    
    candidates = []
    for i, pool in enumerate(pools):
        attrs = pool.get("attributes", {})
        pid = pool.get("id", "")
        pool_addr = pid.replace("base_", "") if pid.startswith("base_") else pid
        
        base_sym = attrs.get("base_token", {}).get("symbol", "").upper()
        quote_sym = attrs.get("quote_token", {}).get("symbol", "").upper()
        liq = float(attrs.get("reserve_in_usd") or 0)
        
        if quote_sym not in ("ETH", "WETH"):
            print(f"  [{i+1}/{len(pools)}] SKIP {pid[:16]}... base={base_sym} quote={quote_sym} liq=${liq:,.0f}")
            continue
        if liq < LIQ_MIN:
            print(f"  [{i+1}/{len(pools)}] SKIP {pid[:16]}... base={base_sym} quote={quote_sym} liq=${liq:,.0f} (<$100k)")
            continue
        
        print(f"  [{i+1}/{len(pools)}] CHECK {pid[:16]}... base={base_sym} quote={quote_sym} liq=${liq:,.0f} — fetching OHLCV")
        try:
            time.sleep(RATE_LIMIT)
            ohlcv = fetch(f"{GECKO_BASE}/pools/{pool_addr}/ohlcv/hour?aggregate=1&limit=120")
            candles = ohlcv.get("data", [])
            if len(candles) < EMA_SLOW + 2:
                print(f"    WARN: only {len(candles)} bars")
                continue
            closes = [float(c["attributes"]["price_usd"]) for c in candles]
            ema_f = calc_ema(closes, EMA_FAST)
            ema_s = calc_ema(closes, EMA_SLOW)
            if ema_f is None or ema_s is None:
                continue
            crossed = detect_cross(ema_f, ema_s)
            vol_5m = float(attrs.get("volume_usd", {}).get("m5") or 0)
            vol_1h = float(attrs.get("volume_usd", {}).get("h1") or 0)
            pc_24h = float(attrs.get("price_change_percentage", {}).get("h24") or 0)
            print(f"    EMA{FAST}={ema_f[-1]:.6f} EMA{SLOW}={ema_s[-1]:.6f} cross={crossed} vol1h=${vol_1h:,.0f}")
            if crossed:
                candidates.append({
                    "pool_address": pool_addr,
                    "token_address": attrs.get("base_token", {}).get("address", pool_addr),
                    "symbol": base_sym, "quote_symbol": quote_sym,
                    "liquidity_usd": liq, "volume_5m": vol_5m, "volume_1h": vol_1h,
                    "price_change_24h": pc_24h, "ema_crossover": True,
                    "ema_fast": round(ema_f[-1], 8), "ema_slow": round(ema_s[-1], 8),
                    "current_price": round(closes[-1], 8),
                })
        except Exception as e:
            print(f"    WARN OHLCV error: {e}")
        time.sleep(RATE_LIMIT)
    
    print(f"\n[*] Crossover candidates: {len(candidates)}")
    results = []
    alerts = []
    for idx, c in enumerate(candidates):
        print(f"\n[*] Scoring {c['symbol']} (pool {c['pool_address'][:10]}...) via Nous...")
        telemetry = {
            "token_address": c["token_address"], "symbol": c["symbol"],
            "liquidity_usd": c["liquidity_usd"],
            "volume_5m_usd": c["volume_5m"], "volume_1h_usd": c["volume_1h"],
            "price_change_24h_pct": c["price_change_24h"],
            "ema_crossover": c["ema_crossover"],
            "ema_fast": c["ema_fast"], "ema_slow": c["ema_slow"],
            "current_price_usd": c["current_price"],
        }
        prompt = ("You are a crypto trading signal analyst. Score this token 0-100 for a BUY decision.\n\n"
                  f"Token: {c['symbol']} (addr: {c['token_address']})\n"
                  f"Liquidity: ${c['liquidity_usd']:,.0f}\n"
                  f"Volume 5m: ${c['volume_5m']:,.0f} | Volume 1h: ${c['volume_1h']:,.0f}\n"
                  f"24h change: {c['price_change_24h']:+.2f}%\n"
                  f"EMA crossover: YES (fast={c['ema_fast']:.6f} slow={c['ema_slow']:.6f})\n"
                  f"Current price: ${c['current_price']:.6f}\n\n"
                  "Respond with ONLY JSON: {\"score\": <0-100>, \"decision\": \"BUY\"/\"NO_GO\", \"reason\": \"<1-2 sentences>\"}\n"
                  "Be conservative. Score >= 80 with BUY = strong signal.")
        
        payload = {"model": NOUS_MODEL, "messages": [
            {"role": "system", "content": "You output ONLY valid JSON with keys: score (int 0-100), decision (BUY/NO_GO), reason (string). No other text."},
            {"role": "user", "content": prompt},
        ], "max_tokens": 256, "temperature": 0.1}
        
        try:
            resp = fetch_post(NOUS_URL, payload)
            content = resp["choices"][0]["message"]["content"].strip()
            s = content.find("{"); e = content.rfind("}") + 1
            scored = json.loads(content[s:e]) if s >= 0 and e > s else {"score": 50, "decision": "NO_GO", "reason": f"parse fail: {content[:60]}"}
            scored.update({k: c[k] for k in ["symbol","pool_address","token_address","liquidity_usd","volume_5m","volume_1h","price_change_24h","ema_crossover","ema_fast","ema_slow","current_price"]})
            print(f"    -> score={scored.get('score')} decision={scored.get('decision')} reason={scored.get('reason','')[:80]}")
            results.append(scored)
            if scored.get("score", 0) >= 80 and scored.get("decision") == "BUY":
                alerts.append(scored)
        except Exception as e:
            print(f"    WARN Nous API error: {e}")
            results.append({"symbol": c["symbol"], "decision": "NO_GO", "score": 0,
                "reason": f"API error: {e}", "crossover": True,
                "pool_address": c["pool_address"], "token_address": c["token_address"],
                "liquidity_usd": c["liquidity_usd"], "volume_5m": c["volume_5m"],
                "volume_1h": c["volume_1h"], "price_change_24h": c["price_change_24h"],
                "ema_crossover": True, "ema_fast": c["ema_fast"], "ema_slow": c["ema_slow"],
                "current_price": c["current_price"]})
        time.sleep(1.0)
    
    output = {"timestamp": datetime.now(timezone.utc).isoformat(), "model": NOUS_MODEL,
              "pools_scanned": len(pools), "candidates_with_crossover": len(candidates), "signals": results}
    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n[*] Wrote {len(results)} signals to {OUTPUT_PATH}")
    
    if alerts:
        print(f"\n{'='*60}\n🔔 ALERT — {len(alerts)} token(s) scored >=80 BUY:\n{'='*60}")
        for a in alerts:
            print(f"  {a['symbol']}: score={a['score']} liq=${a['liquidity_usd']:,.0f}")
            print(f"    reason: {a.get('reason','')}\n")
    else:
        print(f"\n[*] No tokens met ALERT threshold (>=80 + BUY).")
    
    summary = {"timestamp": output["timestamp"], "pools_scanned": len(pools),
               "crossovers_found": len(candidates), "signals": [
                   {"symbol": r["symbol"], "score": r.get("score",0),
                    "decision": r.get("decision","NO_GO"), "liq_usd": r.get("liquidity_usd",0)}
               for r in results], "alerts": len(alerts)}
    print("\n" + json.dumps(summary, indent=2))

if __name__ == "__main__":
    main()
