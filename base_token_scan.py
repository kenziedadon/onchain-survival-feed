#!/usr/bin/env python3
"""
base_token_scan.py — Hermes Vanguard security scanner (DexScreener + on-chain owner check).

Read-only. No keys, no signing. Emits a JSON risk assessment the vanguard daemon parses.

Usage:
  python3 base_token_scan.py <token_address> [--json] [--floor 25000]

Exit code: 0 if scan completed (regardless of PASS/FAIL), 2 on fatal input/usage error.
The verdict lives in JSON:  risk_assessment.status  -> PASS | WARN | FAIL

Free APIs only:
  - DexScreener token-pairs (no key)
  - Base public RPC eth_call for ERC20 owner() renouncement check
"""
import sys, os, json, argparse, urllib.request, urllib.error, time

DEX = "https://api.dexscreener.com/token-pairs/v1/base/"
TOKENS = "https://api.dexscreener.com/latest/dex/tokens/"
RPCS = [
    "https://mainnet.base.org",
    "https://base.llamarpc.com",
    "https://base.meowrpc.com",
    "https://1rpc.io/base",
]
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

# Addresses that mean "renounced / burned" for an ERC20 owner()
RENUNCED = {
    "0x0000000000000000000000000000000000000000",
    "0x0000000000000000000000000000000000000001",
    "0x000000000000000000000000000000000000dead",
    "0x000000000000000000000000000000000000dEaD",
}
OWNER_SEL = "0x8da5cb5b"  # owner()
HTTP_TO = 8


def _http_get(url, timeout=HTTP_TO):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _rpc_owner(token):
    """Return owner address (lower) or None on any failure."""
    data = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": token, "data": OWNER_SEL}, "latest"],
    }).encode()
    for ep in RPCS:
        try:
            req = urllib.request.Request(ep, data=data, headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=HTTP_TO) as r:
                res = json.loads(r.read().decode())
            if "result" in res and res["result"] and res["result"] != "0x":
                h = res["result"]
                # last 20 bytes = address
                addr = "0x" + h[-40:].lower()
                return addr
        except Exception:
            continue
    return None


def _best_pair(pairs):
    if not pairs:
        return None
    # Prefer Aerodrome/Uniswap v3, then highest liquidity
    def keyf(p):
        liq = float(p.get("liquidity", {}).get("usd") or 0)
        return liq
    return max(pairs, key=keyf)


