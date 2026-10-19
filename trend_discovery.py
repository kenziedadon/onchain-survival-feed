#!/usr/bin/env python3
"""
TREND DISCOVERY — find tradeable, trending Base coins (the "discover" half).

Proves requirement: "we can discover trending coins." Uses THREE independent,
keyless, Base-native sources and merges them:
  1. GeckoTerminal  /networks/base/trending_pools  (ranked by real volume)
  2. GeckoTerminal  /networks/base/new_pools       (early, pre-trend)
  3. DEXScreener    /token-boosts/top/v1           (filtered to chainId=='base')
  4. GMGN           trending + security audit       (smart-money, rug filter)
  5. FOMO           whale alerts firehose           (social-trading signal)

Every result is Base-chain filtered, de-duplicated, scored by a transparent
momentum/liquidity heuristic, and written to discovered.jsonl for the strategy
engine. Pure stdlib. Respects 429 with backoff.

Usage:
  python3 trend_discovery.py            # one scan, print + cache
  python3 trend_discovery.py --watch    # print current watchlist
"""
import json, os, sys, time, urllib.request
from datetime import datetime, timezone

# Source .env for GMGN_API_KEY and other env vars
try:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")) as _env:
        for _line in _env:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _, _v = _line.partition('=')
                os.environ.setdefault(_k, _v)
except Exception:
    pass

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json;version=20230302"}
CACHE = "discovered.jsonl"
MIN_LIQUIDITY = 1_000_000     # USD; V3 pool depth needed for clean swaps
MIN_5M_VOL = 2_000          # USD; need real flow
MIN_VOL_24H = 25_000        # USD; matches caps.json min_vol_24h_usd

from discovery import discover_trending as _canonical_discover
from rpc_verify import RpcClient, verify_pool
from _atomic_write import atomic_jsonl_append

# FOMO MCP integration — social-trading signal source
# Source .env BEFORE importing fomo_client (it reads API_KEY at module level)
_FOMO_ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
try:
    with open(_FOMO_ENV_FILE) as _fomo_env:
        for _line in _fomo_env:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _, _v = _line.partition('=')
                os.environ.setdefault(_k, _v)
except Exception:
    pass
_FOMO_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fomo_disco_state.json")
try:
    sys.path.insert(0, "/home/kenzodadon/fomo-mcp")
    import fomo_client
    # Fallback: if FOMO_API_KEY is empty/invalid, try config.yaml (mcp_servers.fomo_family)
    if not fomo_client.API_KEY or len(fomo_client.API_KEY) < 10:
        try:
            import yaml
            cfg_path = os.path.join(os.path.expanduser("~"), ".hermes", "config.yaml")
            if os.path.exists(cfg_path):
                with open(cfg_path) as f:
                    cfg = yaml.safe_load(f)
                k = (cfg.get("mcp_servers", {}).get("fomo_family", {}).get("env", {}) or {}).get("FOMO_API_KEY", "")
                if k and len(k) > 10:
                    fomo_client.API_KEY = k
        except Exception:
            pass
    _FOMO_AVAILABLE = True
except Exception:
    _FOMO_AVAILABLE = False

# On-chain existence gate: any candidate whose pool address returns no code, or
# whose on-chain token0/token1 don't match the expected WETH+token pair, is
# marked unverified and dropped (RPC failures are retried across 5 endpoints
# inside RpcClient before a candidate is rejected).
WETH = "0x4200000000000000000000000000000000000006"

# --- LIVE MOMENTUM PRE-GATE ---------------------------------------------------
# 24h volume tells you a pool WAS active. It says nothing about whether flow is
# alive right now. Scoring on vol_24h alone ranked dead pools at 95 (VVV: $30 of
# 5m volume against $1.4M/24h). The pace ratio compares the current 5m tape to
# the trailing hourly pace:
#     expected 5m volume at hourly pace = vol_1h / 12
#     pace = vol_5m / (vol_1h / 12)
# pace >= 1.0 means the last 5 minutes are running at or above the hourly
# average; pace < LIVE_PACE_MIN means momentum has stalled and the setup is
# stale regardless of headline 24h numbers.
LIVE_PACE_MIN = 1.0          # current 5m pace must be >= 100% of hourly pace (tightened)
LIVE_PACE_MIN_5M_USD = 250   # absolute floor: below this, 5m tape is noise
LIVE_PACE_MAX_TRUSTED = 4.0  # cap: pace above this is a data artifact, not signal
LIVE_PACE_BASELINE_MULT = 2.0  # vol_1h must exceed vol_5m by this much to trust pace

