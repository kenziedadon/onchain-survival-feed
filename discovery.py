#!/usr/bin/env python3
"""
discovery.py — GeckoTerminal pool discovery + OHLCV fetching for Base.

Extracted from strategy_kinetic.py during the 2026-08-26 kinetic removal so the
canonical discovery/fetch code survives independent of any strategy module.
Single source of truth: trend_discovery.py, backtest.py, and pipeline_probe.py
all delegate here.
"""
import json, time, urllib.request

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json;version=20230302"}


def get(url, timeout=25, retries=8):
    last_err = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode()
        except Exception as e:
            last_err = e
            if "429" in str(e):
                time.sleep(8 + i * 4)
            else:
                time.sleep(1.5)
    raise last_err


def fetch_ohlcv(addr, limit=120):
    url = f"https://api.geckoterminal.com/api/v2/networks/base/pools/{addr}/ohlcv/hour?aggregate=1&limit={limit}"
    data = json.loads(get(url))
    rows = data.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
    return sorted([
        {"ts": r[0], "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
         "c": float(r[4]), "v": float(r[5])}
        for r in rows
    ], key=lambda x: x["ts"])


def discover_trending(limit=30):
    """ETH/WETH pools from GeckoTerminal trending. Stables excluded,
    vol_24h > $50k, 24h change > +2%, score-ranked."""
    url = "https://api.geckoterminal.com/api/v2/networks/base/trending_pools?page=1"
    try:
        data = json.loads(get(url)).get("data", [])
    except Exception:
        data = []
    out = []
    STABLES = ("USDC", "USDT", "DAI")
    USDC_ID = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"  # lowercase per GeckoTerminal ids
    for p in data:
        a = p.get("attributes", {})
        name = a.get("name", "?")
        pool_addr = a.get("address") or p.get("id", "").split("_")[-1]
        if not (pool_addr.startswith("0x") and len(pool_addr) == 42):
            continue
        # OHLCV endpoint needs the pool address, not token address.
        rel = p.get("relationships", {})
        quote_id = rel.get("quote_token", {}).get("data", {}).get("id", "").split("_")[-1]
        upper = name.upper()
        is_eth_pair = (quote_id == "0x4200000000000000000000000000000000000001"
                       or quote_id == "0x0000000000000000000000000000000000000000e"
                       or "ETH" in upper or "WETH" in upper)
        is_usdc_pair = quote_id == USDC_ID and not any(s in upper for s in ("USDC", "USDT", "DAI"))
        if is_eth_pair:
            quote = "WETH"
        elif is_usdc_pair:
            # Phase-1 observation: USDC-quoted pools are DISCOVERED + SCORED but the
            # executor only trades ETH-quote entries (sizing/routes assume ETH).
            quote = "USDC"
        else:
            continue
        if any(s in upper for s in STABLES) and not is_usdc_pair:
            continue
        liq = float(a.get("reserve_in_usd") or 0)
        v24h = float(a.get("volume_usd", {}).get("h24") or 0)
        price_change = a.get("price_change_percentage", {})
        h24_change = float(price_change.get("h24", 0))
        if h24_change <= 2:
            continue
        if v24h < 50000:
            continue
        score = 0
        if liq >= 500000:
            score += min(liq / 500000, 8) * 5
        if v24h > 0:
            score += min(v24h / 50000, 10) * 5
        if h24_change > 5:
            score += 10
        elif h24_change > 2:
            score += 5
        out.append({"name": name, "address": pool_addr, "quote": quote,
                    "liquidity": liq, "fee_bps": 30.0, "vol_24h": v24h,
                    "score": int(score), "change_24h": h24_change})
    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:limit]
