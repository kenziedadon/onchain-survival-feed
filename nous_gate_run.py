
import json
import time
import subprocess
import re
from datetime import datetime, timezone

OUTPUT_PATH = "/home/kenzodadon/base-trend-bot/nous_signals.json"
NOUS_AUTH_PATH = "/home/kenzodadon/.hermes/shared/nous_auth.json"
NOUS_MODEL = "upstage/solar-pro4:free"
RATE_LIMIT_S = 1.5
LIQ_THRESHOLD = 100_000

with open(NOUS_AUTH_PATH) as f:
    auth = json.load(f)
token = auth["access_token"]
inference_base = auth.get("inference_base_url", "https://inference-api.nousresearch.com/v1")
print(f"Auth loaded, expires_at={auth.get('expires_at')}")

def curl_get(url, timeout=30):
    cmd = ["curl", "-s", "-S", "-H", "Accept: application/json",
           "-H", "User-Agent: Mozilla/5.0", "--max-time", str(timeout), url]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
    if r.returncode != 0:
        raise Exception(f"curl: {r.stderr}")
    return json.loads(r.stdout)

# --- Fetch trending pools ---
print("\n[1] Fetching trending pools...")
pools_data = curl_get("https://api.geckoterminal.com/api/v2/networks/base/trending_pools?page=1")
pools = pools_data.get("data", [])
print(f"  Got {len(pools)} trending pools")

# --- Filter: ETH/WETH pair, >$100k liq ---
candidate_pools = []
for pool in pools:
    try:
        attrs = pool.get("attributes", {})
        name = attrs.get("name", "")
        liquidity = float(attrs.get("reserve_in_usd", 0) or 0)
        parts = name.split(" / ")
        if len(parts) != 2:
            continue
        sym0 = parts[0].strip().split(" ")[0]
        sym1 = parts[1].strip().split(" ")[0].split(" ")[0]
        dual = f"{sym0}/{sym1}"
        if "ETH" not in dual:
            continue
        if liquidity < LIQ_THRESHOLD:
            continue
        # Strip "base_" prefix for OHLCV endpoint
        raw_addr = pool["id"].replace("base_", "", 1) if pool["id"].startswith("base_") else pool["id"]
        candidate_pools.append({
            "pool_id": pool["id"],
            "raw_addr": raw_addr,
            "dual": dual,
            "sym0": sym0,
            "sym1": sym1,
            "name": name,
            "liquidity_usd": liquidity,
            "attributes": attrs,
        })
    except Exception as e:
        print(f"  Skip {pool.get('id','?')}: {e}")

print(f"\n[2] {len(candidate_pools)} pools (>{LIQ_THRESHOLD:,} USD, ETH/WETH)")
for p in candidate_pools:
    print(f"  {p['dual']} — ${p['liquidity_usd']:,.0f}")

# --- EMA crossover ---
def calc_ema(data, period):
    if len(data) < period:
        return None
    k = 2 / (period + 1)
    ema = data[0]
    for p in data[1:]:
        ema = p * k + ema * (1 - k)
    return ema

def fetch_ohlcv(raw_addr, retries=2):
    url = f"https://api.geckoterminal.com/api/v2/networks/base/pools/{raw_addr}/ohlcv/hour?aggregate=1&limit=120"
    for attempt in range(retries + 1):
        try:
            data = curl_get(url)
            # Handle various error shapes
            if "errors" in data:
                print(f"    API errors: {data['errors']}")
                if attempt < retries:
                    time.sleep(2)
                    continue
                return []
            if "status" in data and data.get("status") != "success":
                print(f"    Status: {data.get('status')} — {data.get('message', '')}")
                if attempt < retries:
                    time.sleep(2)
                    continue
                return []
            if "data" not in data:
                print(f"    Unexpected response keys: {list(data.keys())}")
                if attempt < retries:
                    time.sleep(2)
                    continue
                return []
            ohlcv = data["data"].get("attributes", {}).get("ohlcv_list", [])
            closes = [row[4] for row in ohlcv if len(row) >= 5]
            if not closes:
                print(f"    Empty ohlcv_list" + (" — retrying..." if attempt < retries else " — giving up"))
                if attempt < retries:
                    time.sleep(2)
                    continue
            return closes
        except Exception as e:
            print(f"    Exception: {e}")
            if attempt < retries:
                time.sleep(2)
                continue
            return []

signals = []
crossover_candidates = []