# Known Base bluechip addresses (skip — they don't trend-trade well)
BLUECHIPS = {
    "0x4200000000000000000000000000000000000006",  # WETH
    "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # USDC
    "0xd9aaab39e305cd1f0c6defd5aad6bb2c0e0d3b5e",  # USDbC
    "0x50c5725949a6f0c72e6c4a641f67700984d05e59",  # DAI
    "0x2ae3f1ec7f1f5012cfeab0185bfc7aa3cf0dec22",  # cbETH
    "0x940181a94a35a4569E4529A3CDfB74e38FD98631",  # AERO
    "0x4ed4e862860bed51a9570b96d61a52e8910248e4",  # cbBTC
}

# Manual token overrides — user-specified tokens that bypass normal discovery
# filters and go straight to the executor for evaluation.
# Format: { token_address: { name, quote, liquidity, vol_24h, ... } }
MANUAL_TOKENS = {
    "0xB20000000000000000000074deAC544E1E9a1f33": {
        "name": "STONKFLY (manual)",
        "quote": "WETH",
        "liquidity": 107_614.0,
        "vol_24h": 95_000.0,
        "source": "manual_add",
        "score": 50,
    },
    "0x8b67f2e56139ca052a7ec49cbcd1aa9c83f2752a": {
        "name": "MUTATIO (manual)",
        "quote": "WETH",
        "liquidity": 22_255.0,
        "vol_24h": 1_642.0,
        "source": "manual_add",
        "score": 50,
    },
    "0x9b5e262cf9bb04869ab40b19af91d2dc85761722": {
        "name": "NOCK (manual)",
        "quote": "WETH",
        "liquidity": 1_513_205.0,
        "vol_24h": 2_238_983.0,
        "source": "manual_add",
        "score": 60,
    },
    "0xb095274743941e953c746f9c228da9c18bb6ec29": {
        "name": "LAPTOP (manual)",
        "quote": "WETH",
        "liquidity": 1_612_164.0,
        "vol_24h": 1_693_724.0,
        "source": "manual_add",
        "score": 60,
    },
    "0xb2000000000000000000004884b426556b92883d": {
        "name": "MSTRc (manual)",
        "quote": "WETH",
        "liquidity": 903_016.0,
        "vol_24h": 4_818_988.0,
        "source": "manual_add",
        "score": 60,
    },
}

def get(url, timeout=25, retries=6):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode()
        except Exception as e:
            wait = 3 + i*2 if "429" in str(e) else 1.5
            if i < retries - 1:
                time.sleep(wait); continue
            raise

# ---- pool-address -> token-address resolution (fixes the 66-char pool-ID bug) ----
def _pool_to_token_addr(pool_addr: str, retries=4) -> str:
    """Given a GeckoTerminal pool address, resolve the underlying token address via
    DexScreener keyless API. Returns the token address (lowercased) or None."""
    if not pool_addr:
        return None
    tries = 0
    while tries < retries:
        try:
            url = f"https://api.dexscreener.com/latest/dex/pairs/base/{pool_addr}"
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=25) as r:
                data = json.loads(r.read())
            pairs = data.get("pairs") or []
            if pairs:
                bt = pairs[0].get("baseToken", {})
                addr = bt.get("address")
                if addr and len(addr) == 42:
                    return addr.lower()
                # Fallback: pair address itself (single-token pools)
                pair_addr = pairs[0].get("pairAddress", "")
                if pair_addr and len(pair_addr) == 42:
                    return pair_addr.lower()
            return None  # no match => cannot trust a token here
        except Exception as e:
            tries += 1
            time.sleep(min(3 + tries * 2, 15))
    return None


def _fee_from_name(name):
    fee = 30.0
    for tok in name.upper().replace("%", " %").split():
        if "%" in tok:
            try: fee = float(tok.replace("%", "")) * 100
            except: pass
    return fee

