#!/usr/bin/env python3
"""Nous sentiment gate v3 — correct GeckoTerminal response parsing."""
import urllib.request, urllib.error, json, time, sys, os

sys.setrecursionlimit(10000)

AUTH_PATH = os.path.expanduser("~/.hermes/shared/nous_auth.json")
OUT_PATH = os.path.expanduser("~/base-trend-bot/nous_signals.json")

with open(AUTH_PATH) as f:
    auth = json.load(f)
token = auth["access_token"]
inference_url = auth["inference_base_url"].rstrip("/")
print(f"Token valid until: {auth['expires_at']}")

# (pool_addr, symbol, base_token_addr) — ETH/WETH pairs >$100k
candidates = [
    ("0x7692acc1cdd771d09ebcae3663e1843b2911bec7", "STONKEX", "0x5ab000ff9b9ffe0349ce5ffa5fd86f217c3680f5"),
    ("0xf79478d5a6bae4546f7e489e80b2fc690b558944", "Basecat/WETH", "0xb2000000000000000000004c27f6523082f41d01"),
    ("0x5116773e18a9c7bb03ebb961b38678e45e238923", "DRB", "0x3ec2156d4c0a9cbdab4a016633b7bcf6a8d68ea2"),
    ("0x215b2146c3164aba3f5b18e8619b737c7f7dd11e", "Basecat/ETH", "0xb2000000000000000000004c27f6523082f41d01"),
    ("0xa6979745954ea4976d4c94a5957c225801de74b4ef", "STFY", "0xc6405d7a226e1c18e559be2f335f74c01ad07bf5"),
    ("0xd9edc75a3a797ec92ca370f19051babebfb2edee", "KTA", "0xc0634090f2fe6c6d75e61be2b949464abb498973"),
    ("0x2f335a05efe9c099a893e0150c5f74881d68f32d", "BST", "0xa46dfacc9129c62704b75c3e9914a1db3364ba5e"),
    ("0x4e829f8a5213c42535ab84aa40bd4adcce9cba02", "BRETT", "0x532f27101965dd16442e59d40670faf5ebb142e4"),
    ("0xec33256bf1ded407a57fd3c1965e7556e42ac14db", "GITLAWB", "0x5f980dcfc4c0fa3911554cf5ab288ed0eb13dba3"),
    ("0x01784ef301d79e4b2df3a21ad9a536d4cf09a5ce", "VVV", "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf"),
]

def fetch_json(url, retries=3):
    """Fetch JSON with retry + backoff on 429."""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "HermesBot/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            if e.code == 429 and attempt < retries - 1:
                wait = 10 * (attempt + 1)
                print(f"  429 (attempt {attempt+1}), wait {wait}s...")
                time.sleep(wait)
                continue
            print(f"  HTTP {e.code}: {body[:100]}")
            return None
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(5)
                continue
            print(f"  Error: {e}")
            return None
    return None

def parse_ohlcv(resp):
    """Extract close prices from GeckoTerminal OHLCV response.
    data = {type: 'ohlcv_request_response', attributes: {ohlcv_list: [[ts,open,high,low,close,vol], ...]}}
    """
    if resp is None:
        return None
    d = resp.get("data")
    if not d:
        # Try legacy format: data = list of {attributes: {close_price_usd: ...}}
        if isinstance(resp.get("data"), list):
            return [float(r["attributes"]["close_price_usd"]) for r in resp["data"] if "attributes" in r]
        return None
    attrs = d.get("attributes", {})
    ohlcv_list = attrs.get("ohlcv_list", [])
    if not ohlcv_list:
        return None
    # Each row: [timestamp, open, high, low, close, volume]
    closes = [row[4] for row in ohlcv_list if len(row) >= 5]
    return closes

def compute_ema(data, period):
    if len(data) < period:
        return None
    k = 2.0 / (period + 1)
    ema = [data[0]]
    for i in range(1, len(data)):
        ema.append(data[i] * k + ema[-1] * (1 - k))
    return ema

def detect_crossover(e9, e21):
    if e9 is None or e21 is None or len(e9) < 2 or len(e21) < 2:
        return False
    return e9[-2] <= e21[-2] and e9[-1] > e21[-1]

# ── Fetch OHLCV ──
print("\n=== Fetching OHLCV ({}) ===".format(len(candidates)))
ohlcv_data = {}
for addr, name, base_id in candidates:
    url = f"https://api.geckoterminal.com/api/v2/networks/base/pools/{addr}/ohlcv/hour?aggregate=1&limit=120"
    resp = fetch_json(url, retries=4)
    closes = parse_ohlcv(resp)
    if closes and len(closes) >= 21:
        ohlcv_data[addr] = {"name": name, "base_id": base_id, "closes": closes}
        print(f"OK  {name:18s}: {len(closes):3d} candles, close=${closes[-1]:.6f}")
    elif closes:
        print(f"WARN {name:18s}: only {len(closes)} candles (need >=21), skipping EMA")
    else:
        print(f"ERR {name:18s}: no OHLCV data")
    time.sleep(2.5)

print(f"\nOHLCV fetched: {len(ohlcv_data)}/{len(candidates)}")

# ── EMA analysis ──
print("\n=== EMA(9)/EMA(21) ===")
crossovers = []
for addr, info in ohlcv_data.items():
    c = info["closes"]
    e9 = compute_ema(c, 9)
    e21 = compute_ema(c, 21)
    cross = detect_crossover(e9, e21)
    info["ema9"] = e9
    info["ema21"] = e21
    info["cross"] = cross
    status = "🟢 CROSSOVER" if cross else "⚪"
    print(f"{info['name']:18s}: EMA9=${e9[-1]:.6f} EMA21=${e21[-1]:.6f} diff={(e9[-1]-e21[-1]):+.6f} → {status}")
    if cross:
        crossovers.append((addr, info))