def scan(token, floor):
    reasons = []
    score = 100
    hard_fail = False

    # ---- DexScreener: base-pairs endpoint, fall back to all-chains tokens ----
    pairs = []
    rate_limited = False
    try:
        pairs = _http_get(DEX + token)
        if not isinstance(pairs, list):
            pairs = []
    except urllib.error.HTTPError as e:
        if e.code == 429:
            rate_limited = True
        pairs = []
    except Exception:
        pairs = []

    if not pairs and not rate_limited:
        try:
            resp = _http_get(TOKENS + token)
            allp = resp.get("pairs") or [] if isinstance(resp, dict) else []
            pairs = [p for p in allp if p.get("chainId") == "base"]
        except urllib.error.HTTPError as e:
            if e.code == 429:
                rate_limited = True
        except Exception:
            pass

    if rate_limited:
        return {"token_address": token, "error": "rate_limited",
                "risk_assessment": {"status": "FAIL", "score": 0,
                                     "max_recommended_trade_eth": 0.0,
                                     "reasons": ["DexScreener rate limit (429)"]},
                "market_telemetry": {}}

    pair = _best_pair(pairs)
    if not pair:
        return {"token_address": token, "error": "no_pair",
                "risk_assessment": {"status": "FAIL", "score": 0,
                                     "max_recommended_trade_eth": 0.0,
                                     "reasons": ["no Base pair found on DexScreener"]},
                "market_telemetry": {}}

    liq = float(pair.get("liquidity", {}).get("usd") or 0)
    price = float(pair.get("priceUsd") or 0)
    vol = float(pair.get("volume", {}).get("h24") or 0)
    fdv = float(pair.get("fdv") or 0)
    created = int(pair.get("pairCreatedAt") or 0)
    base = pair.get("baseToken", {})
    quote = pair.get("quoteToken", {})
    labels = pair.get("labels") or []
    txns = pair.get("txns", {}).get("h24", {})
    buys = int(txns.get("buys") or 0)
    sells = int(txns.get("sells") or 0)

    # ---- Liquidity floor ----
    if liq < floor:
        hard_fail = True
        score -= 60
        reasons.append(f"liquidity ${liq:,.0f} < floor ${floor:,.0f}")
    elif liq < floor * 4:
        score -= 15
        reasons.append(f"thin liquidity ${liq:,.0f}")

    # ---- Price sanity ----
    if price <= 0:
        hard_fail = True
        score -= 40
        reasons.append("price_usd <= 0")

    # ---- Volume / liquidity ratio ----
    if liq > 0 and (vol / liq) < 0.05:
        score -= 10
        reasons.append(f"low volume/liquidity ratio {vol/liq:.2f}")

    # ---- Pair age (rug risk) ----
    if created:
        age_h = (int(time.time() * 1000) - created) / 3_600_000
        if age_h < 1:
            score -= 20
            reasons.append(f"pair age {age_h:.1f}h (very new)")
        elif age_h < 6:
            score -= 8
            reasons.append(f"pair age {age_h:.1f}h (new)")

    # ---- Sell pressure present? (honeypot heuristic) ----
    if buys > 0 and sells == 0:
        score -= 25
        reasons.append("buys present but 0 sells in 24h (possible sell lock)")

    # ---- On-chain owner renouncement ----
    owner = _rpc_owner(token)
    if owner is None:
        score -= 10
        reasons.append("owner() unreadable (RPC fail) — treat as unknown")
    elif owner in RENUNCED:
        reasons.append("owner renounced ✓")
    else:
        score -= 15
        reasons.append(f"owner NOT renounced ({owner[:10]}…) — mint/rug risk")

    # ---- Verified/explicitly-flagged ----
    if "VERIFIED" in [str(l).upper() for l in labels]:
        reasons.append("DexScreener VERIFIED label ✓")
    else:
        score -= 5
        reasons.append("no VERIFIED label")

    score = max(0, min(100, score))

    if hard_fail:
        status = "FAIL"
    elif score >= 80:
        status = "PASS"
    else:
        status = "WARN"

    # Recommended spend: capped by liquidity depth, never above a sane max
    max_rec = 0.0
    if status == "PASS" and price > 0:
        eth_price = 1.0 / price if price > 0 else 0  # not used; keep simple
        # cap at 0.1% of liquidity in ETH terms, hard cap 0.0005
        liq_eth_cap = (liq * 0.001) / 2600.0  # assume ~$2600/ETH for depth cap
        max_rec = min(0.0005, max(0.0, liq_eth_cap))
        if max_rec <= 0:
            max_rec = 0.0005

    return {
        "token_address": token,
        "risk_assessment": {
            "status": status,
            "score": score,
            "max_recommended_trade_eth": round(max_rec, 6),
            "reasons": reasons,
        },
        "market_telemetry": {
            "price_usd": price,
            "liquidity_usd": liq,
            "volume_24h": vol,
            "fdv": fdv,
            "pair_age_h": round(age_h, 1) if created else None,
            "buys_24h": buys,
            "sells_24h": sells,
            "dex": pair.get("dexId"),
            "pair": pair.get("pairAddress"),
            "base_symbol": base.get("symbol"),
            "quote_symbol": quote.get("symbol"),
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("token_address")
    ap.add_argument("--json", action="store_true", help="emit JSON (always emitted; flag kept for compat)")
    ap.add_argument("--floor", type=float, default=25000.0, help="liquidity floor USD (default 25000)")
    args = ap.parse_args()

    tok = args.token_address.strip()
    if not tok.startswith("0x") or len(tok) != 42:
        print(json.dumps({"error": "invalid_token_address", "token_address": tok}, indent=2))
        sys.exit(2)

    out = scan(tok, args.floor)
    print(json.dumps(out, indent=2))
    sys.exit(0)


if __name__ == "__main__":
    main()