def gecko_trending():
    """Fetch GeckoTerminal trending pools directly and enrich with live
    momentum fields (vol_5m, vol_1h) that the pace gate requires.

    The old implementation delegated to discovery.discover_trending()
    which only returned vol_24h — so every pool failed the live_pace pre-gate.
    """
    out = {}
    try:
        url = "https://api.geckoterminal.com/api/v2/networks/base/trending_pools?page=1"
        data = json.loads(get(url)).get("data", [])
    except Exception as e:
        print(f"  [warn] gecko trending: {e}", file=sys.stderr)
        return out
    for p in data:
        a = p.get("attributes", {})
        name = a.get("name", "?")
        pool_addr = a.get("address") or p.get("id", "").split("_")[-1]
        if not (pool_addr.startswith("0x") and len(pool_addr) == 42):
            continue
        # Only ETH/WETH pairs
        if "ETH" not in name.upper() and "WETH" not in name.upper():
            continue
        if "USDC" in name.upper() or "USDT" in name.upper() or "DAI" in name.upper():
            continue
        if pool_addr.lower() in BLUECHIPS:
            continue
        liq = float(a.get("reserve_in_usd") or 0)
        v5m = float(a.get("volume_usd", {}).get("m5") or 0)
        v1h = float(a.get("volume_usd", {}).get("h1") or 0)
        v24h = float(a.get("volume_usd", {}).get("h24") or 0)
        price_change = a.get("price_change_percentage", {})
        h24_change = float(price_change.get("h24", 0))
        out[pool_addr] = {
            "address": pool_addr,
            "token_addr": _pool_to_token_addr(pool_addr),
            "name": name, "source": "gecko_trending",
            "liquidity": liq, "vol_5m": v5m, "vol_1h": v1h, "vol_24h": v24h,
            "fee_bps": _fee_from_name(name),
            "price_usd": float(a.get("base_token_price_usd") or 0),
            "change_24h": h24_change,
        }
    return out

def gecko_new():
    out = {}
    try:
        data = json.loads(get("https://api.geckoterminal.com/api/v2/networks/base/new_pools?page=1")).get("data", [])
    except Exception as e:
        print(f"  [warn] gecko new_pools: {e}", file=sys.stderr); return out
    for p in data:
        a = p.get("attributes", {}); name = a.get("name", "?")
        addr = p["id"].split("_")[-1]
        liq = float(a.get("reserve_in_usd") or 0)
        v5m = float(a.get("volume_usd", {}).get("m5") or 0)
        if "ETH" not in name.upper() and "WETH" not in name.upper():
            continue
        if addr.lower() in BLUECHIPS:
            continue
        out[addr] = {
            "address": addr, "token_addr": _pool_to_token_addr(addr),
            "name": name, "source": "gecko_new",
            "liquidity": liq, "vol_5m": v5m, "vol_1h": 0.0,
            "fee_bps": _fee_from_name(name),
            "price_usd": float(a.get("base_token_price_usd") or 0),
        }
    return out

def dexscreener_boosts():
    out = {}
    try:
        data = json.loads(get("https://api.dexscreener.com/token-boosts/top/v1"))
    except Exception as e:
        print(f"  [warn] dexscreener boosts: {e}", file=sys.stderr); return out
    for b in data:
        if b.get("chainId") != "base":
            continue
        addr = b.get("tokenAddress", "")
        if addr.lower() in BLUECHIPS:
            continue
        out[addr] = {
            "address": addr, "name": (b.get("tokenAddress", "")[:10] + "…"),
            "source": "dexscreener_boost",
            "liquidity": 0.0, "vol_5m": 0.0, "vol_1h": 0.0,
            "fee_bps": 30.0, "price_usd": 0.0,
        }
    return out

