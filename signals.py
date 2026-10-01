#!/usr/bin/env python3
"""
OPTIMIZED V2 Signal Layer for Base multi-strategy agent.
Uses GeckoTerminal/CoinGecko data with 24h momentum proxy.
"""

import json
import urllib.request
import statistics
from pathlib import Path

BASE_RPC = "https://mainnet.base.org"
GECKO_BASE = "https://api.geckoterminal.com/api/v2/networks/base"

# OPTIMIZED THRESHOLDS
GAS_SPIKE_GWEI = 12
MIN_LIQUIDITY_USD = 250_000
MIN_24H_VOLUME_USD = 100_000


def rpc_call(method: str, params: list = None) -> dict:
    if params is None:
        params = []
    payload = json.dumps({
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": 1,
    }).encode()
    req = urllib.request.Request(
        BASE_RPC,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def gecko_get(path: str, timeout: int = 15) -> dict:
    url = f"{GECKO_BASE}/{path.lstrip('/')}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return {}


def get_trending_pools(limit: int = 30) -> list[dict]:
    """Get trending pools - 24h momentum proxy."""
    data = gecko_get("trending_pools")
    pools = []
    for item in (data.get("data") or []):
        attrs = item.get("attributes", {})
        rel = item.get("relationships", {})
        base_id = rel.get("base_token", {}).get("data", {}).get("id", "")
        quote_id = rel.get("quote_token", {}).get("data", {}).get("id", "")
        weth = "0x4200000000000000000000000000000000000006"
        
        base_addr = "0x" + base_id.split("_")[-1] if base_id.startswith("base_0x") else ""
        quote_addr = "0x" + quote_id.split("_")[-1] if quote_id.startswith("quote_0x") else ""
        token_addr = base_addr if base_addr.lower() != weth.lower() else quote_addr
        
        if token_addr:
            pools.append({
                "pool_address": attrs.get("address", ""),
                "token_address": token_addr,
                "token_symbol": attrs.get("name", "").split("/")[0].strip(),
                "name": attrs.get("name", ""),
                "volume_24h_usd": float(attrs.get("volume_usd", {}).get("h24", 0) or 0),
                "liquidity_usd": float(attrs.get("reserve_in_usd", 0) or 0),
                "price_usd": float(attrs.get("base_token_price_usd", 0) or 0),
                "is_trending": True,
            })
    return pools[:limit]


def get_new_pools(limit: int = 30) -> list[dict]:
    """Get new pools - full momentum data."""
    data = gecko_get("new_pools")
    pools = []
    for item in (data.get("data") or []):
        attrs = item.get("attributes", {})
        pools.append({
            "pool_address": attrs.get("address", ""),
            "token_symbol": attrs.get("name", "").split("/")[0].strip(),
            "name": attrs.get("name", ""),
            "price_usd": float(attrs.get("base_token_price_usd", 0) or 0),
            "liquidity_usd": float(attrs.get("reserve_in_usd", 0) or 0),
            "volume_24h_usd": float(attrs.get("volume_usd", {}).get("h24", 0) or 0),
            "volume_5m_usd": float(attrs.get("volume_usd", {}).get("m5", 0) or 0),
            "volume_1h_usd": float(attrs.get("volume_usd", {}).get("h1", 0) or 0),
            "price_change_5m": float(attrs.get("price_change_percentage", {}).get("m5", 0) or 0),
            "price_change_1h": float(attrs.get("price_change_percentage", {}).get("h1", 0) or 0),
            "is_trending": False,
        })
    return pools[:limit]


def score_opportunity(pool: dict) -> float:
    """OPTIMIZED V2 Scoring - trending pools use 24h momentum proxy."""
    is_trending = pool.get("is_trending", False)
    
    score = 0.0
    liq = pool.get("liquidity_usd", 0) or 0
    vol = pool.get("volume_24h_usd", 0) or 0
    
    if is_trending:
        # TRENDING POOLS: 24h momentum proxy
        if liq > 0:
            ratio = vol / liq
            score += min(ratio * 667, 40)  # 6% ratio = 40pts
        
        if liq >= 1_000_000:
            score += 30
        elif liq >= 500_000:
            score += 20
        elif liq >= 250_000:
            score += 10
            
        if vol >= 5_000_000:
            score += 20
        elif vol >= 1_000_000:
            score += 10
        elif vol >= 500_000:
            score += 5
    else:
        # NEW POOLS: Full momentum
        chg5 = abs(pool.get("price_change_5m", 0) or 0)
        chg1h = abs(pool.get("price_change_1h", 0) or 0)
        v5m = pool.get("volume_5m_usd", 0) or 0
        v1h = pool.get("volume_1h_usd", 0) or 0
        
        score += min(max(chg5 * 600, 0), 30)
        score += min(max(chg1h * 500, 0), 25)
        
        if v5m >= 150 and v1h > 0 and v1h >= v5m * 1.8:
            pace = (v5m * 12) / v1h
            if 0.3 <= pace <= 5.0:
                score += min(pace * 10, 20)
        
        if liq > 0:
            ratio = vol / liq
            score += min(ratio * 600, 30)
        
        if liq >= 1_000_000:
            score += 20
        elif liq >= 500_000:
            score += 15
        elif liq >= 250_000:
            score += 10
            
        if vol >= 1_000_000:
            score += 15
        elif vol >= 500_000:
            score += 10
        elif vol >= 250_000:
            score += 5
    
    # Skip stable pairs
    name = (pool.get("name", "") or "").lower()
    if any(s in name for s in ["usdc/usdt", "usdt/usdc", "dai/usdc"]):
        score -= 60
    
    return min(max(score, 0), 100)


def scan_opportunities(
    macro_regime: str = "aggressive_hunting",
    min_score: float = 35.0,
) -> list[dict]:
    """Main entry: scan for opportunities."""
    trending = get_trending_pools(limit=30)
    new_pools = get_new_pools(limit=30)
    all_pools = trending + new_pools
    
    # De-dupe by pool_address
    seen = set()
    unique_pools = []
    for p in all_pools:
        addr = p.get("pool_address", "")
        if addr and addr not in seen:
            seen.add(addr)
            unique_pools.append(p)
    
    # Collapse to ONE pool per TOKEN (keep deepest)
    by_token = {}
    for p in unique_pools:
        key = (p.get("token_address") or p.get("token_symbol") or "").lower()
        if key:
            prev = by_token.get(key)
            if prev is None or (p.get("liquidity_usd", 0) or 0) > (prev.get("liquidity_usd", 0) or 0):
                by_token[key] = p
    unique_pools = list(by_token.values())
    
    # Regime-aware threshold
    threshold = min_score
    if macro_regime == "aggressive_hunting":
        threshold = min_score * 0.7
    elif macro_regime == "risk_off":
        threshold = min_score * 1.4
    
    opportunities = []
    for pool in unique_pools:
        liq = pool.get("liquidity_usd", 0) or 0
        vol = pool.get("volume_24h_usd", 0) or 0
        name = (pool.get("name", "") or "").lower()
        
        # Skip stable pairs
        if any(s in name for s in ["usdc/usdt", "usdt/usdc", "dai/usdc"]):
            continue
        
        # Hard filters
        if liq < MIN_LIQUIDITY_USD:
            continue
        if vol < MIN_24H_VOLUME_USD:
            continue
        
        score = score_opportunity(pool)
        
        if score >= threshold:
            pool["score"] = score
            pool["macro_regime"] = macro_regime
            opportunities.append(pool)
    
    opportunities.sort(key=lambda x: x["score"], reverse=True)
    return opportunities[:10]


if __name__ == "__main__":
    opps = scan_opportunities()
    print(f"Opportunities found: {len(opps)}")
    for o in opps[:5]:
        print(f"  {o.get('token_symbol','?')} score={o['score']:.1f} liq=${o.get('liquidity_usd',0):,.0f} vol=${o.get('volume_24h_usd',0):,.0f}")