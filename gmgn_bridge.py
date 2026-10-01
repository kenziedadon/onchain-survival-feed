#!/usr/bin/env python3
"""
gmgn_bridge.py — GMGN API bridge for the Base trend bot.

Wraps gmgn-cli (subprocess) to provide:
  1. Trending discovery with smart-money / KOL / rug-ratio filters
  2. Security pre-gate (honeypot, renounced, blacklist, tax mismatch)
  3. OHLCV kline data (alternative to GeckoTerminal)
  4. Smart-money holder / KOL counts as scoring factors
  5. Wallet portfolio cross-check

All functions are pure stdlib + subprocess. No external pip deps.
API key is read from GMGN_API_KEY env var (set in .env or shell).
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _run(cmd, timeout=60):
    """Run a gmgn-cli command, return parsed JSON or None."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "NODE_PATH": os.path.join(HERE, "node_modules")}
        )
        if result.returncode != 0:
            return None
        out = result.stdout.strip()
        if not out:
            return None
        # Handle multi-line: find first { to last }
        start = out.find('{')
        end = out.rfind('}')
        if start == -1 or end == -1 or end <= start:
            return None
        return json.loads(out[start:end+1])
    except Exception:
        return None


def _api_key():
    """Return GMGN_API_KEY from environment."""
    return os.environ.get("GMGN_API_KEY", "")


# ---------------------------------------------------------------------------
# 1. TRENDING DISCOVERY
# ---------------------------------------------------------------------------

def discover_trending_gmgn(
    interval="1h",
    limit=30,
    min_liquidity=100_000,
    min_volume=50_000,
    require_renounced=True,
    max_rug_ratio=0.3,
    min_smart_degen=0,
    min_renowned=0,
    timeout=60,
):
    """
    Discover trending tokens on Base via GMGN.

    Returns list of normalized coin dicts compatible with the executor's
    expected schema (name, address, quote, liquidity, vol_24h, score, etc.).

    Filters:
      - require_renounced: only renounced tokens (no admin backdoors)
      - max_rug_ratio: exclude tokens with rug_ratio > this
      - min_smart_degen: minimum smart-money holder count
      - min_renowned: minimum KOL holder count
    """
    if not _api_key():
        return []

    cmd = [
        "gmgn-cli", "market", "trending",
        "--chain", "base",
        "--interval", interval,
        "--limit", str(limit),
        "--min-liquidity", str(min_liquidity),
        "--min-volume", str(min_volume),
        "--filter", "not_honeypot",
        "--raw",
    ]

    data = _run(cmd, timeout=timeout)
    if not data:
        return []

    rank_list = data.get("data", {}).get("rank", [])
    out = []
    for t in rank_list:
        addr = t.get("address", "")
        if not (addr.startswith("0x") and len(addr) == 42):
            continue

        # Security filters
        rug_ratio = float(t.get("rug_ratio") or 0)
        if rug_ratio > max_rug_ratio:
            continue

        is_renounced = bool(t.get("is_renounced"))
        if require_renounced and not is_renounced:
            continue

        is_honeypot = bool(t.get("is_honeypot"))
        if is_honeypot:
            continue

        is_blacklisted = bool(t.get("is_blacklist") or t.get("blacklist"))
        if is_blacklisted:
            continue

        smart_degen = int(t.get("smart_degen_count") or 0)
        if smart_degen < min_smart_degen:
            continue

        renowned = int(t.get("renowned_count") or 0)
        if renowned < min_renowned:
            continue

        # Normalize to executor schema
        liq = float(t.get("liquidity") or 0)
        vol = float(t.get("volume") or 0)
        price = float(t.get("price") or 0)
        change_1h = float(t.get("price_change_percent") or 0)
        market_cap = float(t.get("market_cap") or 0)
        holder_count = int(t.get("holder_count") or 0)
        top10_rate = float(t.get("top_10_holder_rate") or 0)
        swaps = int(t.get("swaps") or 0)
        buys = int(t.get("buys") or 0)
        sells = int(t.get("sells") or 0)

        # Score: combine momentum, smart money, liquidity
        score = 0
        if liq >= 500_000:
            score += min(liq / 500_000, 8) * 5
        if vol > 0:
            score += min(vol / 50_000, 10) * 5
        if change_1h > 5:
            score += 10
        elif change_1h > 2:
            score += 5
        # Smart money bonus
        score += min(smart_degen, 10) * 2
        score += min(renowned, 10) * 1.5
        # Penalize high top-10 concentration
        if top10_rate > 0.5:
            score -= 10

        # Determine quote token from launch_quote_address
        launch_quote = t.get("launch_quote_address", "")
        if launch_quote.lower() == "0x4200000000000000000000000000000000000006":
            quote = "WETH"
        elif launch_quote.lower() == "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913":
            quote = "USDC"
        else:
            quote = "WETH"  # default

        out.append({
            "name": t.get("symbol", "?"),
            "address": addr,
            "quote": quote,
            "liquidity": liq,
            "vol_24h": vol,
            "score": int(score),
            "change_1h": change_1h,
            "price_usd": price,
            "market_cap": market_cap,
            "holder_count": holder_count,
            "top_10_holder_rate": top10_rate,
            "swaps": swaps,
            "buys": buys,
            "sells": sells,
            "smart_degen_count": smart_degen,
            "renowned_count": renowned,
            "rug_ratio": rug_ratio,
            "is_renounced": is_renounced,
            "is_honeypot": is_honeypot,
            "source": "gmgn_trending",
            "fee_bps": 30.0,
        })

    out.sort(key=lambda x: x["score"], reverse=True)
    return out


