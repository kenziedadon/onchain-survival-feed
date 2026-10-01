#!/usr/bin/env python3
"""
Nous Sentiment Gate v2 — uses trending pool data directly (no separate pool details call).
Stdlib only.
"""
import json, time, urllib.request, urllib.error, re
from datetime import datetime, timezone

BASE = "https://api.geckoterminal.com/api/v2"
NOUS_MODEL = "upstage/solar-pro4:free"
OUTPUT = "/home/kenzodadon/base-trend-bot/nous_signals.json"
AUTH = "/home/kenzodadon/.hermes/shared/nous_auth.json"
SLEEP = 1.5

def get(url, retries=5):
    headers = {"Accept": "application/json", "User-Agent": "HermesBaseTrend/2.0"}
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(5)
                continue
            return {"error_http": e.code}
        except Exception as e:
            return {"error_exc": str(e)[:150]}
    return {"error_exhausted": True}

def calc_ema(prices, period):
    if len(prices) < period:
        return None
    k = 2.0 / (period + 1)
    ema = prices[0]
    for p in prices[1:]:
        ema = p * k + ema * (1 - k)
    return ema

def ema_series(closes, period):
    out = []
    for i in range(len(closes)):
        e = calc_ema(closes[:i+1], period)
        if e is not None:
            out.append(e)
    return out

def crossover(fast, slow):
    if len(fast) < 2 or len(slow) < 2:
        return None
    pf, cf = fast[-2], fast[-1]
    ps, cs = slow[-2], slow[-1]
    return (pf <= ps and cf > cs, pf, ps, cf, cs)

def fetch_ohlcv(addr):
    url = f"{BASE}/networks/base/pools/{addr}/ohlcv/hour?aggregate=1&limit=120"
    d = get(url)
    time.sleep(SLEEP)
    if "error" in d or "data" not in d:
        return None
    return d["data"]

