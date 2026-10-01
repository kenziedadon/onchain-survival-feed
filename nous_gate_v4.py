#!/usr/bin/env python3
"""
Nous sentiment gate v4 — corrected:
  - OHLCV path: /networks/base/pools/{addr}/ohlcv/hour
  - OHLCV field: ohlcv_list (not ohlcv_series)
  - Pool detail for token addresses
Stdlib only.
"""
import json, time, urllib.request, urllib.error, datetime, re, sys

GT_BASE = "https://api.geckoterminal.com/api/v2/networks/base"
NOUS_MODEL = "upstage/solar-pro4:free"
NOUS_URL = "https://inference-api.nousresearch.com/v1/chat/completions"
AUTH_FILE = "/home/kenzodadon/.hermes/shared/nous_auth.json"
OUT_FILE  = "/home/kenzodadon/base-trend-bot/nous_signals.json"
RATE_SLEEP = 1.5

def gt_get(path, retries=4):
    url = f"{GT_BASE}{path}"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "HermesBot/1.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 5 * (attempt + 1) + (attempt * 2)
                print(f"    429, waiting {wait}s...", flush=True)
                time.sleep(wait)
                continue
            if e.code == 404:
                return None
            raise
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(3)
    return None

def ema(values, span):
    if len(values) < span:
        return [None] * len(values)
    k = 2.0 / (span + 1)
    out = [None] * (span - 1)
    s = sum(values[:span]) / span
    out.append(s)
    for v in values[span:]:
        s = v * k + s * (1 - k)
        out.append(s)
    return out

def load_auth():
    with open(AUTH_FILE) as f:
        return json.load(f)

def call_nous(payload_body):
    auth = load_auth()
    token = auth["access_token"]
    body = json.dumps(payload_body).encode()
    req = urllib.request.Request(
        NOUS_URL, data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())

# ── step 1: fetch trending pools ────────────────────────────────────
print("[1/6] Fetching trending pools...", flush=True)
trending = gt_get("/trending_pools?page=1")
time.sleep(RATE_SLEEP)
pools_raw = trending.get("data", [])
print(f"  Got {len(pools_raw)} trending pools", flush=True)

# ── step 2: filter >100k reserve + ETH/WETH in pool name ───────────
# Pool detail needed to get token addresses, so fetch details in step 3.
candidates = []
for p in pools_raw:
    attrs = p.get("attributes", {})
    pool_id = p.get("id", "")
    addr = pool_id.replace("base_", "")
    pool_name = attrs.get("name", "")
    reserve = float(attrs.get("reserve_in_usd") or 0)

    if reserve < 100_000:
        continue
    if "ETH" not in pool_name.upper():
        continue

    candidates.append({
        "pool_address": addr,
        "pool_id": pool_id,
        "pool_name": pool_name,
        "reserve_usd": reserve,
    })

print(f"  Filtered to {len(candidates)} ETH/WETH pools >$100k reserve", flush=True)
for c in candidates:
    print(f"    {c['pool_name']:35s} reserve=${c['reserve_usd']:>12,.0f}  {c['pool_address'][:10]}...", flush=True)

# ── step 3: pool detail + OHLCV for each candidate ──────────────────
signals = []
for i, c in enumerate(candidates):
    print(f"\n  [{i+1}/{len(candidates)}] {c['pool_name']}", flush=True)

    # Pool detail for token addresses
    try:
        detail = gt_get(f"/pools/{c['pool_address']}")
        time.sleep(RATE_SLEEP)
    except Exception as e:
        print(f"    Pool detail failed: {e}", flush=True)
        continue
    if detail is None:
        print(f"    Pool detail not found", flush=True)
        continue

    rel = detail.get("data", {}).get("relationships", {})
    base_tok = rel.get("base_token", {}).get("data", {})
    quote_tok = rel.get("quote_token", {}).get("data", {})
    base_addr = base_tok.get("id", "").replace("base_", "")
    quote_addr = quote_tok.get("id", "").replace("base_", "")

    c["base_token_address"] = base_addr
    c["quote_token_address"] = quote_addr
    print(f"    Base: {base_addr[:10]}...  Quote: {quote_addr[:10]}...", flush=True)

    # OHLCV
    ohlcv = gt_get(f"/pools/{c['pool_address']}/ohlcv/hour?aggregate=1&limit=120")
    time.sleep(RATE_SLEEP)

    if ohlcv is None:
        print(f"    OHLCV not available", flush=True)
        continue

    ohlcv_data = ohlcv.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
    if not ohlcv_data or len(ohlcv_data) < 21:
        print(f"    Not enough OHLCV ({len(ohlcv_data)} rows)", flush=True)
        continue

    # ohlcv_list rows: [timestamp, open, high, low, close, volume]
    closes = [row[4] for row in ohlcv_data]
    vols   = [row[5] for row in ohlcv_data]
    ema9  = ema(closes, 9)
    ema21 = ema(closes, 21)

    if None in ema9[-2:] or None in ema21[-2:]:
        print(f"    EMA warmup incomplete", flush=True)
        continue

    curr_fast, prev_fast = ema9[-1], ema9[-2]
    curr_slow, prev_slow = ema21[-1], ema21[-2]
    bullish = (prev_fast <= prev_slow and curr_fast > curr_slow)

    print(f"    EMA9={curr_fast:.6f} EMA21={curr_slow:.6f} close={closes[-1]:.6f} cross={'BULL' if bullish else 'no'}", flush=True)

    if not bullish:
        continue

    # 24h change
    if len(closes) >= 25:
        p_24h = closes[-25]
        price_change_24h = ((closes[-1] - p_24h) / p_24h) * 100
    else:
        price_change_24h = 0.0

    volume_1h = vols[-1] if vols else 0
    volume_5m = volume_1h / 12

    signals.append({
        "token_address": base_addr,
        "symbol": c["pool_name"].split("/")[0].strip(),
        "pool_address": c["pool_address"],
        "pool_name": c["pool_name"],
        "liquidity_usd": c["reserve_usd"],
        "volume_5m": round(volume_5m, 2),
        "volume_1h": round(volume_1h, 2),
        "price_change_24h": round(price_change_24h, 2),
        "ema_crossover": True,
        "ema9": round(curr_fast, 6),
        "ema21": round(curr_slow, 6),
    })
    print(f"    → Added to signals", flush=True)

