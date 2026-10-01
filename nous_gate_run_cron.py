#!/usr/bin/env python3
"""Nous sentiment gate for base-trend-bot — stdlib only."""
import json, urllib.request, urllib.error, time, re, os
from datetime import datetime, timezone

GECKO = "https://api.geckoterminal.com/api/v2/networks/base"
NOUS_BASE = "https://inference-api.nousresearch.com/v1"
NOUS_MODEL = "upstage/solar-pro4:free"
OUT = "/home/kenzodadon/base-trend-bot/nous_signals.json"
MIN_LIQ = 100_000
SLEEP = 1.5

# --- auth ---
with open("/home/kenzodadon/.hermes/shared/nous_auth.json") as f:
    auth = json.load(f)
token = auth["access_token"]
exp = auth["expires_at"]
print(f"[1] Token loaded, expires={exp}")

def get(url, t=30):
    req = urllib.request.Request(url, headers={"User-Agent":"btb/1.0"})
    with urllib.request.urlopen(req, timeout=t) as r:
        return json.loads(r.read().decode())

# --- trending pools ---
print("[2] Fetching trending pools...")
tdata = get(f"{GECKO}/trending_pools?page=1")
pools = tdata.get("data", [])
print(f"    {len(pools)} pools returned")

def ok(pool):
    a = pool.get("attributes",{})
    liq = a.get("liquidity",{}).get("usd") or 0
    if liq < MIN_LIQ: return False
    sym = (pool.get("base_token") or {}).get("symbol","").upper()
    return sym in ("ETH","WETH")

cands = [p for p in pools if ok(p)]
print(f"[2] Qualifying (ETH/WETH, >$100k): {len(cands)}")

if not cands:
    out = {"timestamp":datetime.now(timezone.utc).isoformat(),"scanned":0,"signals":[],"alerts":[]}
    open(OUT,"w").write(json.dumps(out,indent=2))
    print(f"    Written {OUT} (empty).")
    raise SystemExit(0)

# --- EMA ---
def ema(prices, period):
    if len(prices) < period: return None
    k = 2.0/(period+1)
    e = prices[0]
    for p in prices[1:]:
        e = (p - e)*k + e
    return e

signals = []
alerts = []

