import json, urllib.request, urllib.error, time, ssl, os, re
from datetime import datetime, timezone

GECKO_BASE = "https://api.geckoterminal.com/api/v2/networks/base"
NOUS_URL   = "https://inference-api.nousresearch.com/v1/chat/completions"
NOUS_MODEL = "upstage/solar-pro4:free"

auth_path = os.path.expanduser("~/.hermes/shared/nous_auth.json")
with open(auth_path) as f:
    auth = json.load(f)
    NOUS_TOKEN = auth["access_token"]

LIQUIDITY_FILTER = 100_000
RATE_LIMIT_S    = 1.5
OUT_PATH        = os.path.expanduser("~/base-trend-bot/nous_signals.json")
ctx = ssl.create_default_context()

def get(path, params=None):
    url = GECKO_BASE + path
    if params:
        qs = "&".join(f"{k}={v}" for k,v in params.items())
        url += "?" + qs
    req = urllib.request.Request(url, headers={"User-Agent":"HermesAgent/1.0"})
    with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
        return json.loads(r.read())

def post_nous(payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(NOUS_URL, data=data,
        headers={"Authorization": f"Bearer {NOUS_TOKEN}",
                 "Content-Type":"application/json"})
    with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
        return json.loads(r.read())

def ema(values, span):
    k = 2.0 / (span + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append((v - out[-1]) * k + out[-1])
    return out

def crossover(fast, slow):
    if len(fast) < 2 or len(slow) < 2:
        return False
    return fast[-2] <= slow[-2] and fast[-1] > slow[-1]

# Resolve token symbol from the token id (base_0x...) by fetching token endpoint
_TOKEN_CACHE = {}
def resolve_symbol(token_id):
    if token_id in _TOKEN_CACHE:
        return _TOKEN_CACHE[token_id]
    # token_id format: "base_0x..."
    try:
        tok = get(f"/tokens/{token_id}", {})
        attrs = tok.get("data",{}).get("attributes",{})
        sym = attrs.get("symbol","UNKNOWN")
    except Exception:
        sym = "UNKNOWN"
    _TOKEN_CACHE[token_id] = sym
    return sym

print("→ Fetching trending pools page 1 …", flush=True)
pools_data = get("/trending_pools", {"page":1})
pools = pools_data.get("data", [])
print(f"  Got {len(pools)} pools", flush=True)

candidates = []
for p in pools:
    a = p.get("attributes",{})
    rel = p.get("relationships",{})
    base_id   = (rel.get("base_token",{}) or {}).get("data",{}).get("id","")
    quote_id  = (rel.get("quote_token",{}) or {}).get("data",{}).get("id","")
    reserve_raw = a.get("reserve_in_usd", 0) or 0
    try:
        reserve = float(reserve_raw)
    except (TypeError, ValueError):
        reserve = 0
    vol24_raw  = (a.get("volume_usd",{}) or {}).get("h24", 0) or 0
    try:
        vol24 = float(vol24_raw)
    except (TypeError, ValueError):
        vol24 = 0

    if reserve >= LIQUIDITY_FILTER:
        base_sym = resolve_symbol(base_id)
        print(f"  Pool {p['id'][:25]}... base={base_sym} ({base_id[:20]}...) reserve=${reserve:,.0f} vol24h=${vol24:,.0f}", flush=True)
        candidates.append({
            "pool_address": p["id"],
            "symbol": base_sym,
            "name": a.get("name",""),
            "liquidity_usd": reserve,
            "base_token_address": base_id,
            "quote_token_address": quote_id,
            "vol_24h": vol24,
        })
    time.sleep(RATE_LIMIT_S)

print(f"\n  Candidates (>=${LIQUIDITY_FILTER:,} reserve, any base): {len(candidates)}", flush=True)
if not candidates:
    print("  No pools passed the filter. Check reserve_in_usd values above.", flush=True)
    # Fall through to write empty result
    now = datetime.now(timezone.utc).isoformat()
    out = {
        "timestamp": now,
        "candidates_scanned": 0,
        "bullish_crossovers": 0,
        "signals": [],
        "note": "No pools with reserve_in_usd >= $100k found on GeckoTerminal trending page 1.",
    }
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\n✓ Written {OUT_PATH} (empty)", flush=True)
    print(json.dumps(out, indent=2), flush=True)
    raise SystemExit(0)

# ── OHLCV + EMA for each candidate ─────────────────────────────────────
scored = []
for c in candidates:
    addr = c["pool_address"]
    print(f"\n  → OHLCV for {c['symbol']} ({addr[:40]}…) …", flush=True)
    bull = False
    try:
        ohlcv = get(f"/pools/{addr}/ohlcv/hour", {"aggregate":1,"limit":120})
        rows = ohlcv.get("data",[])
        if len(rows) < 21:
            print(f"    ⚠  insufficient bars ({len(rows)}), skip", flush=True)
            time.sleep(RATE_LIMIT_S)
            continue
        closes = [r["attributes"]["price_close"] for r in rows]
        ema9  = ema(closes, 9)
        ema21 = ema(closes, 21)
        bull  = crossover(ema9, ema21)
        c["ema9"] = round(ema9[-1], 6)
        c["ema21"] = round(ema21[-1], 6)
        c["bullish_crossover"] = bull
        print(f"    close={closes[-1]:.6f} ema9={ema9[-1]:.6f} ema21={ema21[-1]:.6f} bull={bull}", flush=True)
    except Exception as e:
        print(f"    ⚠  OHLCV error: {e}", flush=True)
        c["bullish_crossover"] = False
    time.sleep(RATE_LIMIT_S)

# ── Nous scoring for bullish crossovers ─────────────────────────────────
nous_results = []
for c in candidates:
    if not c["bullish_crossover"]:
        continue
    addr = c["base_token_address"]
    print(f"\n  → Nous score for {c['symbol']} ({addr[:40]}…) …", flush=True)
    try:
        detail = get(f"/pools/{c['pool_address']}", {})
        dattrs = detail.get("data",{}).get("attributes",{})
        vol_5m  = (dattrs.get("volume_usd",{}) or {}).get("m5", 0) or 0
        vol_1h  = (dattrs.get("volume_usd",{}) or {}).get("h1", 0) or 0
        pc = dattrs.get("price_change_percentage",{}) or {}
        price_chg = pc.get("h24", 0) or 0
    except Exception:
        vol_5m=vol_1h=price_chg=0

    prompt = (
        f"You are a crypto sentiment analyst. Score this token 0-100 and decide BUY/NO_GO.\n"
        f"Token address: {addr}\n"
        f"Symbol: {c['symbol']}\n"
        f"Liquidity USD (reserve): ${c['liquidity_usd']:,.0f}\n"
        f"Volume 5m USD: ${vol_5m:,.0f}\n"
        f"Volume 1h USD: ${vol_1h:,.0f}\n"
        f"Price change 24h: {price_chg:.2f}%\n"
        f"EMA(9): {c['ema9']:.6f}, EMA(21): {c['ema21']:.6f} — BULLISH CROSSOVER detected\n"
        f"Chain: Base (L2). Pair: {c['symbol']}/ETH (or quoted token).\n"
        f"Return JSON ONLY with keys: score (int 0-100), decision (BUY or NO_GO), reason (string)."
    )

    try:
        r = post_nous({
            "model": NOUS_MODEL,
            "messages":[{"role":"user","content":prompt}],
            "max_tokens":300, "temperature":0.0,
        })
        content = r["choices"][0]["message"]["content"]
        m = re.search(r'\{.*\}', content, re.DOTALL)
        js = json.loads(m.group()) if m else {}
        js.setdefault("score", 0)
        js.setdefault("decision", "NO_GO")
        js.setdefault("reason", content.strip())
        js["token_address"] = addr
        js["symbol"] = c["symbol"]
        js["liquidity_usd"] = c["liquidity_usd"]
        js["ema9"] = c["ema9"]
        js["ema21"] = c["ema21"]
        js["volume_5m"] = vol_5m
        js["volume_1h"] = vol_1h
        js["price_change_24h"] = price_chg
        js["ema_crossover"] = True
        nous_results.append(js)
        print(f"    score={js['score']} decision={js['decision']} reason={js['reason'][:80]}", flush=True)
    except Exception as e:
        print(f"    ⚠  Nous error: {e}", flush=True)
        nous_results.append({
            "token_address": addr, "symbol": c["symbol"],
            "score":0, "decision":"NO_GO", "reason":f"API error: {e}",
            "liquidity_usd":c["liquidity_usd"], "ema9":c["ema9"], "ema21":c["ema21"],
            "volume_5m":vol_5m, "volume_1h":vol_1h,
            "price_change_24h":price_chg, "ema_crossover":True,
        })

# ── Write results ───────────────────────────────────────────────────────
now = datetime.now(timezone.utc).isoformat()
out = {
    "timestamp": now,
    "candidates_scanned": len(candidates),
    "bullish_crossovers": sum(1 for c in candidates if c["bullish_crossover"]),
    "signals": nous_results,
}
with open(OUT_PATH, "w") as f:
    json.dump(out, f, indent=2)
print(f"\n✓ Written {OUT_PATH}", flush=True)

# ── Alert or summary ────────────────────────────────────────────────────
print("\n═══ RESULT ═══", flush=True)
alerts = [s for s in nous_results if s["score"] >= 80 and s["decision"] == "BUY"]
if alerts:
    for a in alerts:
        print(f"🚨 ALERT: {a['symbol']} ({a['token_address']}) — "
              f"score={a['score']} decision=BUY — {a['reason'][:120]}", flush=True)
else:
    print(json.dumps(out, indent=2), flush=True)
