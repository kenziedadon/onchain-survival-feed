
#!/usr/bin/env python3
"""Nous sentiment gate runner for base-trend-bot. Stdlib only."""

import json, urllib.request, urllib.parse, time, datetime, sys, re

# ---- helpers ----
def fetch_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent":"HermesNousSentimentGate/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def ema_list(closes, period):
    """Return list of EMA values aligned with closes (first period-1 are None)."""
    if len(closes) < period:
        return [None] * len(closes)
    k = 2.0 / (period + 1)
    result = [None] * (period - 1)
    ema = closes[0]
    for i in range(1, period):
        ema = closes[i] * k + ema * (1 - k)
    result.append(ema)
    for c in closes[period:]:
        ema = c * k + ema * (1 - k)
        result.append(ema)
    return result

# ---- step 1: auth ----
try:
    auth = json.load(open("/home/kenzodadon/.hermes/shared/nous_auth.json"))
    token = auth["access_token"]
    inference_base = auth.get("inference_base_url", "https://inference-api.nousresearch.com/v1")
    expires = datetime.datetime.fromisoformat(auth["expires_at"])
    now = datetime.datetime.now(datetime.timezone.utc)
    print(f"Token expires: {expires.isoformat()} | now: {now.isoformat()}", flush=True)
    if now >= expires:
        print("Token expired — attempting refresh...", flush=True)
        refresh_req = urllib.request.Request(
            f"{auth['portal_base_url'].replace('portal','inference')}/refresh",
            data=urllib.parse.urlencode({"refresh_token": auth["refresh_token"]}).encode(),
            headers={"Content-Type":"application/x-www-form-urlencoded"}
        )
        try:
            with urllib.request.urlopen(refresh_req, timeout=15) as rr:
                new_auth = json.loads(rr.read().decode())
            token = new_auth["access_token"]
            inference_base = new_auth.get("inference_base_url", inference_base)
            print("Token refreshed.", flush=True)
        except Exception as e:
            print(f"Refresh failed: {e}", flush=True)
            token = None
    else:
        print("Token valid.", flush=True)
except Exception as e:
    print(f"Auth read failed: {e}", flush=True)
    token = None

if not token:
    print("CRITICAL: No valid Nous token. Cannot proceed.", flush=True)
    sys.exit(1)

# ---- step 2: fetch trending pools ----
GEOCKO_BASE = "https://api.geckoterminal.com/api/v2/networks/base"
print("Fetching trending pools...", flush=True)
trending = fetch_json(f"{GEOCKO_BASE}/trending_pools?page=1")
pools = trending.get("data", [])
print(f"Fetched {len(pools)} trending pools.", flush=True)

# Filter: >$100k liquidity AND ETH/WETH base pair
CANDIDATES = []
for p in pools:
    try:
        attrs = p.get("attributes", {})
        liquidity = attrs.get("liquidity", {})
        liq_usd = liquidity.get("usd", 0) if isinstance(liquidity, dict) else 0
        base_token = attrs.get("base_token", {})
        base_sym = base_token.get("symbol","").upper()
        if base_sym not in ("ETH","WETH"):
            continue
        if liq_usd < 100_000:
            continue
        pool_addr = p.get("id","")
        CANDIDATES.append({
            "pool_address": pool_addr,
            "base_symbol": base_sym,
            "liquidity_usd": liq_usd,
            "attributes": attrs,
        })
    except Exception:
        continue

print(f"Candidates (>$100k, ETH/WETH): {len(CANDIDATES)}", flush=True)
for c in CANDIDATES:
    print(f"  pool={c['pool_address'][:20]}... base={c['base_symbol']} liq=${c['liquidity_usd']:,.0f}", flush=True)

# ---- step 3: OHLCV + EMA crossover ----
BUY_SIGNALS = []
for cand in CANDIDATES:
    pool = cand["pool_address"]
    print(f"\nFetching OHLCV for {pool[:20]}...", flush=True)
    try:
        time.sleep(1.5)
        ohlcv_data = fetch_json(f"{GEOCKO_BASE}/pools/{pool}/ohlcv/hour?aggregate=1&limit=120")
        rows = ohlcv_data.get("data", [])
        if not rows:
            print(f"  No OHLCV rows for {pool[:20]}", flush=True)
            continue
        closes = []
        for row in rows:
            try:
                close = float(row["attributes"]["price"]["usd"])
                closes.append(close)
            except (KeyError, ValueError, TypeError):
                continue
        if len(closes) < 21:
            print(f"  Not enough closes ({len(closes)}) for {pool[:20]}", flush=True)
            continue
        ema9 = ema_list(closes, 9)
        ema21 = ema_list(closes, 21)
        curr_fast = ema9[-1]
        curr_slow = ema21[-1]
        prev_fast = ema9[-2]
        prev_slow = ema21[-2]
        print(f"  closes={len(closes)} EMA9={curr_fast:.6f} EMA21={curr_slow:.6f} prev_fast={prev_fast:.6f} prev_slow={prev_slow:.6f}", flush=True)
        bullish = (prev_fast is not None and prev_slow is not None and
                   curr_fast is not None and curr_slow is not None and
                   prev_fast <= prev_slow and curr_fast > curr_slow)
        if bullish:
            print(f"  >>> BULLISH CROSSOVER detected!", flush=True)
            BUY_SIGNALS.append({**c, "ema9": curr_fast, "ema21": curr_slow,
                                 "closes": closes[-5:], "crossover": True})
        else:
            print(f"  No crossover.", flush=True)
    except Exception as e:
        print(f"  OHLCV fetch failed: {e}", flush=True)

print(f"\nBUY signals found: {len(BUY_SIGNALS)}", flush=True)