for i, pool in enumerate(cands):
    addr = pool["attributes"]["address"]
    ba = pool.get("base_token",{})
    qa = pool.get("quote_token",{})
    sym = ba.get("symbol","?")
    base_addr = ba.get("address","?")
    liq = pool["attributes"]["liquidity"]["usd"]
    vol24 = pool["attributes"].get("volume",{}).get("h24",{}).get("usd",0)
    print(f"\n[{i+1}/{len(cands)}] {sym} {addr[:8]}... liq=${liq:,.0f}")
    
    try:
        ohlcv = get(f"{GECKO}/pools/{addr}/ohlcv/hour?aggregate=1&limit=120")
    except Exception as e:
        print(f"    OHLCV FAIL: {e}")
        signals.append({"token_address":base_addr,"symbol":sym,"status":"ohlcv_fail","error":str(e)})
        time.sleep(SLEEP)
        continue
    time.sleep(SLEEP)
    
    closes = []
    for c in ohlcv.get("data",[]):
        attrs = c.get("attributes",{})
        cval = attrs.get("close_quote") or attrs.get("close") or 0
        ts = attrs.get("timestamp",0)
        if cval and ts: closes.append(float(cval))
    
    if len(closes) < 21:
        print(f"    Only {len(closes)} candles, skip")
        signals.append({"token_address":base_addr,"symbol":sym,"status":"insufficient","candles":len(closes)})
        continue
    
    e9 = ema(closes, 9)
    e21 = ema(closes, 21)
    pc = closes[-1]
    
    prev_closes = closes[:-1]
    pe9 = ema(prev_closes, 9)
    pe21 = ema(prev_closes, 21)
    
    cross = (pe9 is not None and pe21 is not None and pe9 <= pe21 and e9 > e21)
    
    ch24 = None
    if len(closes) >= 24:
        p24 = closes[-24]
        ch24 = ((pc - p24)/p24)*100 if p24 else None
    
    vol1h = vol24/24.0 if vol24 else 0
    
    entry = {
        "token_address": base_addr, "symbol": sym,
        "quote_symbol": qa.get("symbol","?"),
        "pool_address": addr,
        "liquidity_usd": liq, "volume_24h_usd": vol24,
        "volume_5m_usd": 0, "volume_1h_usd": round(vol1h,4),
        "price_change_24h_pct": round(ch24,4) if ch24 is not None else None,
        "current_price": round(pc,8),
        "ema9": round(e9,6), "ema21": round(e21,6),
        "prev_ema9": round(pe9,6) if pe9 else None,
        "prev_ema21": round(pe21,6) if pe21 else None,
        "crossover": cross, "candles_used": len(closes)
    }
    print(f"    EMA9={e9:.4f} EMA21={e21:.4f} CROSS={cross} ${pc:.8f}")
    
    if cross:
        prompt = (
            f'Analyze this Base chain crypto trading signal and return ONLY JSON:\n'
            f'{{\"score\":0-100,\"decision\":\"BUY\" or \"NO_GO\",\"reason\":\"1-2 sentences\"}}\n\n'
            f'TOKEN: {sym} ({base_addr[:8]}...)\n'
            f'POOL: {addr[:8]}...\n'
            f'LIQUIDITY: ${liq:,.0f}\n'
            f'VOLUME 24H: ${vol24:,.0f}\n'
            f'VOLUME 1H (est): ${vol1h:,.0f}\n'
            f'VOLUME 5M: $0 (hourly data only)\n'
            f'PRICE CHANGE 24H: {ch24:.2f}%\n' if ch24 else f'PRICE CHANGE 24H: N/A\n'
            f'CURRENT PRICE: ${pc:.8f}\n'
            f'EMA(9): {e9:.4f}\nEMA(21): {e21:.4f}\n'
            f'PREV EMA(9): {pe9:.4f}\nPREV EMA(21): {pe21:.4f}\n'
            f'CROSSOVER: BULLISH — EMA(9) crossed above EMA(21)\n\n'
            f'This is a dual EMA 9/21 bullish crossover on Base chain. '
            f'Assess if this is genuine momentum or a false breakout.'
        )
        
        try:
            body = json.dumps({
                "model": NOUS_MODEL,
                "messages": [{"role":"user","content":prompt}],
                "max_tokens": 512, "temperature": 0.2
            }).encode()
            req = urllib.request.Request(
                f"{NOUS_BASE}/chat/completions",
                data=body,
                headers={"Authorization":f"Bearer {token}","Content-Type":"application/json"}
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                rdata = json.loads(resp.read().decode())
            content = rdata["choices"][0]["message"]["content"]
            try:
                nr = json.loads(content)
            except:
                m = re.search(r'\{[^{}]+"score"[^{}]+\}', content)
                nr = json.loads(m.group()) if m else {"score":50,"decision":"NO_GO","reason":"parse fail"}
            
            entry["nous_score"] = nr.get("score",0)
            entry["nous_decision"] = nr.get("decision","NO_GO")
            entry["nous_reason"] = nr.get("reason","")
            print(f"    Nous: score={nr.get('score')} dec={nr.get('decision')} | {nr.get('reason','')[:80]}")
            
            if nr.get("score",0) >= 80 and nr.get("decision") == "BUY":
                a = (f"🚨 ALERT: {sym} ({base_addr[:8]}...) — Nous {nr['score']}/100 BUY. "
                     f"Liq=${liq:,.0f} EMA9/21 crossover bullish.")
                alerts.append(a)
                print(f"    >>> {a}")
            
            time.sleep(1.0)
        except Exception as e:
            print(f"    Nous ERROR: {e}")
            entry["nous_score"] = None
            entry["nous_decision"] = "API_ERR"
            entry["nous_reason"] = str(e)
    
    time.sleep(SLEEP)

# --- write ---
out = {
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "scanned_pools": len(cands),
    "candidates": [{"address":p["attributes"]["address"],
                    "base_token":p["base_token"]["symbol"],
                    "liquidity_usd":p["attributes"]["liquidity"]["usd"]} for p in cands],
    "signals": signals,
    "alerts": alerts
}
open(OUT,"w").write(json.dumps(out,indent=2))
print(f"\n[5] Written {OUT}")

scored = [s for s in signals if s.get("nous_score") is not None]
buys = [s for s in signals if s.get("nous_decision")=="BUY" and s.get("nous_score",0)>=80]
crosses = [s for s in signals if s.get("crossover")]

print(f"\n=== SUMMARY ===")
print(f"Scanned: {len(cands)}  Crossovers: {len(crosses)}  Scored: {len(scored)}  BUY>=80: {len(buys)}  Alerts: {len(alerts)}")
if alerts:
    for a in alerts: print(f"  {a}")
else:
    print("No high-conviction alerts.")
print(f"\nCompact:")
print(json.dumps({
    "ts":out["timestamp"],"scanned":len(cands),"crosses":len(crosses),
    "scored":len(scored),"buys":len(buys),"alerts":len(alerts),
    "preview":[{k:s[k] for k in ["symbol","crossover","nous_score","nous_decision","nous_reason"] if k in s} for s in signals]
}, indent=2))
