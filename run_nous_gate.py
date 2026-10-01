#!/usr/bin/env python3
"""Base Trend Bot — Nous Sentiment Gate (stdlib only)."""
import json
import time
import ssl
import urllib.request
import urllib.error
from datetime import datetime, timezone
import os

# --- Config ---
NOUS_AUTH_PATH = "/home/kenzodadon/.hermes/shared/nous_auth.json"
OUTPUT_PATH = "/home/kenzodadon/base-trend-bot/nous_signals.json"
GECKO_BASE = "https://api.geckoterminal.com/api/v2/networks/base"
NOUS_INFERENCE_URL = "https://inference-api.nousresearch.com/v1/chat/completions"
GECKO_RATE_LIMIT = 1.5
NOUS_RATE_LIMIT = 2.0

def gecko_get(path, timeout=30):
    url = GECKO_BASE + path
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(
        url, headers={"User-Agent": "HermesBaseTrendBot/1.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read()
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code}: {e.reason} for {url}")
        return None
    except Exception as e:
        print(f"  Error fetching {url}: {e}")
        return None

def calc_ema(prices, period):
    if len(prices) < period:
        return None
    k = 2.0 / (period + 1.0)
    ema = prices[0]
    for p in prices[1:]:
        ema = p * k + ema * (1.0 - k)
    return ema

def check_bullish_crossover(closes):
    """Return (bullish, ema9, ema21) for current candle, with prev comparison."""
    if len(closes) < 22:
        return False, None, None
    # Current EMAs
    ema9 = calc_ema(closes, 9)
    ema21 = calc_ema(closes, 21)
    if ema9 is None or ema21 is None:
        return False, None, None
    # Previous EMAs (drop last candle)
    prev_closes = closes[:-1]
    prev_ema9 = calc_ema(prev_closes, 9)
    prev_ema21 = calc_ema(prev_closes, 21)
    if prev_ema9 is None or prev_ema21 is None:
        return False, None, None
    bullish = (prev_ema9 <= prev_ema21) and (ema9 > ema21)
    return bullish, ema9, ema21

def load_nous_auth():
    with open(NOUS_AUTH_PATH) as f:
        auth = json.load(f)
    return auth

def call_nous(token_addr, symbol, liq_usd, vol5m, vol1h, price_chg24h, ema9, ema21):
    auth = load_nous_auth()
    token = auth.get("access_token", "")
    if not token:
        return {"score": 0, "decision": "NO_GO", "reason": "no auth token"}

    prompt = (
        f"You are a crypto sentiment analyst for Base chain trading. "
        f"Analyze this token for a potential BUY signal based on a bullish EMA 9/21 crossover.\n\n"
        f"Token: {symbol} ({token_addr})\n"
        f"Liquidity: ${liq_usd:,.2f}\n"
        f"Volume (5m): ${vol5m:,.2f}\n"
        f"Volume (1h): ${vol1h:,.2f}\n"
        f"24h Price Change: {price_chg24h:+.2f}%\n"
        f"EMA9: {ema9:.6f}\n"
        f"EMA21: {ema21:.6f}\n"
        f"Signal: Bullish EMA crossover detected (EMA9 crossed above EMA21)\n\n"
        f"Output ONLY valid JSON with exactly these fields:\n"
        f'{{\"score\": <0-100 integer>, "decision": "BUY" or "NO_GO", "reason": "<1-2 sentence explanation>"}}\n'
        f"Do not include any other text, markdown, or explanations outside the JSON."
    )

    payload = {
        "model": "upstage/solar-pro4:free",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 200,
        "temperature": 0.3,
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        NOUS_INFERENCE_URL,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "HermesBaseTrendBot/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            content = result["choices"][0]["message"]["content"].strip()
            # Strip markdown code fences if present
            if content.startswith("```"):
                parts = content.split("```")
                if len(parts) >= 3:
                    inner = parts[1].strip()
                    if inner.startswith("json"):
                        inner = inner[4:].strip()
                    content = inner
            return json.loads(content)
    except Exception as e:
        print(f"  Nous API error: {e}")
        return {"score": 0, "decision": "NO_GO", "reason": f"API error: {e}"}

def main():
    print("=" * 60)
    print("Base Trend Bot — Nous Sentiment Gate")
    print(f"Run: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 60)

    # Step 1: Load auth
    auth = load_nous_auth()
    print(f"Auth token expires: {auth.get('expires_at', 'unknown')}")
    api_base = auth.get("inference_base_url", "")
    print(f"Inference base URL: {api_base}")
    # Override URL if present in auth
    global NOUS_INFERENCE_URL
    if api_base and api_base.rstrip("/") != "https://inference-api.nousresearch.com":
        NOUS_INFERENCE_URL = api_base.rstrip("/") + "/v1/chat/completions"
        print(f"Using inference URL from auth: {NOUS_INFERENCE_URL}")

    # Step 2: Fetch trending pools
    print("\n[1/5] Fetching trending pools from GeckoTerminal...")
    pools_data = gecko_get("/trending_pools?page=1")
    if not pools_data or "data" not in pools_data:
        print("  ERROR: Could not fetch trending pools")
        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "scanned_candidates": 0,
            "scored_signals": [],
            "summary": "Failed to fetch trending pools from GeckoTerminal",
        }
        with open(OUTPUT_PATH, "w") as f:
            json.dump(result, f, indent=2)
        return

    pools = pools_data["data"]
    print(f"  Found {len(pools)} trending pools")

    # Step 3: Filter pools (>$100k liq, ETH/WETH quote)
    candidates = []
    for i, pool in enumerate(pools):
        try:
            attrs = pool["attributes"]
            pool_addr = attrs["address"]
            liq = attrs.get("total_liquidity_usd", 0) or 0
            base_token = attrs.get("base_token", {})
            quote_token = attrs.get("quote_token", {})
            base_symbol = base_token.get("symbol", "?")
            quote_symbol = quote_token.get("symbol", "?")
            token_addr = base_token.get("address", "")

            is_eth_pair = quote_symbol.upper() in ("ETH", "WETH")
            if liq > 100_000 and is_eth_pair:
                candidates.append({
                    "address": pool_addr,
                    "base_symbol": base_symbol,
                    "quote_symbol": quote_symbol,
                    "liquidity_usd": liq,
                    "pool_url": attrs.get("pool_url", ""),
                    "token_address": token_addr,
                })
                print(f"  Candidate#{len(candidates)}: {base_symbol}/{quote_symbol} — ${liq:,.0f} liq")
        except Exception as e:
            print(f"  Error parsing pool {i}: {e}")
            continue
        time.sleep(GECKO_RATE_LIMIT)

    print(f"\n  Filtered to {len(candidates)} candidates (>$100k liq, ETH/WETH pair)")

    if not candidates:
        print("\n[RESULT] No qualifying candidates found.")
        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "scanned_candidates": 0,
            "scored_signals": [],
            "summary": "No pools met criteria (>$100k liquidity, ETH/WETH quote)",
        }
        with open(OUTPUT_PATH, "w") as f:
            json.dump(result, f, indent=2)
        print(f"Written to {OUTPUT_PATH}")
        return

    # Step 4: Fetch OHLCV + detect crossovers
    print("\n[2/5] Fetching OHLCV and scanning for EMA crossovers...")
    crossover_candidates = []

    for idx, c in enumerate(candidates):
        pool_addr = c["address"]
        print(f"  [{idx+1}/{len(candidates)}] {c['base_symbol']}...", end=" ", flush=True)

        ohlcv = gecko_get(f"/pools/{pool_addr}/ohlcv/hour?aggregate=1&limit=120")
        time.sleep(GECKO_RATE_LIMIT)

        if not ohlcv or "data" not in ohlcv:
            print("NO OHLCV")
            continue

        closes = []
        for d in ohlcv["data"]:
            a = d.get("attributes", {})
            cv = a.get("close_usd") or a.get("close") or 0
            if isinstance(cv, str):
                try:
                    cv = float(cv)
                except ValueError:
                    cv = 0.0
            closes.append(float(cv) if cv else 0.0)

        closes = [x for x in closes if x > 0]

        if len(closes) < 22:
            print(f"too few closes ({len(closes)})")
            continue

        bullish, ema9, ema21 = check_bullish_crossover(closes)

        if bullish:
            # Fetch pool details for volume / price change
            vol5m = 0.0
            vol1h = 0.0
            price_chg24h = 0.0
            try:
                pool_full = gecko_get(f"/pools/{pool_addr}")
                if pool_full and "data" in pool_full:
                    fa = pool_full["data"][0].get("attributes", {})
                    vol1h = fa.get("total_volume_usd", 0) or 0
                    price_chg24h = fa.get("price_change_24h", 0) or 0
                    # 5m approx from 1h / 12
                    vol5m = vol1h / 12.0 if vol1h else 0.0
            except Exception:
                pass

            c.update({
                "ema9": round(ema9, 6),
                "ema21": round(ema21, 6),
                "vol_5m_usd": round(vol5m, 2),
                "vol_1h_usd": round(vol1h, 2),
                "price_change_24h_pct": round(price_chg24h, 4),
            })
            crossover_candidates.append(c)
            print(f"CROSSOVER ✅ EMA9={ema9:.4f} > EMA21={ema21:.4f}")
        else:
            print(f"no crossover (EMA9={ema9:.4f}, EMA21={ema21:.4f})" if ema9 else "no crossover")

    print(f"\n  Found {len(crossover_candidates)} bullish crossovers")

    # Step 5: Score with Nous
    print("\n[3/5] Scoring crossovers with Nous API...")
    scored = []
    alerts = []

    for idx, c in enumerate(crossover_candidates):
        print(f"  [{idx+1}/{len(crossover_candidates)}] {c['base_symbol']}...")
        sr = call_nous(
            c["token_address"],
            c["base_symbol"],
            c["liquidity_usd"],
            c["vol_5m_usd"],
            c["vol_1h_usd"],
            c["price_change_24h_pct"],
            c["ema9"],
            c["ema21"],
        )
        time.sleep(NOUS_RATE_LIMIT)

        c["nous_score"] = sr.get("score", 0)
        c["nous_decision"] = sr.get("decision", "NO_GO")
        c["nous_reason"] = sr.get("reason", "")

        print(f"    Score: {c['nous_score']}/100 | Decision: {c['nous_decision']} | {c['nous_reason']}")

        scored.append(c)
        if c["nous_score"] >= 80 and c["nous_decision"] == "BUY":
            alerts.append(c)

    # Step 6: Write output
    print("\n[4/5] Writing results...")
    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "scanned_candidates": len(candidates),
        "scored_signals": scored,
        "alerts": alerts,
        "summary": {
            "total_trending_pools": len(pools),
            "qualifying_candidates": len(candidates),
            "crossovers_found": len(crossover_candidates),
            "signals_scored": len(scored),
            "buy_alerts": len(alerts),
        },
    }
    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2)
    print(f"  Written to {OUTPUT_PATH}")

    # Step 7: Summary / ALERT
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Trending pools scanned:     {len(pools)}")
    print(f"Qualifying candidates:      {len(candidates)}")
    print(f"EMA crossovers found:       {len(crossover_candidates)}")
    print(f"Signals scored by Nous:     {len(scored)}")
    print(f"BUY alerts (score>=80):     {len(alerts)}")

    if alerts:
        print("\n🚨 ALERT — BUY SIGNALS DETECTED 🚨")
        for a in alerts:
            print(f"  {a['base_symbol']} ({a['token_address'][:10]}...)")
            print(f"    Score: {a['nous_score']}/100 | Liq: ${a['liquidity_usd']:,.0f}")
            print(f"    EMA9={a['ema9']:.4f} EMA21={a['ema21']:.4f}")
            print(f"    Reason: {a['nous_reason']}")
            print()
    else:
        print("\nNo high-confidence BUY alerts this cycle.")

    # Compact JSON summary
    compact = {
        "ts": output["timestamp"],
        "pools_scanned": len(pools),
        "candidates": len(candidates),
        "crossovers": len(crossover_candidates),
        "signals": [
            {
                "symbol": s["base_symbol"],
                "score": s["nous_score"],
                "decision": s["nous_decision"],
                "liq_usd": s["liquidity_usd"],
                "ema9": s["ema9"],
                "ema21": s["ema21"],
            }
            for s in scored
        ],
        "alerts_count": len(alerts),
        "alert_symbols": [a["base_symbol"] for a in alerts],
    }
    print("\nCompact JSON summary:")
    print(json.dumps(compact, indent=2))

if __name__ == "__main__":
    main()