# ---------------------------------------------------------------------------
# 2. SECURITY PRE-GATE
# ---------------------------------------------------------------------------

def security_check(token_addr, timeout=30):
    """
    Run GMGN security audit on a token.

    Returns dict:
      {
        "safe": bool or None,  # None = unknown (API error), False = unsafe, True = safe
        "is_honeypot": bool,
        "is_renounced": bool,
        "is_blacklisted": bool,
        "sell_tax": str,
        "buy_tax": str,
        "rug_ratio": float,
        "risk_score": int,
        "risk_label": str,
        "reason": str,  # human-readable if unsafe
      }
    """
    if not _api_key():
        return {"safe": None, "reason": "no_api_key"}

    cmd = [
        "gmgn-cli", "token", "security",
        "--chain", "base",
        "--address", token_addr,
        "--raw",
    ]
    data = _run(cmd, timeout=timeout)
    if not data:
        # API error — return None (unknown) instead of False (unsafe)
        # so executor can decide whether to skip or proceed with caution
        return {"safe": None, "reason": "api_error"}

    is_honeypot = bool(data.get("is_honeypot") or data.get("honeypot"))
    is_renounced = bool(data.get("is_renounced") or data.get("renounced"))
    is_blacklisted = bool(data.get("is_blacklist") or data.get("blacklist"))
    rug_ratio = float(data.get("rug_ratio") or 0)
    risk_score = int(data.get("gmgn_risk_score") or data.get("risk_score") or 0)
    risk_label = data.get("gmgn_risk_label") or data.get("risk_label") or "unknown"
    sell_tax = str(data.get("sell_tax") or "0")
    buy_tax = str(data.get("buy_tax") or "0")

    reasons = []
    if is_honeypot:
        reasons.append("honeypot")
    if is_blacklisted:
        reasons.append("blacklisted")
    if not is_renounced:
        reasons.append("not_renounced")
    if rug_ratio > 0.3:
        reasons.append(f"high_rug_ratio={rug_ratio}")
    if risk_score > 50:
        reasons.append(f"high_risk_score={risk_score}")

    return {
        "safe": len(reasons) == 0,
        "is_honeypot": is_honeypot,
        "is_renounced": is_renounced,
        "is_blacklisted": is_blacklisted,
        "sell_tax": sell_tax,
        "buy_tax": buy_tax,
        "rug_ratio": rug_ratio,
        "risk_score": risk_score,
        "risk_label": risk_label,
        "reason": "; ".join(reasons) if reasons else "safe",
    }


# ---------------------------------------------------------------------------
# 3. KLINE DATA (alternative OHLCV source)
# ---------------------------------------------------------------------------

