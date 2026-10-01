#!/usr/bin/env python3
"""Nous sentiment gate for base-trend-bot — stdlib only, no-rate-limit version for debugging."""
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "HermesBot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def jitter_sleep(base):
    time.sleep(base + (hash(str(time.time())) % 500) / 1000)

# ── 1. load Nous token ───────────────────────────────────────────────
with open("/home/kenzodadon/.hermes/shared/nous_auth.json") as f:
    auth = json.load(f)
token = auth["access_token"]
inference_url = auth.get("inference_base_url", "https://inference-api.nousresearch.com/v1")
print(f"[1] Nous token loaded, expires_at={auth['expires_at']}")

# ── 2. fetch trending pools (save raw) ───────────────────────────────
print("[2] Fetching GeckoTerminal trending pools...")
trending = http_get("https://api.geckoterminal.com/api/v2/networks/base/trending_pools?page=1")
pools = trending.get("data", [])
print(f"    Got {len(pools)} trending pools")

with open("/home/kenzodadon/base-trend-bot/trending_raw.json", "w") as f:
    json.dump(trending, f, indent=2)

# ── 3. print pool details ────────────────────────────────────────────
print("\n=== POOL DETAILS ===")
for i, pool in enumerate(pools[:5]):
    attrs = pool.get("attributes", {})
    pool_addr = attrs.get("address", "")
    liq_str = attrs.get("totalLiquidityUsd", "0")
    liq = float(liq_str) if liq_str else 0
    base = attrs.get("base_token", {})
    token = attrs.get("token", {})
    base_sym = base.get("symbol", "?").upper()
    token_sym = token.get("symbol", "?")
    token_addr = token.get("address", "?")
    print(f"  [{i+1}] {token_sym} ({token_addr[:12]}...) | base={base_sym} | liq=${liq:,.0f} | pool={pool_addr[:12]}...")
    # print all attributes for debugging
    for k, v in sorted(attrs.items()):
        if isinstance(v, (str, int, float, bool)):
            print(f"      {k}: {v}")

print("\n=== RUNNING WITH 1.5s RATE LIMIT ===")
print("Re-run with full gate. Raw data saved to trending_raw.json")
