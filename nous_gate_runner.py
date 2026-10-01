#!/usr/bin/env python3
"""Nous sentiment gate — GeckoTerminal trending pools -> EMA crossover -> Nous score.
Handles GeckoTerminal v2 API: reserve_in_usd (liq), volume_usd.h24 (vol), pool name for pair symbol."""

import urllib.request
import json
import time
import math
import os
import re
from datetime import datetime, timezone

NOUS_AUTH = "/home/kenzodadon/.hermes/shared/nous_auth.json"
SIGNALS_OUT = "/home/kenzodadon/base-trend-bot/nous_signals.json"
GECKO = "https://api.geckoterminal.com/api/v2/networks/base"
NOUS_MODEL = "upstage/solar-pro4:free"
RATE_GECKO = 2.0  # seconds between Gecko calls (raised to avoid 429)
RATE_NOUS = 1.5   # seconds between Nous calls

def fetch(url, timeout=30):
    """Fetch with exponential backoff retry on 429/5xx."""
    import urllib.error
    for attempt in range(4):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Gecko/20100101 Firefox/120.0",
                    "Accept": "application/json",
                    "Origin": "https://www.geckoterminal.com",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                wait = 2 ** attempt + 5
                print(f"  429, retry in {wait}s...", end=" ")
                time.sleep(wait)
                continue
            raise
        except Exception:
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise

def ema(values, period):
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    s = sum(values[:period]) / period
    for v in values[period:]:
        s = v * k + s * (1 - k)
    return s

def load_token():
    with open(NOUS_AUTH) as f:
        return json.load(f).get("access_token", "")