def call_nous(tele):
    auth = json.load(open(AUTH))
    tok = auth.get("access_token", "")
    base_url = auth.get("inference_base_url", "https://inference-api.nousresearch.com/v1")
    
    prompt = f"""Score this BUY signal 0-100. Output ONLY JSON: {{ "score": int, "decision": "BUY"|"NO_GO", "reason": "1 sentence" }}

Token: {tele['symbol']} ({tele['address'][:10]}...)
Liquidity: ${tele['liquidity_usd']:,.0f}
Price: ${tele['price']:.6f}
24h change: {tele['price_change_24h']:+.2f}%
1h change: {tele['price_change_1h']:+.2f}%
Volume 24h: ${tele.get('volume_24h_usd', 0):,.0f}
EMA9: {tele['ema9']:.4f} > EMA21: {tele['ema21']:.4f} (prev: EMA9 {tele['ema_prev_fast']:.4f} <= EMA21 {tele['ema_prev_slow']:.4f})
Crossover confirmed: YES

Conservative scoring — false positives cost real money.
"""
    payload = json.dumps({
        "model": NOUS_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 200,
        "temperature": 0.1
    }).encode()
    headers = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    try:
        req = urllib.request.Request(f"{base_url}/chat/completions", data=payload, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
        content = result["choices"][0]["message"]["content"]
        # Extract JSON
        m = re.search(r'\{[^}]*"score"\s*:\s*\d+[^}]*\}', content, re.DOTALL)
        if m:
            return json.loads(m.group())
        m2 = re.search(r'\{.*\}', content, re.DOTALL)
        if m2:
            return json.loads(m2.group())
        return {"score": 0, "decision": "NO_GO", "reason": f"Parse fail: {content[:80]}"}
    except Exception as e:
        return {"score": 0, "decision": "NO_GO", "reason": f"API error: {str(e)[:100]}"}

def main():
    ts = datetime.now(timezone.utc).isoformat()
    print(f"=== Nous Sentiment Gate v2 ({ts}) ===\n")
    
    # 1. Fetch trending pools
    print("[1] Fetching trending pools...")
    tr = get(f"{BASE}/networks/base/trending_pools?page=1")
    if "error" in tr:
        print(f"  FAILED: {tr}")
        json.dump({"timestamp": ts, "status": "error", "error": str(tr)}, open(OUTPUT, "w"), indent=2)
        return
    
    entries = tr.get("data", [])
    print(f"  Got {len(entries)} pools")
    
    # 2. Filter by liquidity (>$100k) using fdv_usd or market_cap_usd
    candidates = []
    for e in entries:
        addr = e["id"].replace("base_", "")  # strip prefix for OHLCV calls
        attrs = e.get("attributes", {})
        fdv = float(attrs.get("fdv_usd") or 0)
        mcap = float(attrs.get("market_cap_usd") or 0)
        liq = max(fdv, mcap)  # proxy for liquidity
        name = attrs.get("name", addr[:8])
        pc = attrs.get("price_change_percentage", {})
        
        candidates.append({
            "address": addr,
            "raw_id": e["id"],
            "name": name,
            "fdv_usd": fdv,
            "market_cap_usd": mcap,
            "liquidity_proxy": liq,
            "price_change": pc,
            "base_token_price_usd": float(attrs.get("base_token_price_usd") or 0),
            "quote_token_price_usd": float(attrs.get("quote_token_price_usd") or 0),
        })
    
    # Filter >$100k
    filtered = [c for c in candidates if c["liquidity_proxy"] > 100000]
    print(f"  Filtered to {len(filtered)} pools with >$100k (fdv/mcap proxy)")
    
    results = []
    alerts = []
    
    for i, c in enumerate(filtered):
        addr = c["address"]
        print(f"\n[{i+1}/{len(filtered)}] {c['name']} ({addr[:10]}...) fdv=${c['fdv_usd']:,.0f}")
        
        # Fetch OHLCV
        candles = fetch_ohlcv(addr)
        if not candles or len(candles) < 21:
            print(f"  Skip: OHLCV unavailable ({len(candles) if candles else 0} candles)")
            continue
        
        closes = [float(cd["attributes"]["price_usd_close"]) for cd in candles]
        
        fs = ema_series(closes, 9)
        ss = ema_series(closes, 21)
        
        if len(fs) < 2 or len(ss) < 2:
            print(f"  Skip: insufficient EMA data")
            continue
        
        x = crossover(fs, ss)
        if not x or not x[0]:
            print(f"  No bullish crossover (EMA9={fs[-1]:.4f}, EMA21={ss[-1]:.4f})")
            continue
        
        is_x, pf, ps, cf, cs = x
        print(f"  ✅ BULLISH CROSSOVER: EMA9 {cf:.4f} > EMA21 {cs:.4f}")
        print(f"     Prev: EMA9 {pf:.4f} <= EMA21 {ps:.4f}")
        
        price = closes[-1]
        price_1h = ((closes[-1] - closes[-2]) / closes[-2]) * 100 if len(closes) >= 2 else 0
        
        # 24h change
        pc = c["price_change"]
        p24 = float(pc.get("h24", pc.get("24h", 0) or 0))
        
        # Volume 24h from pool data if available
        vol24 = 0
        for cd in candles:
            v = cd["attributes"].get("volume_usd", 0)
            if v:
                vol24 += float(v)
        
        tele = {
            "address": addr,
            "symbol": c["name"].split("/")[0].strip(),
            "liquidity_usd": c["liquidity_proxy"],
            "price": price,
            "price_change_24h": p24,
            "price_change_1h": price_1h,
            "volume_24h_usd": vol24,
            "ema9": cf,
            "ema21": cs,
            "ema_prev_fast": pf,
            "ema_prev_slow": ps,
        }
        
        # Score via Nous
        print(f"  Scoring via Nous API...")
        nr = call_nous(tele)
        score = nr.get("score", 0)
        decision = nr.get("decision", "NO_GO")
        reason = nr.get("reason", "")
        print(f"  Nous: score={score}, {decision} — {reason}")
        
        signal = {**tele, "nous_score": score, "nous_decision": decision, "nous_reason": reason}
        results.append(signal)
        
        if score >= 80 and decision == "BUY":
            alerts.append(signal)
    
    # Write output
    out = {
        "timestamp": ts,
        "status": "success" if results else "no_signals",
        "scanned_pools": len(filtered),
        "crossovers_found": len(results),
        "alerts": len(alerts),
        "signals": results
    }
    json.dump(out, open(OUTPUT, "w"), indent=2)
    
    print(f"\n=== SUMMARY ===")
    print(f"Pools scanned: {len(filtered)}")
    print(f"Crossovers: {len(results)}")
    print(f"Alerts (>=80 + BUY): {len(alerts)}")
    
    if alerts:
        print("\n🚨 ALERTS 🚨")
        for a in alerts:
            print(f"  {a['symbol']} | score={a['nous_score']} | ${a['price']:.6f} | {a['nous_reason']}")
    else:
        print("\nNo high-confidence BUY signals.")
    
    # Compact summary
    summary = {
        "timestamp": ts,
        "scanned": len(filtered),
        "crossovers": len(results),
        "alerts": len(alerts),
        "signals": [{
            "address": s["address"],
            "symbol": s["symbol"],
            "score": s["nous_score"],
            "decision": s["nous_decision"],
            "liquidity": s["liquidity_usd"],
            "price": s["price"],
            "reason": s["nous_reason"]
        } for s in results]
    }
    print(f"\nCompact JSON:")
    print(json.dumps(summary, indent=2))

if __name__ == "__main__":
    main()