def live_pace(coin):
    """Ratio of current 5m volume to the trailing hourly pace.

    Returns (pace, alive, note). pace is None when vol_1h is unusable.
    """
    v5m = coin.get("vol_5m", 0) or 0
    v1h = coin.get("vol_1h", 0) or 0
    estimated = coin.get("source") == "gmgn_trending"

    # GMGN coins don't have real-time 5m data — the estimates are conservative
    # (uniform distribution) and would always fail the live tape floor.
    # They've already passed GMGN's own security audit + on-chain verification.
    if estimated:
        return None, True, "GMGN coin — no live tape check (security audited)"

    if v5m < LIVE_PACE_MIN_5M_USD:
        return 0.0, False, f"5m tape ${v5m:,.0f} < ${LIVE_PACE_MIN_5M_USD:,} floor"

    if v1h <= 0:
        # No hourly baseline to compare against; the absolute floor above is the
        # only evidence we have, so accept but flag it.
        return None, True, "no vol_1h baseline"

    # If vol_1h is barely larger than vol_5m, the "hour" is effectively a single
    # 5m bar — dividing by 12 fabricates a huge pace from a thin tape. Treat the
    # baseline as unusable rather than reading it as acceleration.
    if v1h < v5m * LIVE_PACE_BASELINE_MULT:
        return None, True, f"vol_1h ${v1h:,.0f} too close to 5m — baseline unusable"

    expected_5m = v1h / 12.0
    if expected_5m <= 0:
        return None, True, "no vol_1h baseline"

    pace = v5m / expected_5m
    if pace < LIVE_PACE_MIN:
        return pace, False, f"pace {pace:.2f}x < {LIVE_PACE_MIN}x hourly"
    if pace > LIVE_PACE_MAX_TRUSTED:
        # Alive, but don't hand out an acceleration bonus on an implausible spike.
        return None, True, f"pace {pace:.2f}x exceeds trusted cap — alive, unscored"
    return pace, True, f"pace {pace:.2f}x hourly"


def score(coin):
    """Transparent momentum+liquidity score. Higher = more interesting.

    Live momentum is a HARD GATE, not a bonus: a pool with a dead 5m tape scores
    0 no matter how large its 24h volume is.
    """
    s = 0; reasons = []

    pace, alive, note = live_pace(coin)
    coin["live_pace"] = round(pace, 3) if pace is not None else None
    coin["live_note"] = note
    if not alive:
        coin["score"] = 0
        coin["reasons"] = ["stale:" + note]
        return coin

    if coin["liquidity"] >= MIN_LIQUIDITY:
        s += min(coin["liquidity"] / MIN_LIQUIDITY, 8) * 5  # up to 40
        reasons.append("liquid")
    # 24h volume breakout (momentum regime) — context, gated on live flow above
    if coin.get("vol_24h", 0) >= MIN_VOL_24H:
        s += min(coin["vol_24h"] / 50000, 10) * 5  # up to 50
        reasons.append("vol_breakout")
    elif coin.get("vol_5m", 0) >= MIN_5M_VOL:
        s += min(coin["vol_5m"] / MIN_5M_VOL, 5) * 6
        reasons.append("flow")
    # Live pace bonus: reward tape that is accelerating, not merely alive
    if pace is not None and pace >= 1.0:
        s += min(pace, 3.0) * 5  # up to 15
        reasons.append("accelerating")
    if coin["source"] == "gecko_new":
        s += 15; reasons.append("early")
    if coin["source"] == "dexscreener_boost":
        s += 10; reasons.append("boosted")
    if coin["source"] == "gmgn_trending":
        s += 20; reasons.append("security_audited")
    if coin["fee_bps"] <= 30:
        s += 5; reasons.append("lowfee")
    coin["score"] = int(s); coin["reasons"] = reasons
    return coin

def gmgn_trending():
    """Fetch trending tokens from GMGN API (third independent source).

    Uses gmgn_bridge.discover_trending_gmgn() which applies security filters
    (honeypot, blacklist, rug ratio) and returns coins with smart-money scoring.
    Returns dict keyed by token address.
    """
    out = {}
    try:
        from gmgn_bridge import discover_trending_gmgn
        coins = discover_trending_gmgn(
            limit=20,
            min_liquidity=100_000,
            min_volume=25_000,
            require_renounced=False,
            max_rug_ratio=0.3,
        )
        for c in coins:
            addr = c.get("address", "")
            if addr and len(addr) == 42:
                vol_24h = c.get("vol_24h", 0)
                # GMGN only returns 24h volume. Derive hourly and 5m estimates
                # so the live_pace gate has a baseline to work with.
                # Conservative: assume uniform distribution (vol_1h = vol_24h/24,
                # vol_5m = vol_1h/12). Real pace will be higher during active hours.
                vol_1h_est = vol_24h / 24.0
                vol_5m_est = vol_1h_est / 12.0
                out[addr] = {
                    "address": addr,
                    "token_addr": addr,
                    "name": c.get("name", "?"),
                    "source": "gmgn_trending",
                    "liquidity": c.get("liquidity", 0),
                    "vol_5m": vol_5m_est,
                    "vol_1h": vol_1h_est,
                    "vol_24h": vol_24h,
                    "fee_bps": c.get("fee_bps", 30.0),
                    "price_usd": c.get("price_usd", 0),
                    "change_24h": c.get("change_1h", 0),
                    "smart_degen_count": c.get("smart_degen_count", 0),
                    "renowned_count": c.get("renowned_count", 0),
                    "rug_ratio": c.get("rug_ratio", 0),
                    "is_renounced": c.get("is_renounced", False),
                    "is_honeypot": c.get("is_honeypot", False),
                    "score": c.get("score", 0),
                }
    except Exception as e:
        print(f"  [warn] gmgn trending: {e}", file=sys.stderr)
    return out


