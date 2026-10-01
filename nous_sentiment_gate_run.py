#!/usr/bin/env python3
"""Nous sentiment gate: scan GeckoTerminal trending pools, detect EMA crossovers, score with Nous API."""

import json
import urllib.request
import urllib.error
import time
import re
import sys

# --- Config ---
NOUS_TOKEN = "eyJhbG...Wp3Q"
NOUS_URL = "https://inference-api.nousresearch.com/v1/chat/completions"
GECKO_BASE = "https://api.geckoterminal.com/api/v2/networks/base"
OUTPUT_PATH = "/home/kenzodadon/base-trend-bot/nous_signals.json"

def fetch_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "HermesBot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())

def calc_ema(data, period):
    k = 2.0 / (period + 1)
    ema = data[0]["close"]
    result = [ema]
    for i in range(1, len(data)):
        ema = data[i]["close"] * k + ema * (1 - k)
        result.append(ema)
    return result

def ema_crossover(ema_fast, ema_slow):
    if len(ema_fast) < 2 or len(ema_slow) < 2:
        return False
    return ema_fast[-2] <= ema_slow[-2] and ema_fast[-1] > ema_slow[-1]

def main():
    print("=== Nous Sentiment Gate ===", flush=True)
    
    # Step 1: fetch trending pools
    print("\n[1/6] Fetching GeckoTerminal trending pools...", flush=True)
    try:
        trending = fetch_json(f"{GECKO_BASE}/trending_pools?page=1")
        pools = trending.get("data", [])
        print(f"  Got {len(pools)} trending pools", flush=True)
    except Exception as e:
        print(f"  FAILED: {e}", flush=True)
        sys.exit(1)
    
    # Step 2: filter >$100k liq + ETH/WETH pair
    print("\n[2/6] Filtering for >$100k liquidity + ETH/WETH pairs...", flush=True)
    candidates = []
    for pool in pools:
        try:
            attrs = pool["attributes"]
            liq = float(attrs.get("pool_usd_liquidity", 0) or 0)
            base_token = attrs.get("base_token", {})
            quote_token = attrs.get("quote_token", {})
            base_symbol = base_token.get("symbol", "").upper()
            quote_symbol = quote_token.get("symbol", "").upper()
            
            is_eth_pair = quote_symbol in ("ETH", "WETH")
            if liq > 100000 and is_eth_pair:
                pool_addr = pool["id"].split("/")[-1]
                candidates.append({
                    "pool_address": pool_addr,
                    "base_symbol": base_symbol,
                    "quote_symbol": quote_symbol,
                    "liquidity_usd": liq,
                    "base_token_address": base_token.get("address", ""),
                })
        except Exception as e:
            print(f"  Skip pool: {e}", flush=True)
    
    print(f"  Candidates: {len(candidates)}", flush=True)
    for c in candidates:
        print(f"    {c['base_symbol']}: ${c['liquidity_usd']:,.0f} liq ({c['quote_symbol']})", flush=True)
    
    # Step 3: OHLCV + EMA crossover detection
    print("\n[3/6] Fetching OHLCV and checking EMA(9)/EMA(21) crossovers...", flush=True)
    crossover_tokens = []
    
    for idx, cand in enumerate(candidates):
        addr = cand["pool_address"]
        symbol = cand["base_symbol"]
        print(f"  {symbol}...", flush=True)
        try:
            ohlcv = fetch_json(f"{GECKO_BASE}/pools/{addr}/ohlcv/hour?aggregate=1&limit=120")
            candles = ohlcv.get("data", [])
            if not candles:
                print(f"    No candles", flush=True)
                if idx < len(candidates) - 1:
                    time.sleep(1.5)
                continue
            
            closes = [{"close": c["attributes"]["price_close"]} for c in candles]
            ema9 = calc_ema(closes, 9)
            ema21 = calc_ema(closes, 21)
            
            cross = ema_crossover(ema9, ema21)
            print(f"    EMA9={ema9[-1]:.6f} EMA21={ema21[-1]:.6f} cross={cross}", flush=True)
            
            if cross:
                crossover_tokens.append({
                    **cand,
                    "ema9": round(ema9[-1], 8),
                    "ema21": round(ema21[-1], 8),
                    "price": candles[-1]["attributes"]["price_close"],
                })
        except Exception as e:
            print(f"    ERROR: {e}", flush=True)
        
        if idx < len(candidates) - 1:
            time.sleep(1.5)
    
    print(f"\n  Crossovers found: {len(crossover_tokens)}", flush=True)
    for t in crossover_tokens:
        print(f"    {t['base_symbol']}: EMA9={t['ema9']} > EMA21={t['ema21']} @ ${t['price']}", flush=True)
    
    # Step 4: call Nous API for each crossover
    print("\n[4/6] Calling Nous API for crossover tokens...", flush=True)
    nous_results = []
    
    for tok in crossover_tokens:
        payload = {
            "model": "upstage/solar-pro4:free",
            "messages": [
                {
                    "role": "system",
                    "content": "You are a crypto trading sentiment analyzer. Analyze the following token's EMA(9)/EMA(21) bullish crossover signal and return a JSON object with exactly these keys: {\"score\": <0-100 integer>, \"decision\": \"BUY\" or \"NO_GO\", \"reason\": \"<brief explanation>\"}. Score based on: liquidity depth, volume momentum, crossover strength, and risk of fakeout. Be conservative — most crossovers fail."
                },
                {
                    "role": "user",
                    "content": json.dumps({
                        "token_address": tok["base_token_address"],
                        "symbol": tok["base_symbol"],
                        "liquidity_usd": tok["liquidity_usd"],
                        "ema9": tok["ema9"],
                        "ema21": tok["ema21"],
                        "price": tok["price"],
                        "signal": "EMA(9)/EMA(21) bullish crossover detected"
                    }, indent=2)
                }
            ],
            "max_tokens": 500,
            "temperature": 0.3,
        }
        
        try:
            req = urllib.request.Request(
                NOUS_URL,
                data=json.dumps(payload).encode(),
                headers={
                    "Authorization": f"Bearer {NOUS_TOKEN}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                resp_data = json.loads(resp.read())
                content = resp_data["choices"][0]["message"]["content"]
                # Parse JSON from response
                try:
                    result = json.loads(content.strip())
                except json.JSONDecodeError:
                    match = re.search(r'\{.*\}', content, re.DOTALL)
                    if match:
                        result = json.loads(match.group())
                    else:
                        result = {"score": 0, "decision": "NO_GO", "reason": f"Parse error: {content[:100]}"}
                
                result["symbol"] = tok["base_symbol"]
                result["pool_address"] = tok["pool_address"]
                result["liquidity_usd"] = tok["liquidity_usd"]
                result["base_token_address"] = tok["base_token_address"]
                result["ema9"] = tok["ema9"]
                result["ema21"] = tok["ema21"]
                result["price"] = tok["price"]
                nous_results.append(result)
                print(f"  {tok['base_symbol']}: score={result.get('score')}, decision={result.get('decision')}", flush=True)
        except Exception as e:
            print(f"  ERROR for {tok['base_symbol']}: {e}", flush=True)
            nous_results.append({
                "symbol": tok["base_symbol"],
                "pool_address": tok["pool_address"],
                "liquidity_usd": tok["liquidity_usd"],
                "base_token_address": tok["base_token_address"],
                "ema9": tok["ema9"],
                "ema21": tok["ema21"],
                "price": tok["price"],
                "score": 0,
                "decision": "NO_GO",
                "reason": f"API error: {str(e)}",
            })
        
        time.sleep(2)
    
    # Step 5: write results
    print(f"\n[5/6] Writing results to {OUTPUT_PATH}...", flush=True)
    output = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_trending_pools": len(pools),
        "candidates_filtered": len(candidates),
        "crossover_count": len(crossover_tokens),
        "nous_scored": len(nous_results),
        "signals": nous_results,
    }
    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2)
    print(f"  Written {len(nous_results)} signals", flush=True)
    
    # Step 6: ALERT check
    print("\n[6/6] Checking for strong BUY signals...", flush=True)
    alerts = [s for s in nous_results if s.get("score", 0) >= 80 and s.get("decision") == "BUY"]
    
    if alerts:
        print("\n🚨 ALERT — Strong BUY signals:", flush=True)
        for a in alerts:
            print(f"  {a['symbol']}: score={a['score']}, reason={a['reason']}", flush=True)
    else:
        print("  No strong BUY signals (score>=80 + BUY).", flush=True)
    
    # Step 7: compact summary
    print("\n=== SUMMARY ===", flush=True)
    summary = {
        "timestamp": output["timestamp"],
        "trending_pools": len(pools),
        "eth_weth_candidates": len(candidates),
        "crossovers": len(crossover_tokens),
        "scored": len(nous_results),
        "alerts": len(alerts),
        "symbols": [s["symbol"] for s in nous_results],
        "scores": {s["symbol"]: s["score"] for s in nous_results},
        "decisions": {s["symbol"]: s["decision"] for s in nous_results},
    }
    print(json.dumps(summary, indent=2))

if __name__ == "__main__":
    main()