print(f"\nCrossovers: {len(crossovers)}")

if not crossovers:
    result = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "scanned_pools": len(ohlcv_data),
        "crossovers": 0,
        "signals": [],
        "summary": "No bullish EMA(9)/EMA(21) crossovers in trending ETH/WETH pools."
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(result, f, indent=2)
    print(f"Written: {OUT_PATH}")
    print("Done — no signals.")
    sys.exit(0)

# ── Score via Nous ──
print("\n=== Nous API Scoring ===")
signals = []
for addr, info in crossovers:
    name = info["name"]
    base_id = info["base_id"]
    close_price = info["closes"][-1]

    # Pool stats
    liq_usd = vol_5m = vol_1h = price_change_24h = 0
    try:
        purl = f"https://api.geckoterminal.com/api/v2/networks/base/pools/{addr}"
        pr = fetch_json(purl, retries=2)
        if pr and pr.get("data"):
            pa = pr["data"].get("attributes", {})
            liq_usd = float(pa.get("reserve_in_usd", 0))
            v = pa.get("volume_usd", {})
            vol_5m = float(v.get("m5", 0))
            vol_1h = float(v.get("h1", 0))
            pc = pa.get("price_change_percentage", {})
            price_change_24h = float(pc.get("h24", 0))
        print(f"  {name}: liq=${liq_usd:,.0f} vol5m=${vol_5m:,.0f} vol1h=${vol_1h:,.0f} chg24h={price_change_24h:+.2f}%")
    except Exception as e:
        print(f"  {name}: stats fetch failed ({e})")

    tel = {
        "token_address": base_id,
        "pool_address": addr,
        "symbol": name,
        "liquidity_usd": round(liq_usd, 2),
        "volume_5m_usd": round(vol_5m, 2),
        "volume_1h_usd": round(vol_1h, 2),
        "price_change_24h_pct": round(price_change_24h, 2),
        "ema9": round(info["ema9"][-1], 6),
        "ema21": round(info["ema21"][-1], 6),
        "close_price_usd": round(close_price, 6),
        "ema_crossover": True,
    }

    prompt = f"""You are a crypto trading signal analyst for Base chain DeFi tokens. Score this EMA(9)/EMA(21) bullish crossover 0-100 on tradeability.

Telemetry:
{json.dumps(tel, indent=2)}

Return ONLY:
{{
  "score": <0-100 int>,
  "decision": "BUY" or "NO_GO",
  "reason": "<1-2 sentences>"
}}

Rules:
- 80+ = strong buy (deep liq, strong mom, clean cross)
- 60-79 = moderate, watch
- <60 = weak, NO_GO
- Conservative: false breakouts common in micro-caps
"""

    score = decision = reason = 0; decision = "NO_GO"; reason = "API error"
    try:
        body = json.dumps({
            "model": "upstage/solar-pro4:free",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 512,
            "temperature": 0.0,
        }).encode()
        req = urllib.request.Request(
            f"{inference_url}/chat/completions",
            data=body,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            rd = json.loads(resp.read())
        txt = rd["choices"][0]["message"]["content"].strip()
        s = txt.find("{"); e = txt.rfind("}") + 1
        if s >= 0 and e > s:
            rj = json.loads(txt[s:e])
            score = int(rj.get("score", 0))
            decision = rj.get("decision", "NO_GO")
            reason = rj.get("reason", "")
        else:
            reason = f"Parse error: {txt[:80]}"
        print(f"  ✅ {name}: score={score} {decision} | {reason}")
    except Exception as e:
        print(f"  ❌ {name}: {e}")
        score = 0; decision = "NO_GO"; reason = str(e)

    signals.append({
        "token_address": base_id,
        "pool_address": addr,
        "symbol": name,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ema_crossover": True,
        "ema9": round(info["ema9"][-1], 6),
        "ema21": round(info["ema21"][-1], 6),
        "close_price_usd": round(close_price, 6),
        "liquidity_usd": round(liq_usd, 2),
        "volume_5m_usd": round(vol_5m, 2),
        "volume_1h_usd": round(vol_1h, 2),
        "price_change_24h_pct": round(price_change_24h, 2),
        "nous_score": score,
        "nous_decision": decision,
        "nous_reason": reason,
    })
    time.sleep(3.0)

# ── Write ──
output = {
    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "scanned_pools": len(ohlcv_data),
    "crossovers_detected": len(crossovers),
    "signals_scored": len(signals),
    "signals": signals,
    "alerts": [s for s in signals if s["nous_score"] >= 80 and s["nous_decision"] == "BUY"],
}
os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
with open(OUT_PATH, "w") as f:
    json.dump(output, f, indent=2)

print(f"\n=== RESULTS ===")
print(f"Scanned={len(ohlcv_data)} Crossovers={len(crossovers)} Scored={len(signals)}")
print(f"Output: {OUT_PATH}")

if output["alerts"]:
    print(f"\n🔔 ALERT — {len(output['alerts'])} signal(s) with score>=80 AND BUY:")
    for a in output["alerts"]:
        print(f"  {a['symbol']} ({a['token_address'][:10]}...) score={a['nous_score']} | {a['nous_reason']}")
else:
    print("\nNo alerts (no score>=80 AND BUY)")

print("\n=== SIGNALS ===")
for s in signals:
    ic = "🟢" if s["nous_decision"] == "BUY" else "🔴"
    print(f"  {ic} {s['symbol']:18s} score={s['nous_score']:3d} {s['nous_decision']:5s} liq=${s['liquidity_usd']:>12,.0f} | {s['nous_reason'][:80]}")
print("\nDone.")
