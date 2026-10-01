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

print("→ Fetching trending pools page 1 …", flush=True)
pools_data = get("/trending_pools", {"page":1})
pools = pools_data.get("data", [])
print(f"  Got {len(pools)} pools", flush=True)

candidates = []
for p in pools:
    attrs = p.get("attributes", {})
    liq = attrs.get("total_volume_usd", 0) or 0
    base_sym = (attrs.get("base_token", {}) or {}).get("symbol","").upper()
    if liq >= LIQUIDITY_FILTER and base_sym in ("ETH","WETH"):
        candidates.append({
            "pool_address": p["id"],
            "symbol": attrs.get("token_codes",{}).get("base", attrs.get("base_token",{}).get("symbol","UNKNOWN")),
            "name": attrs.get("name",""),
            "liquidity_usd": liq,
            "base_token_address": attrs.get("base_token",{}).get("address",""),
            "quote_token_address": attrs.get("quote_token",{}).get("address",""),
        })
    time.sleep(RATE_LIMIT_S)

print(f"  Candidates (>=${LIQUIDITY_FILTER:,} liq, ETH/WETH base): {len(candidates)}", flush=True)

scored = []
for c in candidates:
    addr = c["pool_address"]
    print(f"  → OHLCV for {addr} …", flush=True)
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

nous_results = []
for c in candidates:
    if not c["bullish_crossover"]:
        continue
    addr = c["base_token_address"]
    print(f"  → Nous score for {c['symbol']} ({addr}) …", flush=True)
    try:
        detail = get(f"/pools/{c['pool_address']}", {})
        dattrs = detail.get("data",{}).get("attributes",{})
        vol_5m  = dattrs.get("volume_usd_5m", 0) or 0
        vol_1h  = dattrs.get("volume_usd_1h", 0) or 0
        price_chg = dattrs.get("price_change_24h", 0) or 0
    except Exception:
        vol_5m=vol_1h=price_chg=0

    prompt = (
        f"You are a crypto sentiment analyst. Score this token 0-100 and decide BUY/NO_GO.\n"
        f"Token address: {addr}\n"
        f"Symbol: {c['symbol']}\n"
        f"Liquidity USD: ${c['liquidity_usd']:,.0f}\n"
        f"Volume 5m USD: ${vol_5m:,.0f}\n"
        f"Volume 1h USD: ${vol_1h:,.0f}\n"
        f"Price change 24h: {price_chg:.2f}%\n"
        f"EMA(9): {c['ema9']:.6f}, EMA(21): {c['ema21']:.6f} — BULLISH CROSSOVER detected\n"
        f"Chain: Base (L2). Pair: {c['symbol']}/WETH.\n"
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

print("\n═══ RESULT ═══", flush=True)
alerts = [s for s in nous_results if s["score"] >= 80 and s["decision"] == "BUY"]
if alerts:
    for a in alerts:
        print(f"🚨 ALERT: {a['symbol']} ({a['token_address']}) — "
              f"score={a['score']} decision=BUY — {a['reason'][:120]}", flush=True)
else:
    print(json.dumps(out, indent=2), flush=True)