def fetch_kline_gmgn(token_addr, resolution="1h", limit=120, timeout=30):
    """
    Fetch OHLCV kline data from GMGN.

    Returns list of candle dicts: {"ts": int, "o": float, "h": float, "l": float, "c": float, "v": float}
    Sorted by timestamp ascending.
    """
    if not _api_key():
        return []

    # GMGN kline uses --from / --to; default to last N hours
    now = int(time.time())
    from_ts = now - (limit * 3600)  # approximate

    cmd = [
        "gmgn-cli", "market", "kline",
        "--chain", "base",
        "--address", token_addr,
        "--resolution", resolution,
        "--from", str(from_ts),
        "--to", str(now),
        "--raw",
    ]
    data = _run(cmd, timeout=timeout)
    if not data:
        return []

    # Parse kline response
    klines = data.get("data") or []
    if isinstance(klines, dict):
        klines = klines.get("kline") or klines.get("data") or []

    out = []
    for k in klines:
        if isinstance(k, list) and len(k) >= 6:
            out.append({
                "ts": int(k[0]),
                "o": float(k[1]),
                "h": float(k[2]),
                "l": float(k[3]),
                "c": float(k[4]),
                "v": float(k[5]),
            })
        elif isinstance(k, dict):
            out.append({
                "ts": int(k.get("timestamp") or k.get("ts") or 0),
                "o": float(k.get("open") or k.get("o") or 0),
                "h": float(k.get("high") or k.get("h") or 0),
                "l": float(k.get("low") or k.get("l") or 0),
                "c": float(k.get("close") or k.get("c") or 0),
                "v": float(k.get("volume") or k.get("v") or 0),
            })

    out.sort(key=lambda x: x["ts"])
    return out


# ---------------------------------------------------------------------------
# 4. SMART MONEY / KOL HOLDER ANALYSIS
# ---------------------------------------------------------------------------

def get_smart_money_stats(token_addr, limit=20, timeout=30):
    """
    Get smart-money and KOL holder counts for a token.

    Returns dict:
      {
        "smart_degen_count": int,
        "renowned_count": int,
        "top_holder_concentration": float,
        "top_smart_holders": list[dict],
      }
    """
    if not _api_key():
        return {"smart_degen_count": 0, "renowned_count": 0, "top_holder_concentration": 0, "top_smart_holders": []}

    # Get smart degen holders
    cmd_smart = [
        "gmgn-cli", "token", "holders",
        "--chain", "base",
        "--address", token_addr,
        "--limit", str(limit),
        "--tag", "smart_degen",
        "--raw",
    ]
    smart_data = _run(cmd_smart, timeout=timeout)
    smart_holders = smart_data.get("list", []) if smart_data else []

    # Get renowned holders
    cmd_renowned = [
        "gmgn-cli", "token", "holders",
        "--chain", "base",
        "--address", token_addr,
        "--limit", str(limit),
        "--tag", "renowned",
        "--raw",
    ]
    renowned_data = _run(cmd_renowned, timeout=timeout)
    renowned_holders = renowned_data.get("list", []) if renowned_data else []

    # Calculate concentration from top smart holders
    total_pct = sum(float(h.get("amount_percentage") or 0) for h in smart_holders)

    return {
        "smart_degen_count": len(smart_holders),
        "renowned_count": len(renowned_holders),
        "top_holder_concentration": total_pct,
        "top_smart_holders": smart_holders[:5],
    }


# ---------------------------------------------------------------------------
# 5. WALLET PORTFOLIO CROSS-CHECK
# ---------------------------------------------------------------------------

def get_wallet_holdings(wallet_addr, timeout=30):
    """
    Get wallet token holdings via GMGN.

    Returns list of holding dicts with token info + value.
    """
    if not _api_key():
        return []

    cmd = [
        "gmgn-cli", "portfolio", "holdings",
        "--chain", "base",
        "--address", wallet_addr,
        "--raw",
    ]
    data = _run(cmd, timeout=timeout)
    if not data:
        return []

    return data.get("list") or data.get("data") or []


# ---------------------------------------------------------------------------
# CLI test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="GMGN bridge test")
    parser.add_argument("--trending", action="store_true", help="Test trending discovery")
    parser.add_argument("--security", type=str, help="Security check for token address")
    parser.add_argument("--kline", type=str, help="Kline data for token address")
    parser.add_argument("--holders", type=str, help="Smart money stats for token address")
    parser.add_argument("--portfolio", type=str, help="Wallet holdings")
    args = parser.parse_args()

    if args.trending:
        coins = discover_trending_gmgn(limit=10)
        print(json.dumps(coins, indent=2))
    elif args.security:
        result = security_check(args.security)
        print(json.dumps(result, indent=2))
    elif args.kline:
        candles = fetch_kline_gmgn(args.kline, limit=24)
        print(json.dumps(candles[:5], indent=2))
        print(f"... {len(candles)} total candles")
    elif args.holders:
        stats = get_smart_money_stats(args.holders)
        print(json.dumps(stats, indent=2))
    elif args.portfolio:
        holdings = get_wallet_holdings(args.portfolio)
        print(json.dumps(holdings, indent=2))
    else:
        parser.print_help()