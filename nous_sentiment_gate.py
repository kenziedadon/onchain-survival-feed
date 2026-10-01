#!/usr/bin/env python3
# Nous sentiment gate for base-trend-bot (cron) - stdlib only
import json, time, os, urllib.request, datetime, re, sys

BASE = "https://api.geckoterminal.com/api/v2/networks/base"
WETH = "0x4200000000000000000000000000000000000006"

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

def http_get_json(url, timeout=30, headers=None, retries=4):
    last = None
    hdrs = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:
                time.sleep(6.0 * (i + 1))  # respect rate limit, back off
            else:
                time.sleep(2.0 * (i + 1))
        except Exception as e:
            last = e
            time.sleep(2.0 * (i + 1))
    raise last

# ---------- 1. auth token ----------
token = None
auth_path = os.path.expanduser("~/.hermes/shared/nous_auth.json")
try:
    raw = open(auth_path).read().strip()
    try:
        auth = json.loads(raw)
        if isinstance(auth, dict):
            for k in ("token", "api_key", "key", "access_token", "auth_token"):
                if auth.get(k):
                    token = auth[k]
                    break
        else:
            token = auth
    except json.JSONDecodeError:
        token = raw
except Exception as e:
    print("AUTH_ERR:", e, file=sys.stderr)

# ---------- 2. trending pools ----------
try:
    tp = http_get_json(f"{BASE}/trending_pools?page=1")
    pools = tp.get("data", [])
except Exception as e:
    print("POOLS_ERR:", e, file=sys.stderr)
    pools = []

qual = []
for p in pools:
    a = p.get("attributes", {})
    addr = a.get("address")
    name = a.get("name") or "?"
    bt = a.get("base_token_address") or ""
    qt = a.get("quote_token_address") or ""
    liq = float(a.get("reserve_in_usd") or 0)
    vol = a.get("volume_usd") or {}
    pchg = a.get("price_change_percentage") or {}
    if addr is None or liq < 100000:
        continue
    is_eth = bt.lower() == WETH or qt.lower() == WETH or "weth" in name.lower() or "eth/" in name.lower()
    if not is_eth:
        continue
    qual.append((addr, name, liq, vol, pchg))

qual.sort(key=lambda x: -x[2])
qual = qual[:12]

# ---------- 3. EMAs ----------
def ema_series(values, period):
    if len(values) < period + 1:
        return None
    k = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    out = [ema]
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
        out.append(ema)
    return out

crossovers = []
for addr, name, liq, vol, pchg in qual:
    time.sleep(1.5)
    try:
        o = http_get_json(f"{BASE}/pools/{addr}/ohlcv/hour?aggregate=1&limit=120")
        ohlcv = o.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
    except Exception as e:
        print(f"OHLCV_ERR {addr}: {e}", file=sys.stderr)
        continue
    closes = [row[4] for row in ohlcv if row and len(row) > 4 and isinstance(row[4], (int, float))]
    if len(closes) < 30:
        continue
    f, s = ema_series(closes, 9), ema_series(closes, 21)
    if not f or not s or len(f) < 2:
        continue
    if f[-2] <= s[-2] and f[-1] > s[-1]:
        crossovers.append({
            "token_address": addr,
            "symbol": name.split("/")[0],
            "liquidity_usd": round(liq, 2),
            "volume_5m": vol.get("m5", {}).get("usd", 0),
            "volume_1h": vol.get("h1", {}).get("usd", 0),
            "price_change_24h": pchg.get("h24", 0),
            "ema_crossover": True,
            "prev_fast": round(f[-2], 8), "prev_slow": round(s[-2], 8),
            "curr_fast": round(f[-1], 8), "curr_slow": round(s[-1], 8),
        })

# ---------- 4. Nous scoring ----------
NOUS_URL = "https://inference.nousresearch.com/v1/chat/completions"
scored = []
for c in crossovers:
    if not token:
        scored.append({**c, "score": None, "decision": "NO_NOUS_TOKEN", "reason": "auth token missing"})
        continue
    payload = {
        "model": "upstage/solar-pro4:free",
        "messages": [
            {"role": "system", "content": 'You are a cautious crypto sentiment analyst for Base-chain momentum entries. Respond with ONLY valid JSON: {"score": <int 0-100>, "decision": "BUY"|"NO_GO", "reason": "<short reason>"}. High liquidity and strong momentum raise score; janky low-volume pumps lower it.'},
            {"role": "user", "content": "Assess this token telemetry: " + json.dumps(c)}
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
    }
    try:
        req = urllib.request.Request(NOUS_URL, data=json.dumps(payload).encode(),
                                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read().decode())
        content = resp["choices"][0]["message"]["content"]
        m = re.search(r"\{.*\}", content, re.S)
        parsed = json.loads(m.group(0)) if m else {}
        scored.append({**c, "score": parsed.get("score"), "decision": parsed.get("decision"),
                       "reason": parsed.get("reason")})
    except Exception as e:
        scored.append({**c, "score": None, "decision": "ERROR", "reason": str(e)[:120]})

# ---------- 5. write results ----------
out = {
    "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "pools_qualified": [{"address": a, "name": n, "liquidity_usd": round(l, 2)} for a, n, l, _, _ in qual],
    "crossovers_found": len(crossovers),
    "signals": scored,
}
os.makedirs(os.path.expanduser("~/base-trend-bot"), exist_ok=True)
with open(os.path.expanduser("~/base-trend-bot/nous_signals.json"), "w") as fh:
    json.dump(out, fh, indent=2)

# ---------- 6/7. output ----------
alerts = [s for s in scored if s.get("score") is not None and s.get("score", 0) >= 80 and s.get("decision") == "BUY"]
summary = {
    "timestamp": out["timestamp"],
    "pools_qualified": len(qual),
    "crossovers": len(crossovers),
    "scored": [{k: s.get(k) for k in ("symbol", "token_address", "liquidity_usd", "volume_1h", "price_change_24h", "score", "decision", "reason")} for s in scored],
    "alerts": alerts,
}
print(json.dumps(summary, indent=2, default=str))