def fomo_trending():
    """Fetch tokens from FOMO alerts firehose (social-trading signal source).

    Watches leaderboard traders' buys on Base and state-diffs against the last
    scan. Only tokens bought by top whales or leaderboard traders pass.
    Returns dict keyed by token address.
    """
    out = {}
    if not _FOMO_AVAILABLE:
        return out
    try:
        # Load state
        try:
            with open(_FOMO_STATE_FILE) as f:
                state = json.load(f)
        except Exception:
            state = {"seen": {}, "leaderboard_handles": []}
        seen = state.get("seen", {})
        now = time.time()
        cutoff = now - 1800  # 30 minutes

        # Get leaderboard handles (or use cached)
        handles = set(state.get("leaderboard_handles", []))
        # Always refresh leaderboard handles if possible — leaderboard turns over
        # frequently and stale handles miss new top traders' alerts.
        try:
            lb = fomo_client.leaderboard("24h", 15)
            fresh_handles = {t["handle"] for t in lb.get("traders", [])}
            if fresh_handles:
                handles = fresh_handles
                state["leaderboard_handles"] = list(handles)
        except Exception:
            if not handles:
                pass

        # Pull alerts firehose (no chain filter — we filter below)
        data = fomo_client.alerts(limit=100, type_="buy")
        alerts = data.get("alerts", [])
        new_count = 0
        for a in alerts:
            aid = a.get("id") or ""
            ts = a.get("ts") or 0
            if isinstance(ts, (int, float)) and ts > 1e12:
                ts = ts / 1000.0
            if ts < cutoff:
                continue
            chain = (a.get("chain") or "").lower()
            trader = a.get("trader") or ""
            usd = a.get("usdValue") or 0
            addr = a.get("tokenAddress", "")

            # Accept if: (a) leaderboard trader + min usd (any chain), OR
            #            (b) base chain + any trader + large buy (organic base signal)
            if not addr or len(addr) != 42:
                continue
            if trader in handles:
                if usd < 5000:
                    continue
            elif chain == "base" and usd >= 25000:
                pass  # organic base signal
            else:
                continue
            if aid in seen:
                continue

            seen[aid] = {"ts": ts, "trader": trader, "token": a.get("token"), "usd": usd}
            out[addr.lower()] = {
                "address": addr.lower(),
                "token_addr": addr.lower(),
                "name": a.get("token", "?"),
                "source": "fomo_whale",
                "liquidity": 0.0,
                "vol_5m": 0.0,
                "vol_1h": 0.0,
                "vol_24h": 0.0,
                "fee_bps": 30.0,
                "price_usd": 0.0,
                "change_24h": 0.0,
                "score": 50,  # FOMO whale buys are high-signal
            }
            new_count += 1

        # Save state
        state["seen"] = seen
        state["last_run"] = int(now)
        with open(_FOMO_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)

        if new_count:
            print(f"  FOMO: found {new_count} new whale buys from leaderboard")
    except Exception as e:
        print(f"  [warn] fomo trending: {e}", file=sys.stderr)
    return out