for i, pool in enumerate(candidate_pools):
    print(f"\n[3] {i+1}/{len(candidate_pools)}: {pool['dual']}")
    time.sleep(RATE_LIMIT_S)
    try:
        closes = fetch_ohlcv(pool["raw_addr"])
        if len(closes) < 21:
            print(f"  {len(closes)} OHLCV points, need 21+, skip")
            continue
        ema9 = calc_ema(closes, 9)
        ema21 = calc_ema(closes, 21)
        if ema9 is None or ema21 is None:
            print(f"  EMA fail, skip")
            continue
        prev_fast = calc_ema(closes[:-1], 9)
        prev_slow = calc_ema(closes[:-1], 21)
        is_cross = (prev_fast is not None and prev_slow is not None
                    and prev_fast <= prev_slow and ema9 > ema21)
        print(f"  EMA9={ema9:.6f} EMA21={ema21:.6f}  prev EMA9={prev_fast:.6f} EMA21={prev_slow:.6f}  cross={is_cross}")
        sig = {
            "pool_id": pool["pool_id"],
            "dual": pool["dual"],
            "sym0": pool["sym0"],
            "sym1": pool["sym1"],
            "liquidity_usd": pool["liquidity_usd"],
            "ema9": round(ema9, 6),
            "ema21": round(ema21, 6),
            "ema_crossover": is_cross,
            "ohlcv_points": len(closes),
            "attributes": pool["attributes"],
        }
        signals.append(sig)
        if is_cross:
            crossover_candidates.append(sig)
            print(f"  >>> BUY CROSSOVER")
    except Exception as e:
        print(f"  Error: {e}")

print(f"\n[4] Crossovers: {len(crossover_candidates)}")

# --- Nous scoring ---
def call_nous(payload):
    url = f"{inference_base}/chat/completions"
    body = {
        "model": NOUS_MODEL,
        "messages": [
            {"role": "system", "content": "You are a crypto trading signal evaluator. Output ONLY JSON: {\"score\": 0-100, \"decision\": \"BUY\"|\"NO_GO\", \"reason\": \"short string\"}. Be honest — most signals should be NO_GO."},
            {"role": "user", "content": json.dumps(payload)},
        ],
        "max_tokens": 256,
        "temperature": 0.3,
    }
    data = json.dumps(body).encode()
    cmd = ["curl", "-s", "-S", "-H", f"Authorization: Bearer {token}",
           "-H", "Content-Type: application/json", "-d", data, "--max-time", "60", url]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=70)
    if r.returncode != 0:
        raise Exception(f"curl: {r.stderr}")
    return json.loads(r.stdout)["choices"][0]["message"]["content"]

nous_results = []
for cand in crossover_candidates:
    print(f"\n[5] Scoring {cand['dual']} via Nous...")
    time.sleep(1)
    attrs = cand.get("attributes", {})
    vol = attrs.get("volume_usd", {})
    pch = attrs.get("price_change_percentage", {})
    payload = {
        "token_address": cand["pool_id"],
        "symbol": cand["dual"],
        "liquidity_usd": cand["liquidity_usd"],
        "volume_5m": vol.get("m5", 0),
        "volume_1h": vol.get("h1", 0),
        "price_change_24h": pch.get("h24", 0),
        "ema_crossover": True,
        "ema9": cand["ema9"],
        "ema21": cand["ema21"],
    }
    try:
        resp = call_nous(payload)
        m = re.search(r'\{[^}]+\}', resp.strip())
        parsed = json.loads(m.group()) if m else {"score": 0, "decision": "NO_GO", "reason": "Unparseable"}
        print(f"  Nous: score={parsed.get('score')}, decision={parsed.get('decision')}, reason={parsed.get('reason','')[:80]}")
        parsed.update({
            "pool_id": cand["pool_id"], "dual": cand["dual"],
            "liquidity_usd": cand["liquidity_usd"],
            "ema9": cand["ema9"], "ema21": cand["ema21"],
            "ema_crossover": True,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        nous_results.append(parsed)
    except Exception as e:
        print(f"  Error: {e}")
        nous_results.append({
            "pool_id": cand["pool_id"], "dual": cand["dual"],
            "score": 0, "decision": "NO_GO",
            "reason": f"API error: {str(e)[:100]}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

# --- Write ---
output = {
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "total_pools_scanned": len(candidate_pools),
    "crossover_count": len(crossover_candidates),
    "nous_scored": len(nous_results),
    "signals": signals,
    "nous_results": nous_results,
}
with open(OUTPUT_PATH, "w") as f:
    json.dump(output, f, indent=2)
print(f"\n[6] Written to {OUTPUT_PATH}")

alerts = [r for r in nous_results if r.get("score", 0) >= 80 and r.get("decision") == "BUY"]
print(f"\n[7] ALERTS: {len(alerts)}")
for a in alerts:
    print(f"  🚨 {a['dual']} — score={a['score']}, decision={a['decision']}, reason={a.get('reason','')[:100]}")

summary = {
    "timestamp": output["timestamp"],
    "pools_scanned": len(candidate_pools),
    "crossovers": len(crossover_candidates),
    "nous_scored": len(nous_results),
    "alerts": [{"dual": a["dual"], "score": a["score"], "decision": a["decision"], "reason": a.get("reason","")[:80]} for a in alerts],
    "all_scores": [{"dual": r["dual"], "score": r.get("score",0), "decision": r.get("decision","NO_GO")} for r in nous_results],
}

print("\n=== SUMMARY ===")
print(json.dumps(summary, indent=2))
