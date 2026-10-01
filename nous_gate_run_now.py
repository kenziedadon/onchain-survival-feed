#!/usr/bin/env python3
"""Nous sentiment gate for base-trend-bot. Stdlib only."""
import urllib.request
import urllib.error
import json
import time
import ssl
import sys
from datetime import datetime, timezone

OUT_PATH = "/home/kenzodadon/base-trend-bot/nous_signals.json"
AUTH_PATH = "/home/kenzodadon/.hermes/shared/nous_auth.json"
GT_TRENDING = "https://api.geckoterminal.com/api/v2/networks/base/trending_pools?page=1"

def fetch(url, timeout=25):
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "base-trend-bot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return json.loads(r.read().decode())

def ema(values, period):
    if len(values) < period:
        return [None] * len(values)
    k = 2.0 / (period + 1)
    out = [None] * (period - 1)
    sma = sum(values[:period]) / period
    out.append(sma)
    for i in range(period, len(values)):
        out.append((values[i] - out[-1]) * k + out[-1])
    return out

def main():
    # 1. token
    with open(AUTH_PATH) as f:
        auth = json.load(f)
    token = auth["access_token"]
    inference_url = auth.get("inference_base_url", "https://inference-api.nousresearch.com/v1")
    print(f"[1] Token valid until {auth['expires_at']}  inference={inference_url}")

    # 2. trending pools
    print("[2] Fetching GeckoTerminal trending pools...")
    gt = fetch(GT_TRENDING)
    pools = gt.get("data", [])
    print(f"    {len(pools)} pools")

    # 3. filter + OHLCV
    candidates = []
    for i, pool in enumerate(pools):
        a = pool.get("attributes", {})
        pu = a.get("pool", {})
        if pu.get("chain", {}).get("slug") != "base":
            continue
        liq = pu.get("total_liquidity_usd")
        if liq is None or liq <= 100_000:
            continue
        quote = pu.get("quote_token", {}).get("symbol", "")
        if quote not in ("ETH", "WETH"):
            continue
        taddr = pu.get("token_address", "")
        paddr = pu.get("address", "")
        sym = pu.get("token", {}).get("symbol", "")
        vol_5m = pu.get("volume_5m")
        vol_1h = pu.get("volume_1h")
        chg24 = pu.get("price_change_24h")
        print(f"    [{i}] {sym} ({taddr[:10]}...) quote={quote} liq=${liq:,.0f} vol5m={vol_5m} vol1h={vol_1h} chg24h={chg24}")

        print(f"       OHLCV...")
        try:
            time.sleep(1.5)
            ohlcv_url = f"https://api.geckoterminal.com/api/v2/networks/base/pools/{paddr}/ohlcv/hour?aggregate=1&limit=120"
            ohlcv = fetch(ohlcv_url)
            candles = ohlcv.get("data", [])
            if not candles:
                continue
            closes = [c["attributes"]["price_close"] for c in candles]
            e9 = ema(closes, 9)
            e21 = ema(closes, 21)
            if len(e9) < 2 or len(e21) < 2:
                continue
            cf, cs = e9[-1], e21[-1]
            pf, ps = e9[-2], e21[-2]
            bullish = (pf is not None and ps is not None and
                       cf is not None and cs is not None and
                       pf <= ps and cf > cs)
            if bullish:
                print(f"       CROSS: e9={cf:.6f} e21={cs:.6f}")
                candidates.append({
                    "token_address": taddr,
                    "pool_address": paddr,
                    "symbol": sym,
                    "liquidity_usd": liq,
                    "volume_5m": vol_5m,
                    "volume_1h": vol_1h,
                    "price_change_24h": chg24,
                    "ema_crossover": {"fast": cf, "slow": cs, "prev_fast": pf, "prev_slow": ps},
                    "last_close": closes[-1],
                })
        except Exception as e:
            print(f"       ! {e}")

    print(f"\n[3] {len(candidates)} bullish crossovers")

    # 4. Nous scoring
    scored = []
    for c in candidates:
        prompt = f"""You are a sentiment analyst for a crypto trading bot on Base mainnet. Score the following token 0-100 for entry viability.

Token: {c['symbol']}
Address: {c['token_address']}
Liquidity: ${c['liquidity_usd']:,.2f}
5m Volume: ${c['volume_5m'] or 0:,.2f}
1h Volume: ${c['volume_1h'] or 0:,.2f}
24h Price Change: {c['price_change_24h']}%
EMA(9)/EMA(21) Bullish Crossover: fast={c['ema_crossover']['fast']:.6f}, slow={c['ema_crossover']['slow']:.6f}. Previous: fast={c['ema_crossover']['prev_fast']:.6f}, slow={c['ema_crossover']['prev_slow']:.6f}

Return ONLY this JSON, nothing else:
{{"score": <int 0-100>, "decision": "BUY" or "NO_GO", "reason": "<one-sentence>"}}

Conservative: NO_GO unless clear momentum + liquidity >$500k.
"""
        payload = json.dumps({"model": "upstage/solar-pro4:free", "messages": [
            {"role": "user", "content": prompt}
        ], "max_tokens": 200, "temperature": 0.3}).encode()
        req = urllib.request.Request(
            f"{inference_url}/chat/completions",
            data=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                resp = json.loads(r.read().decode())
            content = resp["choices"][0]["message"]["content"].strip()
            s = content.find("{")
            e = content.rfind("}") + 1
            parsed = json.loads(content[s:e])
            parsed.update({
                "token_address": c["token_address"],
                "symbol": c["symbol"],
                "liquidity_usd": c["liquidity_usd"],
                "volume_5m": c["volume_5m"],
                "volume_1h": c["volume_1h"],
                "price_change_24h": c["price_change_24h"],
                "ema_crossover": c["ema_crossover"],
            })
            scored.append(parsed)
            print(f"    {c['symbol']}: score={parsed['score']} decision={parsed['decision']} | {parsed['reason']}")
        except Exception as ex:
            print(f"    {c['symbol']}: ERROR {ex}")
            scored.append({
                "token_address": c["token_address"], "symbol": c["symbol"],
                "score": 0, "decision": "NO_GO", "reason": f"score error: {ex}",
                "liquidity_usd": c["liquidity_usd"], "volume_5m": c["volume_5m"],
                "volume_1h": c["volume_1h"], "price_change_24h": c["price_change_24h"],
                "ema_crossover": c["ema_crossover"],
            })
        time.sleep(1.5)

    # 5. write
    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_pools_scanned": len(pools),
        "bullish_crossovers": len(candidates),
        "signals_scored": len(scored),
        "signals": scored,
    }
    with open(OUT_PATH, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\n[5] Wrote {OUT_PATH}")

    # 6. alert
    alerts = [s for s in scored if s.get("score", 0) >= 80 and s.get("decision") == "BUY"]
    if alerts:
        print("\n🚨 ALERT — HIGH-CONVICTION BUY SIGNALS:")
        for a in alerts:
            print(f"   {a['symbol']} ({a['token_address'][:10]}...) score={a['score']} {a['reason']}")
    else:
        print(f"\n[6] No ALERT. Scanned {len(pools)} pools, {len(candidates)} crossovers, scored {len(scored)}.")

    # 7. summary
    print("\n=== SUMMARY ===")
    print(json.dumps(result, indent=2))

if __name__ == "__main__":
    main()