# ---- step 4: Nous scoring ----
RESULTS = []
ALERTS = []

for sig in BUY_SIGNALS:
    pool = sig["pool_address"]
    attrs = sig.get("attributes", {})
    vol_data = attrs.get("volume", {})
    vol_5m = vol_data.get("m5", {}) if isinstance(vol_data, dict) else {}
    vol_1h = vol_data.get("h1", {}) if isinstance(vol_data, dict) else {}
    price_change = attrs.get("price_change_percentage", {})
    telemetry = {
        "token_address": pool,
        "symbol": sig.get("base_symbol",""),
        "liquidity_usd": sig["liquidity_usd"],
        "volume_5m": vol_5m.get("usd", 0) if isinstance(vol_5m, dict) else 0,
        "volume_1h": vol_1h.get("usd", 0) if isinstance(vol_1h, dict) else 0,
        "price_change_24h": price_change.get("h24", 0) if isinstance(price_change, dict) else 0,
        "ema_crossover": True,
        "ema9": sig["ema9"],
        "ema21": sig["ema21"],
    }
    print(f"\nScoring {pool[:20]}... via Nous API", flush=True)
    try:
        time.sleep(1.0)
        payload = json.dumps({
            "model": "upstage/solar-pro4:free",
            "messages": [
                {"role":"system","content":"You are a crypto sentiment analyst. Score this EMA crossover signal 0-100. Decision: BUY if score>=70 and signal is genuine, else NO_GO. Output ONLY valid JSON: {\"score\": int, \"decision\": \"BUY\"|\"NO_GO\", \"reason\": \"short string\"}."},
                {"role":"user","content": json.dumps(telemetry)}
            ],
            "max_tokens": 200,
            "temperature": 0.1,
        }).encode()
        req = urllib.request.Request(
            f"{inference_base}/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp_data = json.loads(resp.read().decode())
        choices = resp_data.get("choices", [])
        if choices:
            content = choices[0].get("message", {}).get("content", "")
            score, decision, reason = None, "NO_GO", "parse error"
            try:
                idx = content.find("{")
                if idx >= 0:
                    parsed = json.loads(content[idx:])
                    score = parsed.get("score")
                    decision = parsed.get("decision", "NO_GO")
                    reason = parsed.get("reason", "")
            except json.JSONDecodeError:
                m = re.search(r'"score"\s*:\s*(\d+)', content)
                if m: score = int(m.group(1))
                m2 = re.search(r'"decision"\s*:\s*"([^"]+)"', content)
                if m2: decision = m2.group(1)
                m3 = re.search(r'"reason"\s*:\s*"([^"]+)"', content)
                if m3: reason = m3.group(1)
            result = {"score": score, "decision": decision, "reason": reason}
            RESULTS.append({**sig, "nous": result, "telemetry": telemetry})
            print(f"  Score={score} Decision={decision} Reason={reason}", flush=True)
            if score is not None and score >= 80 and decision == "BUY":
                ALERTS.append({**sig, "nous": result, "telemetry": telemetry})
                print(f"  *** ALERT: Score {score} >= 80 AND BUY ***", flush=True)
        else:
            print(f"  No choices from Nous API", flush=True)
            RESULTS.append({**sig, "nous": {"score": None, "decision": "NO_GO", "reason": "no_response"}, "telemetry": telemetry})
    except Exception as e:
        print(f"  Nous scoring failed: {e}", flush=True)
        RESULTS.append({**sig, "nous": {"score": None, "decision": "NO_GO", "reason": f"api_error:{str(e)}"}, "telemetry": telemetry})

# ---- step 5: write results ----
output = {
    "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "tokens_scanned": len(CANDIDATES),
    "buy_signals_found": len(BUY_SIGNALS),
    "signals_scored": len(RESULTS),
    "alerts": len(ALERTS),
    "signals": RESULTS,
    "alerts_detail": ALERTS,
}
outpath = "/home/kenzodadon/base-trend-bot/nous_signals.json"
with open(outpath, "w") as f:
    json.dump(output, f, indent=2)
print(f"\nWritten to {outpath}", flush=True)

# ---- step 6: ALERT or summary ----
if ALERTS:
    print("\n=== ALERT ===", flush=True)
    for a in ALERTS:
        t = a["telemetry"]
        print(f"  TOKEN: {a['pool_address']} ({a['base_symbol']})", flush=True)
        print(f"  SCORE: {a['nous']['score']} | DECISION: {a['nous']['decision']}", flush=True)
        print(f"  REASON: {a['nous']['reason']}", flush=True)
        print(f"  LIQ: ${t['liquidity_usd']:,.0f} | VOL_5m: ${t['volume_5m']:,.0f} | VOL_1h: ${t['volume_1h']:,.0f} | 24h CHG: {t['price_change_24h']:.2f}%", flush=True)
        print(f"  EMA9: {a['ema9']:.6f} > EMA21: {a['ema21']:.6f} (crossover)", flush=True)
        print(flush=True)
else:
    print(f"\n=== SUMMARY ===", flush=True)
    print(f"  Pools scanned (>$100k ETH/WETH): {len(CANDIDATES)}", flush=True)
    print(f"  EMA crossovers detected: {len(BUY_SIGNALS)}", flush=True)
    print(f"  Nous signals scored: {len(RESULTS)}", flush=True)
    print(f"  ALERTS (score>=80 AND BUY): {len(ALERTS)}", flush=True)
    for r in RESULTS:
        sn = r.get("nous", {})
        print(f"  - {r['pool_address'][:20]}... {r['base_symbol']} | score={sn.get('score')} decision={sn.get('decision')}", flush=True)