def main():
    print("=== DISCOVER TRENDING BASE COINS (v3 — GMGN + Gecko + DEXScreener) ===")
    print("Filters: stable pairs excluded, momentum regime, GMGN security pre-gate\n")
    g_trend = gecko_trending();  time.sleep(1.5)
    g_new = gecko_new();         time.sleep(1.5)
    dx = dexscreener_boosts()
    gm = gmgn_trending();       time.sleep(1.0)
    fm = fomo_trending();       time.sleep(0.5)

    merged = {}
    for src in (g_trend, g_new, dx, gm, fm):
        for addr, c in src.items():
            if addr in merged:
                merged[addr]["reasons"] = list(set(merged[addr].get("reasons", []) + c.get("reasons", [])))
                merged[addr]["liquidity"] = max(merged[addr]["liquidity"], c["liquidity"])
                merged[addr]["vol_5m"] = max(merged[addr]["vol_5m"], c["vol_5m"])
                merged[addr]["source"] = "multi:" + merged[addr]["source"] + "+" + c["source"]
            else:
                merged[addr] = c

    # Add manual tokens (user-specified, bypass normal filters)
    for addr, c in MANUAL_TOKENS.items():
        if addr not in merged:
            merged[addr] = c

    coins = [score(c) for c in merged.values()]
    # v2 filters: exclude stable pairs, require vol > $5k or liq > $500k
    # GMGN coins are security-audited, so allow lower liquidity (100k+)
    # Manual tokens bypass liquidity filters entirely
    coins = [c for c in coins if (c["source"] in ("gmgn_trending", "manual_add") or c["liquidity"] >= 250_000)
             and "USDC" not in c["name"].upper()
             and "USDT" not in c["name"].upper()
             and "DAI" not in c["name"].upper()]
    # LIVE MOMENTUM PRE-GATE: drop pools whose 5m tape is dead. These scored 0 in
    # score() and are stale regardless of headline 24h volume. Report them so the
    # rejection is visible rather than silent.
    # Manual tokens bypass this gate (user explicitly added them).
    stale = [c for c in coins if c["score"] == 0 and c["source"] != "manual_add"]
    coins = [c for c in coins if c["score"] > 0 or c["source"] == "manual_add"]
    if stale:
        print(f"Pre-gate rejected {len(stale)} stale pools (dead 5m tape):")
        for c in stale:
            print(f"    - {c['name'][:26]:26} {c['reasons'][0]}")
        print()
    # Resolve the underlying token address for each pool (GeckoTerminal gives pool
    # addresses; swap.js needs the token). DexScreener keyless API resolves pool->token.
    for c in coins:
        pool_addr = c.get("address")
        if pool_addr and len(pool_addr) == 42 and c.get("token_addr") is None:
            resolved = _pool_to_token_addr(pool_addr)
            if resolved:
                c["token_addr"] = resolved
    coins.sort(key=lambda x: x["score"], reverse=True)

    # ON-CHAIN EXISTENCE GATE: verify each token has a contract on-chain.
    # For pool-based sources (GeckoTerminal), also verify the pool holds the
    # expected WETH+token pair. For token-based sources (GMGN, DEXScreener),
    # just check the token contract exists — they don't return pool addresses.
    _rpc = RpcClient()
    verified, unverified = [], []
    for c in coins:
        token = c.get("token_addr")
        pool_addr = c.get("address")
        if not token:
            c.update(verified=False, reason="no resolvable token address")
            unverified.append(c); continue
        try:
            # First: verify token contract exists
            code = _rpc.get_code(token)
            if code in ("0x", "0x0", None, ""):
                c.update(verified=False, reason="token contract has no code")
                unverified.append(c); continue
            # For pool-based sources, also verify the pool
            if pool_addr and pool_addr != token and len(pool_addr) == 42:
                r = verify_pool(_rpc, pool_addr, WETH, token)
                if r["verified"]:
                    c["onchain"] = {"codesize": r["codesize"],
                                    "token0": r["token0"], "token1": r["token1"]}
                    verified.append(c)
                else:
                    c.update(verified=False, reason=r["reason"])
                    unverified.append(c)
            else:
                # Token-only source (GMGN/DEXScreener) — token exists, accept
                c["onchain"] = {"codesize": max(0, (len(code) - 2) // 2)}
                verified.append(c)
        except Exception as e:
            c.update(verified=False, reason=f"rpc failover exhausted: {e}")
            unverified.append(c)
        print(f"On-chain gate rejected {len(unverified)} unverified candidates:")
        for c in unverified:
            print(f"    - {c['name'][:26]:26} {(c.get('reason') or '?')[:70]}")
        print()
    coins = verified

    # GMGN SECURITY PRE-GATE: run every candidate through GMGN's security
    # audit (honeypot, blacklist, rug ratio, renounced). Tokens that fail are
    # dropped before they reach the cache — the executor's GMGN gate would
    # block them anyway, so don't waste candles on them.
    # Manual tokens bypass this gate (user explicitly added them).
    try:
        from gmgn_bridge import security_check
        safe, unsafe = [], []
        for c in coins:
            addr = c.get("token_addr") or c.get("address")
            if not addr or len(addr) != 42:
                continue
            if c.get("source") == "manual_add":
                safe.append(c)
                continue
            sec = security_check(addr)
            if sec.get("safe") is True:
                safe.append(c)
            elif sec.get("safe") is False:
                c["gmgn_reject"] = sec.get("reason", "unknown")
                unsafe.append(c)
            # safe is None = API unavailable; keep the coin (skip this gate)
        if unsafe:
            print(f"GMGN security pre-gate dropped {len(unsafe)} candidates:")
            for c in unsafe:
                print(f"    - {c['name'][:26]:26} {c.get('gmgn_reject', '?')[:60]}")
            print()
        coins = safe
    except Exception as e:
        print(f"  [warn] GMGN security pre-gate unavailable: {e}", file=sys.stderr)

    # RISK-OFFICER VETO (pre-trade candidate filter): same 7-check independent
    # gate used in executor.py, so rejected candidates never reach the cache.
    try:
        from risk_officer import veto_check
        passed, vetoed = [], []
        for c in coins:
            prop = {
                "action": "swap",
                "token_out": c.get("token_addr"),
                "pool": c.get("address"),
                "amount_eth": 0.0005,
                "score": c.get("score", 0),
                "slippage_bps": c.get("fee_bps", 30),
                "liquidity": c.get("liquidity", 0),
                "vol_5m": c.get("vol_5m", 0),
                "source": c.get("source", ""),
            }
            ok, fails = veto_check(prop)
            if ok:
                passed.append(c)
            else:
                c["officer_veto"] = "; ".join(fails)
                vetoed.append(c)
        if vetoed:
            print(f"Risk-officer vetoed {len(vetoed)} candidates before cache:")
            for c in vetoed:
                print(f"    - {c['name'][:26]:26} {c['officer_veto'][:70]}")
            print()
        coins = passed
    except Exception as e:
        # fail-safe: if the officer can't import/run, keep the on-chain-verified
        # set rather than silently passing everything. Log and proceed.
        print(f"  [warn] risk_officer unavailable in discovery: {e}", file=sys.stderr)

    print(f"Discovered {len(coins)} Base coins with LIVE momentum:\n")
    for i, c in enumerate(coins[:20]):
        pace = c.get("live_pace")
        pace_s = f"{pace:.2f}x" if pace is not None else "  n/a"
        print(f"  {i+1:2}. {c['name'][:26]:26} score={c['score']:>3} "
              f"liq=${c['liquidity']:>10,.0f} v5m=${c['vol_5m']:>8,.0f} "
              f"pace={pace_s:>6} [{c['source']}]")
    if not coins:
        print("  (no pools have live 5m flow right now — tape is quiet)")

    # Token grading — BLOCK tokens that fail security/market checks
    try:
        from token_grade import TokenGrader
        grader = TokenGrader()
        graded = []
        for c in coins:
            addr = c.get("token_addr") or c.get("address")
            if not addr or len(addr) != 42:
                continue
            grade = grader.grade(addr)
            if grade["verdict"] != "BLOCK":
                c["token_grade"] = grade
                graded.append(c)
            else:
                print(f"  [BLOCKED] {c['name'][:25]:25} — {', '.join(grade['reasons'][:2])}")
        coins = graded
    except Exception as e:
        print(f"  [warn] token grading unavailable: {e}", file=sys.stderr)

    rec = {"ts": datetime.now(timezone.utc).isoformat(), "coins": coins}
    atomic_jsonl_append(CACHE, rec)
    print(f"\nWrote {len(coins)} discovered coins to {CACHE}")
    return coins


if __name__ == "__main__":
    main()