print(f"\n[3/6] {len(signals)} bullish crossovers detected", flush=True)

# ── step 4: Nous scoring ────────────────────────────────────────────
now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
scored = []

for idx, sig in enumerate(signals):
    print(f"\n[4/6] Nous scoring {sig['symbol']} ({idx+1}/{len(signals)})...", flush=True)

    msgs = [
        {"role": "system", "content": (
            "You are a crypto sentiment analyst for Base chain DEX tokens. "
            "Given telemetry on a token showing a bullish EMA(9)/EMA(21) crossover, "
            "score the trade setup 0-100 and decide BUY or NO_GO. "
            "Be strict — most crossovers fail. Return ONLY compact JSON with keys: "
            "score (int 0-100), decision (BUY or NO_GO), reason (short string)."
        )},
        {"role": "user", "content": json.dumps({
            "token_address": sig["token_address"],
            "symbol": sig["symbol"],
            "pool_name": sig["pool_name"],
            "liquidity_usd": sig["liquidity_usd"],
            "volume_5m": sig["volume_5m"],
            "volume_1h": sig["volume_1h"],
            "price_change_24h_pct": sig["price_change_24h"],
            "ema9": sig["ema9"],
            "ema21": sig["ema21"],
            "ema_crossover": sig["ema_crossover"],
        }, indent=2)},
    ]

    try:
        resp = call_nous({
            "model": NOUS_MODEL,
            "messages": msgs,
            "max_tokens": 300,
            "temperature": 0.3,
        })
        content = resp["choices"][0]["message"]["content"]
        parsed = None
        try:
            parsed = json.loads(content.strip())
        except:
            m = re.search(r'\{[\s\S]*\}', content)
            if m:
                try:
                    parsed = json.loads(m.group())
                except:
                    pass
        if parsed and "score" in parsed and "decision" in parsed:
            parsed["score"] = int(parsed["score"])
            parsed["decision"] = parsed["decision"].upper()
            scored.append({**sig, **parsed})
            print(f"    Score={parsed['score']} Decision={parsed['decision']} Reason={parsed.get('reason','')[:80]}", flush=True)
        else:
            print(f"    Parse failed: {content[:200]}", flush=True)
            scored.append({**sig, "score": 0, "decision": "NO_GO", "reason": "parse_error"})
    except Exception as e:
        print(f"    Nous API error: {e}", flush=True)
        scored.append({**sig, "score": 0, "decision": "NO_GO", "reason": f"api_error: {e}"})

    if idx < len(signals) - 1:
        time.sleep(1)

# ── step 5: write ───────────────────────────────────────────────────
result = {
    "timestamp": now_utc,
    "model": NOUS_MODEL,
    "pools_scanned": len(candidates),
    "crossovers_found": len(signals),
    "signals_scored": len(scored),
    "signals": scored,
}
with open(OUT_FILE, "w") as f:
    json.dump(result, f, indent=2)
print(f"\n[5/6] Wrote {OUT_FILE}", flush=True)

# ── step 6: alert or summary ────────────────────────────────────────
alerts = [s for s in scored if s.get("score", 0) >= 80 and s.get("decision") == "BUY"]
print(f"\n{'='*60}", flush=True)
if alerts:
    print("⚠️  ALERT — HIGH-CONVICTION BUY SIGNALS", flush=True)
    for a in alerts:
        print(f"  {a['symbol']} (score={a['score']}, liq=${a['liquidity_usd']:,.0f})", flush=True)
        print(f"    {a.get('reason','')[:120]}", flush=True)
else:
    print(f"✓ No high-conviction signals (score≥80 + BUY).", flush=True)
    print(f"  Scanned {len(candidates)} ETH/WETH pools, {len(signals)} crossovers, {len(scored)} scored.", flush=True)
    if scored:
        for s in sorted(scored, key=lambda x: -x.get("score",0)):
            print(f"    {s['symbol']:12s} score={s.get('score',0):3d} {s.get('decision',''):6s} liq=${s['liquidity_usd']:>12,.0f} 24h={s['price_change_24h']:+.1f}% EMA9={s.get('ema9')} EMA21={s.get('ema21')}", flush=True)
        top = max(scored, key=lambda x: x.get("score", 0))
        print(f"  Highest: {top['symbol']} = {top.get('score',0)} ({top.get('decision','')})", flush=True)

print(f"\n[6/6] Compact summary:", flush=True)
print(json.dumps({
    "timestamp": now_utc,
    "pools_scanned": len(candidates),
    "crossovers": len(signals),
    "scored": len(scored),
    "alerts": len(alerts),
    "signals": [
        {
            "symbol": s["symbol"],
            "score": s.get("score", 0),
            "decision": s.get("decision", ""),
            "liquidity_usd": s["liquidity_usd"],
            "ema9": s.get("ema9"),
            "ema21": s.get("ema21"),
            "reason": s.get("reason", "")[:80],
        }
        for s in scored
    ],
}, indent=2))
