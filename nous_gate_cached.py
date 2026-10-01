#!/usr/bin/env python3
"""Nous sentiment gate — use cached trending, OHLCV + EMA + Nous scoring."""
import json, urllib.request, urllib.error, time, sys, re
from datetime import datetime, timezone

CACHE = "/home/kenzodadon/base-trend-bot/current_trending.json"
OUTPUT = "/home/kenzodadon/base-trend-bot/nous_signals.json"
NOUS_URL = "https://inference-api.nousresearch.com/v1/chat/completions"
AUTH_PATH = "/home/kenzodadon/.hermes/shared/nous_auth.json"
RATE_SLEEP = 1.5
MIN_LIQ = 100_000

def log(msg):
    ts = datetime.now(timezone.utc).isoformat()
    line = f"[{ts}] {msg}"
    print(line, flush=True)

def load_auth():
    with open(AUTH_PATH) as f:
        return json.load(f)

def gecko_get(url, timeout=30, retries=3):
    req = urllib.request.Request(url, headers={"User-Agent": "HermesBaseTrendBot/1.0"})
    last_err = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            if e.code == 429 and attempt < retries - 1:
                wait = 20 * (attempt + 1)
                log(f"GECKO 429, wait {wait}s retry {attempt+1}/{retries}")
                time.sleep(wait)
                continue
            last_err = f"HTTP {e.code}: {body[:120]}"
            log(f"GECKO FAIL {url[:80]}: {last_err}")
            return None
        except Exception as e:
            last_err = str(e)
            log(f"GECKO ERR {url[:80]}: {e}")
            if attempt < retries - 1:
                time.sleep(5)
                continue
            return None
    return None

def calc_ema_series(prices, period):
    if len(prices) < period:
        return None
    k = 2.0 / (period + 1)
    result = [prices[0]]
    for p in prices[1:]:
        result.append(p * k + result[-1] * (1 - k))
    return result

def parse_ohlcv(data):
    attrs = data.get("data", {}).get("attributes", {})
    rows = attrs.get("ohlcv_list", [])
    closes = []
    for r in rows:
        if isinstance(r, (list, tuple)) and len(r) >= 5:
            try:
                closes.append(float(r[4]))
            except:
                pass
    return closes

def chronological(closes):
    if not closes:
        return closes
    # Gecko returns newest-first if first > last
    if len(closes) >= 2 and closes[0] > closes[-1]:
        return list(reversed(closes))
    return closes

