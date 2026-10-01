#!/usr/bin/env python3
"""Nous sentiment gate v2 — with retry+backoff for rate-limited GeckoTerminal."""
import urllib.request, json, time, sys, os

sys.setrecursionlimit(10000)

AUTH_PATH = os.path.expanduser("~/.hermes/shared/nous_auth.json")
OUT_PATH = os.path.expanduser("~/base-trend-bot/nous_signals.json")

with open(AUTH_PATH) as f:
    auth = json.load(f)
token = auth["access_token"]
inference_url = auth["inference_base_url"].rstrip("/")
print(f"Token valid until: {auth['expires_at']}")

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
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "HermesBot/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                wait = 10 * (attempt + 1)
                print(f"  429 hit, waiting {wait}s... (attempt {attempt+1}/{retries})")
                time.sleep(wait)
                continue
            raise
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(5)
                continue
            raise
    return None

def compute_ema(data, period):
    if len(data) < period:
        return None
    k = 2.0 / (period + 1)
    ema = [data[0]]
    for i in range(1, len(data)):
        ema.append(data[i] * k + ema[-1] * (1 - k))
    return ema

def detect_crossover(ema_fast, ema_slow):
    if ema_fast is None or ema_slow is None:
        return False
    if len(ema_fast) < 2 or len(ema_slow) < 2:
        return False
    return ema_fast[-2] <= ema_slow[-2] and ema_fast[-1] > ema_slow[-1]

print("\n=== Fetching OHLCV ===")
ohlcv_data = {}
for addr, name, base_id in candidates:
    try:
        url = f"https://api.geckoterminal.com/api/v2/networks/base/pools/{addr}/ohlcv/hour?aggregate=1&limit=120"
        data = fetch_json(url, retries=4)
        if data is None:
            print(f"SKIP {name} — all retries exhausted")
            continue
        rows = data.get("data", [])
        closes = [float(r["attributes"]["close_price_usd"]) for r in rows]
        ohlcv_data[addr] = {"name": name, "base_id": base_id, "closes": closes, "raw": data}
        print(f"OK  {name:18s}: {len(closes):3d} candles, close=${closes[-1]:.6f}")
    except Exception as e:
        print(f"ERR {name:18s}: {e}")
    time.sleep(2.5)

print(f"\nOHLCV: {len(ohlcv_data)}/{len(candidates)} pools fetched")

# EMA analysis
print("\n=== EMA(9)/EMA(21) Analysis ===")
crossovers = []
for addr, info in ohlcv_data.items():
    closes = info["closes"]
    ema9 = compute_ema(closes, 9)
    ema21 = compute_ema(closes, 21)
    cross = detect_crossover(ema9, ema21)
    info["ema9"] = ema9
    info["ema21"] = ema21
    info["crossover"] = cross
    if ema9 and ema21:
        print(f"{info['name']:18s}: EMA9=${ema9[-1]:.6f} EMA21=${ema21[-1]:.6f} → {'🟢 CROSSOVER' if cross else '⚪ no'}")
    else:
        print(f"{info['name']:18s}: insufficient data (need >=21 candles, have {len(closes)})")

    if cross:
        crossovers.append((addr, info))

print(f"\nCrossovers: {len(crossovers)}")

# If no crossovers, write result and exit
if not crossovers:
    result = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "scanned_pools": len(ohlcv_data),
        "crossovers": 0,
        "signals": [],
        "summary": "No bullish EMA(9)/EMA(21) crossovers detected in trending ETH/WETH pools."
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(result, f, indent=2)
    print(f"Written to {OUT_PATH}")
    sys.exit(0)

# Score via Nous API
print("\n=== Scoring via Nous API ===")
signals = []
for addr, info in crossovers:
    name = info["name"]
    base_id = info["base_id"]
    close_price = info["closes"][-1]

    try:
        pool_url = f"https://api.geckoterminal.com/api/v2/networks/base/pools/{addr}"
        pool_data = fetch_json(pool_url, retries=2)
        if pool_data is None:
            raise Exception("pool stats fetch failed")
        attrs = pool_data["data"]["attributes"]
        liq_usd = float(attrs["reserve_in_usd"])
        vol_5m = float(attrs["volume_usd"]["m5"])
        vol_1h = float(attrs["volume_usd"]["h1"])
        price_change_24h = float(attrs["price_change_percentage"]["h24"])
        print(f"  Stats for {name}: liq=${liq_usd:,.0f} vol5m=${vol_5m:,.0f} vol1h=${vol_1h:,.0f} chg24h={price_change_24h:.2f}%")
    except Exception as e:
        print(f"  Warning: stats fetch failed for {name}: {e}")
        liq_usd = vol_5m = vol_1h = price_change_24h = 0

    telemetry = {
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
        "ohlcv_count": len(info["closes"]),
    }

    prompt = f"""You are a crypto trading signal analyst for Base chain DeFi tokens. Analyze the following EMA(9)/EMA(21) bullish crossover signal and score it 0-100 on tradeability.

Telemetry:
{json.dumps(telemetry, indent=2)}

Return ONLY a JSON object with exactly these fields:
{{
  "score": <0-100 integer>,
  "decision": "BUY" or "NO_GO",
  "reason": "<brief 1-2 sentence explanation>"
}}

Scoring:
- 80+ = strong buy (high liquidity, strong momentum, clean crossover)
- 60-79 = moderate, watch
- <60 = weak signal, NO_GO
- Consider: liquidity depth, volume relative to market cap, 24h momentum, crossover clarity
- Conservative: false breakouts common in micro-cap DeFi
"""

    try:
        req_body = json.dumps({
            "model": "upstage/solar-pro4:free",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 512,
            "temperature": 0.0,
        }).encode()

        req = urllib.request.Request(
            f"{inference_url}/chat/completions",
            data=req_body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "HermesBot/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp_data = json.loads(resp.read())
        content = resp_data["choices"][0]["message"]["content"].strip()
        sf = content.find("{")
        ef = content.rfind("}") + 1
        if sf >= 0 and ef > sf:
            rj = json.loads(content[sf:ef])
            score = int(rj.get("score", 0))
            decision = rj.get("decision", "NO_GO")
            reason = rj.get("reason", "")
        else:
            score, decision, reason = 0, "NO_GO", f"Parse error: {content[:100]}"
        print(f"  {name}: score={score} → {decision} | {reason}")
    except Exception as e:
        print(f"  ERROR scoring {name}: {e}")
        score, decision, reason = 0, "NO_GO", f"API error: {str(e)}"

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
    time.sleep(3.0)  # between-signal delay

# Write output
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
print(f"Scanned: {len(ohlcv_data)} | Crossovers: {len(crossovers)} | Scored: {len(signals)}")
print(f"File: {OUT_PATH}")

if output["alerts"]:
    print(f"\n🔔 ALERT — {len(output['alerts'])} signal(s) with score>=80 AND BUY:")
    for a in output["alerts"]:
        print(f"  {a['symbol']} ({a['token_address'][:10]}...) score={a['nous_score']} | {a['nous_reason']}")
else:
    print("\nNo alerts (no token with score>=80 AND BUY)")

print("\n=== ALL SIGNALS ===")
for s in signals:
    icon = "🟢" if s["nous_decision"] == "BUY" else "🔴"
    print(f"  {icon} {s['symbol']:18s} score={s['nous_score']:3d} {s['nous_decision']:5s} liq=${s['liquidity_usd']:>12,.0f} vol5m=${s['volume_5m_usd']:>10,.0f} | {s['nous_reason'][:80]}")
print("\nDone.")
