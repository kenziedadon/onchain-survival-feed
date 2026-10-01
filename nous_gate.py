#!/usr/bin/env python3
"""Nous sentiment gate v2 — correct GeckoTerminal v2 API parsing."""

import json, urllib.request, urllib.error, time, os
from datetime import datetime, timezone

GECKO = "https://api.geckoterminal.com/api/v2/networks/base"

def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={
        "User-Agent": "HermesBot/1.0",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())

def fetch_retry(url, max_attempts=5, base_delay=8):
    for attempt in range(max_attempts):
        try:
            return fetch(url)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = base_delay * (attempt + 1) + 2
                print(f"  [GATE]  429, retry {attempt+1}/{max_attempts} in {wait}s")
                time.sleep(wait)
            else:
                raise
        except Exception as e:
            if attempt < max_attempts - 1:
                time.sleep(3)
            else:
                raise
    raise RuntimeError(f"Failed after {max_attempts} attempts")

def calc_ema(prices, period):
    if len(prices) < period:
        return None
    k = 2.0 / (period + 1)
    ema = prices[0]
    for p in prices[1:]:
        ema = p * k + ema * (1 - k)
    return ema

def evaluate_narrative(telemetry, model="upstage/solar-pro4:free", timeout=15):
    """Call Nous LLM for sentiment scoring. Real implementation."""
    auth_path = "/home/kenzodadon/.hermes/shared/nous_auth.json"
    try:
        with open(auth_path) as f:
            auth = json.loads(f.read())
    except Exception as e:
        return {"score": 0, "decision": "NO_GO", "reason": f"Auth load failed: {e}"}
    
    token = auth.get("access_token")
    if not token:
        return {"score": 0, "decision": "NO_GO", "reason": "No token"}
    
    inference_url = auth.get("inference_base_url", "https://inference-api.nousresearch.com/v1").rstrip("/")
    
    prompt = (
        "You are the Sentiment Proposer for a Base chain trading system. "
        "Analyze this token telemetry for a fast scalp. Focus on narrative strength, "
        "volume velocity, and liquidity. "
        "Output ONLY valid JSON with keys: 'score' (0-100), 'decision' ('BUY' or 'NO_GO'), and 'reason'. "
        f"Data: {json.dumps(telemetry)}"
    )
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "Mozilla/5.0"
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 150
    }
    
    try:
        req = urllib.request.Request(f"{inference_url}/chat/completions",
                                     data=json.dumps(payload).encode(),
                                     headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            result = json.loads(r.read().decode())
        content = result['choices'][0]['message']['content']
        content = content.replace('```json', '').replace('```', '').strip()
        return json.loads(content)
    except Exception as e:
        return {"score": 0, "decision": "NO_GO", "reason": str(e)}

def score_with_nous(candidate, token, inference_url):
    prompt = {
        "task": "Score this Base-chain token for a bullish EMA crossover trade.",
        "token_address": candidate["pool_address"],
        "symbol": candidate["base_symbol"],
        "pair": candidate["pair_name"],
        "liquidity_usd": candidate["liquidity_usd"],
        "volume_24h_usd": candidate.get("volume_24h_usd", 0),
        "ema_crossover": True,
        "ema9": round(candidate["ema9"], 6),
        "ema21": round(candidate["ema21"], 6),
        "last_close": round(candidate["last_close"], 6),
        "price_change_24h": round(candidate["price_change_24h"], 2),
        "instructions": "Score 0-100 (higher = more bullish), decision BUY or NO_GO, brief reason. Consider liquidity depth, crossover definitiveness, volume, and recent price action."
    }
    payload = {
        "model": "upstage/solar-pro4:free",
        "messages": [
            {"role": "system", "content": "Return ONLY valid JSON: {\"score\": <int 0-100>, \"decision\": \"BUY\"|\"NO_GO\", \"reason\": \"<1 sentence>\"}. No markdown, no other text."},
            {"role": "user", "content": json.dumps(prompt, indent=2)}
        ],
        "max_tokens": 256,
        "temperature": 0.3,
    }
    req = urllib.request.Request(
        f"{inference_url}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "HermesBot/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read())
    content = result["choices"][0]["message"]["content"].strip()
    if content.startswith("```"):
        lines = content.split("\n")
        json_lines = []
        in_json = False
        for line in lines:
            if line.strip().startswith("{"):
                in_json = True
            if in_json:
                json_lines.append(line)
            if in_json and line.strip().endswith("}"):
                break
        content = "\n".join(json_lines)
    return json.loads(content)


def main():
    global token
    auth_path = "/home/kenzodadon/.hermes/shared/nous_auth.json"
    with open(auth_path) as f:
        auth = json.loads(f.read())
    token = auth["access_token"]
    inference_url = auth["inference_base_url"].rstrip("/")
    print(f"[GATE] Token expires: {auth['expires_at']}")
    print(f"[GATE] Inference: {inference_url}")

    # Step 2: Trending pools
    print("\n[GATE] Fetching trending pools...")
    try:
        data = fetch_retry(f"{GECKO}/trending_pools?page=1")
    except Exception as e:
        print(f"[GATE] FAILED: {e}")
        return

    pools = data.get("data", [])
    print(f"[GATE] Fetched {len(pools)} trending pools")

    # Parse + filter
    candidates = []
    for p in pools:
        attrs = p.get("attributes", {})
        rel = p.get("relationships", {})
        pool_addr = attrs.get("address", "")
        pair_name = attrs.get("name", "")
        liq_str = attrs.get("reserve_in_usd", "0")
        try:
            liq = float(liq_str)
        except (ValueError, TypeError):
            liq = 0

        # Parse pair name: "VVV / WETH" -> base=VVV, quote=WETH
        if " / " in pair_name:
            parts = pair_name.split(" / ")
            base_sym = parts[0].strip().upper()
            quote_sym = parts[1].strip().upper()
        else:
            base_sym = "?"
            quote_sym = "?"

        is_eth_pair = "ETH" in base_sym or "WETH" in base_sym or "ETH" in quote_sym or "WETH" in quote_sym

        if liq >= 100_000 and is_eth_pair:
            candidates.append({
                "pool_address": pool_addr,
                "pair_name": pair_name,
                "base_symbol": base_sym,
                "quote_symbol": quote_sym,
                "liquidity_usd": liq,
                "volume_24h_usd": 0,
            })
            print(f"  [GATE]  CANDIDATE: {pair_name} liq=${liq:,.0f} addr={pool_addr[:12]}...")

    print(f"\n[GATE] {len(candidates)} pools pass filters (>$100k liq + ETH/WETH pair)\n")

    if not candidates:
        print("[GATE] No candidates — nothing to scan.")
        # Write empty result
        out_path = "/home/kenzodadon/base-trend-bot/nous_signals.json"
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w") as f:
            json.dump({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "tokens_scanned": 0,
                "signals": [],
                "note": "No ETH/WETH pools with >$100k liquidity in trending",
            }, f, indent=2)
        print(f"[GATE] Written to {out_path}")
        print("\n[GATE] Summary: scanned=0, crossovers=0, alerts=0")
        return

    # Step 3: OHLCV per candidate
    scored = []
    for i, c in enumerate(candidates):
        print(f"[GATE] [{i+1}/{len(candidates)}] {c['pair_name']} ({c['pool_address'][:12]}...)")
        try:
            ohlcv_data = fetch_retry(
                f"{GECKO}/pools/{c['pool_address']}/ohlcv/hour?aggregate=1&limit=120"
            )
        except Exception as e:
            print(f"  [GATE]  OHLCV failed: {e}\n")
            time.sleep(1.5)
            continue

        # OHLCV: data.data is a single object; candles in attributes.ohlcv_list
        ohlcv_obj = ohlcv_data.get("data", {})
        attrs_ohlcv = ohlcv_obj.get("attributes", {})
        ohlcv_list = attrs_ohlcv.get("ohlcv_list", [])
        if len(ohlcv_list) < 21:
            print(f"  [GATE]  Only {len(ohlcv_list)} candles (need 21+)\n")
            time.sleep(1.5)
            continue
        # candles: [ts, open, high, low, close, volume]
        closes = [float(c[4]) for c in ohlcv_list]  # close is index 4

        ema9 = calc_ema(closes, 9)
        ema21 = calc_ema(closes, 21)

        if ema9 is None or ema21 is None:
            print(f"  [GATE]  EMA failed\n")
            time.sleep(1.5)
            continue

        prev_fast = calc_ema(closes[:-1], 9)
        prev_slow = calc_ema(closes[:-1], 21)
        crossover = (prev_fast is not None and prev_slow is not None and
                     prev_fast <= prev_slow and ema9 > ema21)

        pc24h = 0.0
        if len(closes) >= 25:
            pc24h = ((closes[-1] - closes[-25]) / closes[-25]) * 100

        # Get volume
        vol_h24 = 0
        if len(ohlcv_list) >= 24:
            vol_h24 = sum(float(c[5]) for c in ohlcv_list[-24:])

        c["ema9"] = ema9
        c["ema21"] = ema21
        c["last_close"] = closes[-1]
        c["price_change_24h"] = pc24h
        c["volume_24h_usd"] = vol_h24
        c["crossover"] = crossover
        c["ohlcv_count"] = len(closes)

        print(f"  [GATE]  Close=${closes[-1]:,.6f} EMA9=${ema9:,.6f} EMA21=${ema21:,.6f}")
        print(f"  [GATE]  24h={pc24h:+.2f}% vol24h=${vol_h24:,.0f} CROSSOVER={crossover}")

        if crossover:
            print(f"  [GATE]  >>> BULLISH CROSSOVER — scoring with Nous")
            try:
                nr = score_with_nous(c, token, inference_url)
                c["nous_score"] = nr.get("score")
                c["nous_decision"] = nr.get("decision")
                c["nous_reason"] = nr.get("reason")
                print(f"  [GATE]  Nous: score={nr.get('score')} decision={nr.get('decision')} — {nr.get('reason','')}")
            except Exception as e:
                print(f"  [GATE]  Nous failed: {e}")
                c["nous_score"] = None
                c["nous_decision"] = "ERROR"
                c["nous_reason"] = str(e)

        print()
        time.sleep(1.5)
        scored.append(c)

    # Write
    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tokens_scanned": len(candidates),
        "signals": scored,
    }
    out_path = "/home/kenzodadon/base-trend-bot/nous_signals.json"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"[GATE] Written to {out_path}")

    # Alerts
    alerts = [s for s in scored if s.get("nous_score") is not None
              and s["nous_score"] >= 80 and s.get("nous_decision") == "BUY"]
    if alerts:
        print(f"\n{'='*60}")
        print(f"[ALERT] {len(alerts)} token(s) score>=80 AND BUY:")
        for a in alerts:
            print(f"  • {a['base_symbol']} ({a['pair_name']}) score={a['nous_score']} liq=${a['liquidity_usd']:,.0f}")
            print(f"    {a.get('nous_reason','')}")
        print(f"{'='*60}")
    else:
        print("\n[GATE] No alerts (no score>=80 AND BUY)")

    # Compact summary
    summary = {
        "timestamp": output["timestamp"],
        "tokens_scanned": len(candidates),
        "crossovers": sum(1 for s in scored if s.get("crossover")),
        "nous_scored": sum(1 for s in scored if s.get("nous_score") is not None),
        "alerts": len(alerts),
        "signals": [
            {
                "symbol": s["base_symbol"],
                "pair": s["pair_name"],
                "liquidity_usd": s["liquidity_usd"],
                "volume_24h_usd": s.get("volume_24h_usd", 0),
                "close": round(s["last_close"], 6),
                "ema9": round(s["ema9"], 6),
                "ema21": round(s["ema21"], 6),
                "crossover": s.get("crossover", False),
                "nous_score": s.get("nous_score"),
                "nous_decision": s.get("nous_decision"),
                "price_change_24h": round(s.get("price_change_24h", 0), 2),
            }
            for s in scored
        ]
    }
    print("\n[GATE] Summary:")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