def query_nous(prompt, token):
    body = json.dumps({
        "model": "upstage/solar-pro4:free",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 256,
        "temperature": 0.1,
    }).encode()
    req = urllib.request.Request(
        NOUS_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "HermesBaseTrendBot/1.0"
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        # extract JSON
        s = content.find("{")
        e = content.rfind("}")
        if s >= 0 and e > s:
            parsed = json.loads(content[s:e+1])
            return parsed.get("score", 0), parsed.get("decision", "NO_GO"), parsed.get("reason", "")
        return 0, "NO_GO", f"parse fail: {content[:80]}"
    except urllib.error.HTTPError as http_err:
        return 0, "NO_GO", f"HTTP {http_err.code}"
    except Exception as ex:
        return 0, "NO_GO", f"err: {str(ex)[:80]}"

def main():
    log("=== Nous Sentiment Gate (cached trending) ===")
    
    # Load auth
    auth = load_auth()
    token = auth.get("access_token", "")
    log(f"Auth: token present={bool(token)}, expires={auth.get('expires_at')}")
    
    # Load cached trending
    log(f"Loading cached trending from {CACHE}")
    with open(CACHE) as f:
        cached = json.load(f)
    pools = cached.get("data", [])
    log(f"Cached pools: {len(pools)}")
    
    # Filter candidates
    candidates = []
    for p in pools:
        a = p.get("attributes", {})
        liq = float(a.get("reserve_in_usd", 0) or 0)
        if liq < MIN_LIQ:
            continue
        name = a.get("name", "")
        parts = name.split("/") if name else []
        base_sym = parts[0].strip().upper() if len(parts) >= 1 else ""
        quote_sym = parts[1].strip().upper() if len(parts) >= 2 else ""
        is_eth = ("ETH" in base_sym or "WETH" in base_sym or "ETH" in quote_sym or "WETH" in quote_sym)
        if not is_eth:
            continue
        pool_addr = a.get("address", "")
        token_addr = p.get("relationships", {}).get("base_token", {}).get("data", {}).get("id", "").replace("base_", "")
        candidates.append({
            "pool_address": p.get("id", ""),
            "token_address": token_addr,
            "base_symbol": base_sym,
            "quote_symbol": quote_sym,
            "liquidity_usd": liq,
            "pool_raw_address": pool_addr,
            "attributes": a,
        })
        log(f"  CANDIDATE: {base_sym}/{quote_sym} liq=${liq:,.0f} pool={pool_addr[:20]}...")
    
    log(f"Candidates (>=${MIN_LIQ:,.0f} ETH/WETH): {len(candidates)}")
    
    if not candidates:
        log("No candidates. Writing empty result.")
        out = {"timestamp": datetime.now(timezone.utc).isoformat(), "scanned": 0, "crossover_count": 0, "scored": 0, "alerts": 0, "signals": []}
        with open(OUTPUT, "w") as f:
            json.dump(out, f, indent=2)
        log(json.dumps(out, indent=2))
        return
    
    # OHLCV + EMA scan
    log("Step: OHLCV + EMA scan")
    ohlcv_results = []
    for idx, c in enumerate(candidates):
        addr = c["pool_raw_address"]
        url = f"https://api.geckoterminal.com/api/v2/networks/base/pools/{addr}/ohlcv/hour?aggregate=1&limit=120"
        log(f"[{idx+1}/{len(candidates)}] OHLCV {c['base_symbol']} ({addr[:16]}...)")
        data = gecko_get(url)
        time.sleep(RATE_SLEEP)
        if not data:
            log(f"  No OHLCV for {c['base_symbol']}")
            continue
        closes = parse_ohlcv(data)
        if len(closes) < 21:
            log(f"  Only {len(closes)} candles, need 21+")
            continue
        chrono = chronological(closes)
        ema9 = calc_ema_series(chrono, 9)
        ema21 = calc_ema_series(chrono, 21)
        if not ema9 or not ema21:
            log(f"  EMA calc failed")
            continue
        curr9, curr21 = ema9[-1], ema21[-1]
        prev9 = ema9[-2] if len(ema9) >= 2 else None
        prev21 = ema21[-2] if len(ema21) >= 2 else None
        cross = (prev9 is not None and prev21 is not None and prev9 <= prev21 and curr9 > curr21)
        r = {
            **c,
            "close_latest": chrono[-1],
            "ema9": curr9, "ema21": curr21,
            "prev_ema9": prev9, "prev_ema21": prev21,
            "crossover": cross,
            "candles_used": len(chrono),
        }
        ohlcv_results.append(r)
        log(f"  {c['base_symbol']}: close={chrono[-1]:.6f} EMA9={curr9:.6f} EMA21={curr21:.6f} cross={cross}")
    
    # BUY crossovers
    buys = [r for r in ohlcv_results if r["crossover"]]
    log(f"BUY crossovers: {len(buys)}")
    for b in buys:
        log(f"  BUY: {b['base_symbol']} EMA9={b['ema9']:.6f} > EMA21={b['ema21']:.6f}")
    
    # Score via Nous
    log("Step: Nous scoring")
    signals = []
    alerts = []
    for b in buys:
        a = b["attributes"]
        vol = a.get("volume_usd", {})
        vol5 = float(vol.get("m5", 0) or 0)
        vol1 = float(vol.get("h1", 0) or 0)
        chg24 = float(a.get("price_change_percentage", {}).get("h24", 0) or 0)
        tel = {
            "token_address": b["token_address"],
            "symbol": b["base_symbol"],
            "liquidity_usd": b["liquidity_usd"],
            "volume_5m": vol5,
            "volume_1h": vol1,
            "price_change_24h": chg24,
            "ema_crossover": {"ema9": b["ema9"], "ema21": b["ema21"], "close": b["close_latest"]},
        }
        prompt = (
            f"Base chain EMA crossover signal for {b['base_symbol']} "
            f"(token {b['token_address']}). Liq=${b['liquidity_usd']:,.0f}, "
            f"5m vol=${vol5:,.0f}, 1h vol=${vol1:,.0f}, "
            f"24h chg={chg24:.2f}%, "
            f"EMA9={b['ema9']:.6f} > EMA21={b['ema21']:.6f}, close={b['close_latest']:.6f}. "
            f"Score 0-100 (buy conviction). Output ONLY JSON: "
            f'{{"score":<int>,"decision":"BUY"|"NO_GO","reason":"<text>"}}.'
        )
        log(f"  Scoring {b['base_symbol']} via Nous...")
        score, decision, reason = query_nous(prompt, token)
        log(f"  => {b['base_symbol']}: score={score}, decision={decision}, reason={reason[:100]}")
        sig = {
            **b,
            "nous_score": score,
            "nous_decision": decision,
            "nous_reason": reason,
            "telemetry": tel,
        }
        signals.append(sig)
        if score is not None and score >= 80 and decision == "BUY":
            alerts.append(sig)
        time.sleep(RATE_SLEEP)
    
    # Write results
    out = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "scanned_candidates": len(candidates),
        "ohlcv_scanned": len(ohlcv_results),
        "buy_crossovers": len(buys),
        "nous_scored": len(signals),
        "signals": signals,
    }
    with open(OUTPUT, "w") as f:
        json.dump(out, f, indent=2)
    log(f"Written to {OUTPUT}")
    
    # Alert check
    log("Step: ALERT check")
    if alerts:
        log("")
        log("🚨 ALERT: High-conviction BUY signals (score>=80 AND decision=BUY):")
        for a in alerts:
            log(f"  {a['base_symbol']} (score={a['nous_score']}, {a['nous_reason'][:120]})")
    else:
        log("No high-conviction BUY signals (score>=80 AND decision=BUY).")
    
    # Compact summary
    summary_signals = []
    for s in signals:
        summary_signals.append({
            "symbol": s["base_symbol"],
            "token": s["token_address"][:20] + ("..." if len(s["token_address"]) > 20 else ""),
            "liq_usd": round(s["liquidity_usd"]),
            "ema9": round(s["ema9"], 6),
            "ema21": round(s["ema21"], 6),
            "crossover": s["crossover"],
            "score": s["nous_score"],
            "decision": s["nous_decision"],
            "reason": (s["nous_reason"] or "")[:100],
        })
    
    summary = {
        "timestamp": out["timestamp"],
        "scanned": out["scanned_candidates"],
        "crossover_count": out["buy_crossovers"],
        "scored": out["nous_scored"],
        "alerts": len(alerts),
        "signals": summary_signals,
    }
    log("")
    log("=== Compact JSON Summary ===")
    log(json.dumps(summary, indent=2))
    log("=== Done ===")
    print(json.dumps(summary, indent=2), flush=True)

if __name__ == "__main__":
    main()