def call_nous(token, prompt):
    url = "https://inference-api.nousresearch.com/v1/chat/completions"
    body = {"model": NOUS_MODEL, "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0, "max_tokens": 300}
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "User-Agent": "Hermes/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())

def parse_nous_json(text):
    """Extract JSON object from Nous response."""
    m = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except:
            pass
    clean = text.strip().strip('`').strip()
    if clean.startswith('{'):
        try:
            return json.loads(clean)
        except:
            pass
    return None

def main():
    ts = datetime.now(timezone.utc).isoformat()
    print(f"[{ts}] Nous sentiment gate starting")
    
    token = load_token()
    if not token:
        print("FATAL: no Nous token"); return
    print(f"Token ok (expires ~21:45 UTC)")
    
    # 1. Fetch trending pools
    print("\n[1/6] Fetching GeckoTerminal trending pools...")
    data = fetch(f"{GECKO}/trending_pools?page=1")
    pools = data.get("data", [])
    print(f"  {len(pools)} pools returned")
    
    # 2. Filter: >100k liq + ETH/WETH pair
    print("\n[2/6] Filtering pools (reserve_in_usd >100k, ETH/WETH pair)...")
    candidates = []
    for p in pools:
        a = p.get("attributes", {})
        name = a.get("name", "")
        liq_raw = a.get("reserve_in_usd", "0")
        liq = float(liq_raw) if isinstance(liq_raw, (int,float)) else float(str(liq_raw).replace(",",""))
        vol_dict = a.get("volume_usd", {})
        vol24 = float(vol_dict.get("h24", 0)) if isinstance(vol_dict, dict) else float(vol_dict or 0)
        # ETH/WETH check: split pair name, check if either token is ETH or WETH
        # Handles "WETH 1%", "ETH", "WETH" (with or without fee suffix)
        parts = [p.strip() for p in name.split("/")]
        tokens_in_pair = [p.split()[0] for p in parts if p]
        is_eth = any(t in ("ETH", "WETH") for t in tokens_in_pair)
        
        if liq > 100000 and is_eth:
            addr = p.get("id", "")
            pool_addr = addr.replace("base_", "") if addr.startswith("base_") else addr
            candidates.append({
                "address": addr,
                "pool_addr": pool_addr,
                "name": name,
                "pair": name,
                "liq_usd": liq,
                "vol_24h": vol24,
            })
        print(f"  {name:30s} liq=${liq:>12,.0f} vol24=${vol24:>12,.0f} {'*' if is_eth else ''}")
        time.sleep(RATE_GECKO)
    
    print(f"\n  -> {len(candidates)} qualifying ETH/WETH pools")
    for c in candidates:
        print(f"    {c['name']:30s} addr={c['pool_addr'][:25]} liq=${c['liq_usd']:,.0f}")
    
    if not candidates:
        print("\n[SKIP] No qualifying pools. Writing empty result.")
        out = {"timestamp": ts, "scanned": len(pools), "qualifying": 0,
               "crossovers": 0, "scored": 0, "alerts": 0, "results": []}
        os.makedirs(os.path.dirname(SIGNALS_OUT), exist_ok=True)
        with open(SIGNALS_OUT, "w") as f:
            json.dump(out, f, indent=2)
        print(f"Written to {SIGNALS_OUT}")
        print(f"[SUMMARY] Scanned {len(pools)} pools, 0 qualifying ETH/WETH >$100k. No signals.")
        return
    
    # 3. OHLCV + EMA crossover
    print(f"\n[3/6] Fetching OHLCV + computing EMA(9)/EMA(21) crossovers...")
    cross_candidates = []
    
    for i, c in enumerate(candidates):
        addr = c["pool_addr"]
        print(f"  [{i+1}/{len(candidates)}] {c['name']}", end=" ... ")
        try:
            url = f"{GECKO}/pools/{addr}/ohlcv/hour?aggregate=1&limit=120"
            ohlcv = fetch(url)
            odata = ohlcv.get("data", {})
            attrs2 = odata.get("attributes", {})
            cols = attrs2.get("ohlcv_list", [])
            # cols: list of [timestamp, open, high, low, close, volume]
            closes = [float(r[4]) for r in cols]  # close is index 4
            volumes = [float(r[5]) for r in cols]  # volume is index 5
            
            if len(closes) < 22:
                print(f"only {len(closes)} candles, skip")
                time.sleep(RATE_GECKO)
                continue
            
            e9c = ema(closes, 9)
            e21c = ema(closes, 21)
            e9p = ema(closes[:-1], 9)
            e21p = ema(closes[:-1], 21)
            price = closes[-1]
            cross = (e9p is not None and e21p is not None and
                     e9p <= e21p and e9c > e21c)
            
            c["ema9"] = round(e9c, 6) if e9c else None
            c["ema21"] = round(e21c, 6) if e21c else None
            c["crossover"] = cross
            c["price"] = price
            
            print(f"EMA9={e9c:.6f} EMA21={e21c:.6f} close=${price:.6f} cross={'YES' if cross else 'no'}")
            
            if cross:
                try:
                    v5 = fetch(f"{GECKO}/pools/{addr}/ohlcv/5m?aggregate=1&limit=100")
                    o5 = v5.get("data", {})
                    a5 = o5.get("attributes", {})
                    cols5 = a5.get("ohlcv_list", [])
                    vol5 = sum(float(r[5]) for r in cols5[-10:]) / 10 if cols5 else 0
                except:
                    vol5 = 0
                vol1h = sum(volumes[-24:]) / 24 if len(volumes) >= 24 else 0
                if len(closes) >= 25:
                    p24 = closes[-25]
                    chg24 = ((price - p24) / p24) * 100
                else:
                    chg24 = 0
                c["vol_5m_avg"] = vol5
                c["vol_1h_avg"] = vol1h
                c["price_change_24h_pct"] = chg24
                cross_candidates.append(c)
        except Exception as e:
            print(f"ERROR: {e}")
        time.sleep(RATE_GECKO)
    
    print(f"\n  -> {len(cross_candidates)} bullish crossovers")
    
    # 4. Nous scoring
    print(f"\n[4/6] Calling Nous API for crossover candidates...")
    scored = []
    alerts = []
    
    for c in cross_candidates:
        name = c["name"]
        print(f"  Scoring {name} ({c['pool_addr'][:20]}...)", end=" ... ")
        
        vol5s = f"${c['vol_5m_avg']:,.2f}"
        vol1hs = f"${c['vol_1h_avg']:,.2f}"
        chgs = f"{c['price_change_24h_pct']:+.2f}%"
        
        prompt = (
            f"Score this Base-chain token based on EMA(9)/EMA(21) bullish crossover telemetry.\n\n"
            f"Token/Pair: {name}\n"
            f"Pool Address: {c['address']}\n"
            f"Liquidity (reserve): ${c['liq_usd']:,.2f}\n"
            f"24h Volume: ${c['vol_24h']:,.2f}\n"
            f"Current Price: ${c['price']:.6f}\n"
            f"EMA(9): {c['ema9']}\n"
            f"EMA(21): {c['ema21']}\n"
            f"Crossover: BULLISH (EMA9 crossed above EMA21)\n"
            f"Avg 5m Volume (last 10): {vol5s}\n"
            f"Avg 1h Volume (last 24): {vol1hs}\n"
            f"24h Price Change: {chgs}\n\n"
            f"Return ONLY valid JSON:\n"
            f'{{"score": <0-100>, "decision": "BUY"|"NO_GO", "reason": "<1-2 sentence explanation>"}}'
        )
        
        try:
            resp = call_nous(token, prompt)
            content = resp.get("choices", [{}])[0].get("message", {}).get("content", "{}")
            sd = parse_nous_json(content)
            if not sd:
                print(f"PARSE FAIL: {content[:150]}")
                sd = {"score": 0, "decision": "NO_GO", "reason": "parse failure"}
            c["nous_score"] = sd.get("score", 0)
            c["nous_decision"] = sd.get("decision", "NO_GO")
            c["nous_reason"] = sd.get("reason", "N/A")
            print(f"score={c['nous_score']} {c['nous_decision']}")
            if c["nous_score"] >= 80 and c["nous_decision"] == "BUY":
                alerts.append(c)
            scored.append(c)
        except Exception as e:
            print(f"API ERROR: {e}")
            c["nous_score"] = 0
            c["nous_decision"] = "NO_GO"
            c["nous_reason"] = f"API error: {e}"
            scored.append(c)
        time.sleep(RATE_NOUS)
    
    # 5. Write results
    print(f"\n[5/6] Writing {len(scored)} results to {SIGNALS_OUT}...")
    out = {
        "timestamp": ts,
        "scanned_pools": len(pools),
        "qualifying_pools": len(candidates),
        "crossovers_found": len(cross_candidates),
        "scored": len(scored),
        "alerts": len(alerts),
        "results": scored,
    }
    os.makedirs(os.path.dirname(SIGNALS_OUT), exist_ok=True)
    with open(SIGNALS_OUT, "w") as f:
        json.dump(out, f, indent=2)
    print("  Written.")
    
    # 6. ALERT or summary
    print("\n" + "="*60)
    if alerts:
        print("🚨  ALERT — HIGH-CONVICTION BUY SIGNALS (Nous >=80 + BUY)")
        print("="*60)
        for a in alerts:
            print(f"\n  Pair:      {a['name']}")
            print(f"  Address:   {a['address']}")
            print(f"  Liq:       ${a['liq_usd']:,.0f}")
            print(f"  Price:     ${a['price']:.6f}")
            print(f"  EMA9/21:   {a['ema9']} / {a['ema21']}")
            print(f"  Score:     {a['nous_score']}/100")
            print(f"  Decision:  {a['nous_decision']}")
            print(f"  Reason:    {a['nous_reason']}")
    else:
        print("[SUMMARY]")
        print(f"  Pools scanned:           {len(pools)}")
        print(f"  Qualifying (>$100k + ETH/WETH): {len(candidates)}")
        print(f"  Bullish crossovers:      {len(cross_candidates)}")
        print(f"  Scored by Nous:          {len(scored)}")
        print(f"  Alerts (score>=80+BUY):  {len(alerts)}")
        if scored:
            print("\n  Top signals by score:")
            for r in sorted(scored, key=lambda x: x.get("nous_score",0), reverse=True)[:5]:
                flag = "🚨" if (r.get("nous_score",0) >= 80 and r.get("nous_decision") == "BUY") else "  "
                print(f"  {flag} {r['name']:30s} score={r.get('nous_score',0):3d} {r.get('nous_decision','?')}")
        else:
            print("\n  No crossover signals to score.")
    print("="*60)

if __name__ == "__main__":
    main()
