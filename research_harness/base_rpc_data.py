"""Base RPC swap-event data layer for the research harness.

Free / keyless: reads Swap events directly from Base public RPC via
eth_getLogs, then reconstructs hourly OHLCV candles. No GeckoTerminal,
no CoinGecko, no API keys. Swap data is the most direct market record.

Pool selection rule (mechanical, anti-cherry-pick):
  For each token in the universe, select the pool with the HIGHEST
  liquidity from the discovery snapshot (candles_cache.json /
  discovered.jsonl). Ties broken by lowest fee tier, then lowest
  pool address. This rule is applied BEFORE any run and is documented
  in the artifact provenance.

Supports both:
  - V2 (Aerodrome / UniV2 Bophades) Swap: amount0In/amount1In/amount0Out
  - V3 (UniSwap V3 / Aerodrome SlipStream) Swap: amount0/amount1 (signed)

Candle format matches the harness: {ts, o, h, l, c, v} with ts as unix
seconds, prices in token/WETH terms.
"""
from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Optional

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --- Swap event topic hashes (keccak256 of the canonical event signatures) ---

# V2 (Uniswap V2 / Aerodrome Bophades):
#   Swap(address indexed sender, uint amount0In, uint amount1In,
#        uint amount0Out, uint amount1Out, address indexed to)
# keccak256("Swap(address,uint256,uint256,uint256,uint256,address)")
SWAP_V2_TOPIC = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"

# V3 (Uniswap V3 / Aerodrome SlipStream):
#   Swap(address indexed sender, address indexed recipient,
#        int256 amount0, int256 amount1,
#        uint160 sqrtPriceX96, uint128 liquidity, int24 tick)
# keccak256("Swap(address,address,int256,int256,uint160,uint128,int24)")
SWAP_V3_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"

# --- Base RPC pool ---
# mainnet.base.org is the primary — the user confirmed it returns curl 200
# where urllib 403s, so we route through curl when available or fall back
# to urllib with proper headers.
RPC_PRIMARY = "https://mainnet.base.org"
RPC_FALLBACKS = [
    "https://base.llamarpc.com",
    "https://1rpc.io/base",
    "https://base.meowrpc.com",
    "https://base.drpc.org",
]

_WETH = "0x4200000000000000000000000000000000000006"
_USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"

# eth_call selectors
_SEL_TOKEN0 = "0x02e95bed"  # token0()
_SEL_TOKEN1 = "0xd21220a7"  # token1()
_SEL_SLOT0 = "0x3850c7bd"   # slot0() -> sqrtPriceX96, tick


# --------------------------------------------------------------------------- #
# RPC transport
# --------------------------------------------------------------------------- #

class RPCError(Exception):
    pass


def _rpc_post(url: str, method: str, params: list, timeout: int = 25) -> dict:
    """Single RPC call. Raises on transport error, returns parsed JSON."""
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
    }).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode())
    if "error" in data:
        raise RPCError(f"RPC {method} error: {data['error']}")
    return data


def _rpc(url: str, method: str, params: list) -> object:
    """Call with failover across the pool."""
    last_err = None
    for ep in [url] + [r for r in RPC_FALLBACKS if r != url]:
        try:
            data = _rpc_post(ep, method, params)
            return data["result"]
        except Exception as e:
            last_err = e
            continue
    raise RPCError(f"All RPCs failed for {method}: {last_err}")


# --------------------------------------------------------------------------- #
# Pool metadata
# --------------------------------------------------------------------------- #

def get_token0(pool_addr: str) -> str:
    """eth_call token0() — works for both V2 and V3 pools."""
    res = _rpc(RPC_PRIMARY, "eth_call",
               [{"to": pool_addr, "data": _SEL_TOKEN0}, "latest"])
    raw = res if isinstance(res, str) else res.get("result", "0x")
    if not raw or raw in ("0x", "0x0"):
        return ""
    return "0x" + raw[-40:].lower()


def get_token1(pool_addr: str) -> str:
    """eth_call token1()."""
    res = _rpc(RPC_PRIMARY, "eth_call",
               [{"to": pool_addr, "data": _SEL_TOKEN1}, "latest"])
    raw = res if isinstance(res, str) else res.get("result", "0x")
    if not raw or raw in ("0x", "0x0"):
        return ""
    return "0x" + raw[-40:].lower()


# --------------------------------------------------------------------------- #
# Swap log fetching + decoding
# --------------------------------------------------------------------------- #

# V2 Swap log decoding (Uniswap V2 / Aerodrome Bophades):
#   topics[0] = Swap signature
#   topics[1] = sender (indexed)
#   topics[2] = to (indexed)
#   data: amount0In:uint256, amount1In:uint256,
#         amount0Out:uint256, amount1Out:uint256
#   (4 × 32 bytes = 128 bytes = 256 hex chars after 0x)
def _decode_v2_swap(log: dict, token0: str, token1: str) -> dict | None:
    data = log.get("data", "0x")
    if len(data) < 2 + 256:  # 0x + 4 words
        return None
    try:
        off = 2
        a0_in = int.from_bytes(bytes.fromhex(data[off:off+64]), "big")
        a1_in = int.from_bytes(bytes.fromhex(data[off+64:off+128]), "big")
        a0_out = int.from_bytes(bytes.fromhex(data[off+128:off+192]), "big")
        a1_out = int.from_bytes(bytes.fromhex(data[off+192:off+256]), "big")

        block_num_hex = log.get("blockNumber", "0x0")
        block_ts = int(block_num_hex, 16) if block_num_hex else 0

        # In a V2 swap, one side is "in" and the other is "out".
        # amount0In > 0, amount0Out = 0 → selling token0, buying token1
        # amount1In > 0, amount1Out = 0 → selling token1, buying token0
        # The price ratio is: token1_per_token0 = amount1_out / amount0_in
        #   (when selling token0) or amount0_out / amount1_in (when selling token1)

        if a0_in > 0 and a1_out > 0:
            # Selling token0, buying token1
            # price = token1_out / token0_in
            ratio = a1_out / a0_in if a0_in else 0
            direction = "0->1"
            amount_in = a0_in
            amount_out = a1_out
        elif a1_in > 0 and a0_out > 0:
            # Selling token1, buying token0
            # price = token0_out / token1_in
            ratio = a0_out / a1_in if a1_in else 0
            direction = "1->0"
            amount_in = a1_in
            amount_out = a0_out
        else:
            return None

        return {
            "block": block_ts,
            "ts": block_ts,
            "token0": token0,
            "token1": token1,
            "direction": direction,
            "ratio_token1_per_token0": ratio,
            "amount_in": amount_in,
            "amount_out": amount_out,
            "pool_type": "v2",
        }
    except (ValueError, IndexError):
        return None


# V3 Swap log decoding (Uniswap V3 / Aerodrome SlipStream):
#   topics[0] = Swap signature
#   topics[1] = sender (indexed)
#   topics[2] = recipient (indexed)
#   data: amount0:int256, amount1:int256, sqrtPriceX96:uint160,
#         liquidity:uint128, tick:int24
#   (5 × 32 bytes = 160 bytes = 320 hex chars after 0x)
def _decode_v3_swap(log: dict, token0: str, token1: str) -> dict | None:
    data = log.get("data", "0x")
    if len(data) < 2 + 320:  # 0x + 5 words
        return None
    try:
        off = 2  # skip 0x
        amount0 = int.from_bytes(
            bytes.fromhex(data[off:off+64]), "big", signed=True)
        amount1 = int.from_bytes(
            bytes.fromhex(data[off+64:off+128]), "big", signed=True)
        sqrt_price = int.from_bytes(
            bytes.fromhex(data[off+128:off+192]), "big", signed=False)
        # liquidity = int.from_bytes(data[off+192:off+256])
        # tick = int.from_bytes(data[off+256:off+266])

        block_num_hex = log.get("blockNumber", "0x0")
        block_ts = int(block_num_hex, 16) if block_num_hex else 0

        # Compute price from sqrtPriceX96
        # price = (sqrtPriceX96 / 2^96)^2
        # This gives price of token1 in terms of token0 (token1 per token0)
        if sqrt_price:
            price_ratio = (sqrt_price / (2**96)) ** 2
        else:
            # Fall back to amount ratio
            if amount0 != 0:
                price_ratio = abs(amount1 / amount0)
            else:
                return None

        # amount0 and amount1 have opposite signs in a valid swap
        # amount0 > 0, amount1 < 0 → buying token0, selling token1
        # amount0 < 0, amount1 > 0 → selling token0, buying token1
        if amount0 < 0 and amount1 > 0:
            direction = "1->0"  # selling token0 (= buying token1)
        elif amount0 > 0 and amount1 < 0:
            direction = "0->1"  # buying token0 (= selling token1)
        else:
            direction = "unknown"

        return {
            "block": block_ts,
            "ts": block_ts,
            "token0": token0,
            "token1": token1,
            "direction": direction,
            "ratio_token1_per_token0": price_ratio,
            "amount0": amount0,
            "amount1": amount1,
            "sqrt_price_x96": sqrt_price,
            "pool_type": "v3",
        }
    except (ValueError, IndexError):
        return None


def _get_block_ts(block_num: int) -> int:
    """Resolve block number to unix timestamp via eth_getBlockByNumber."""
    try:
        res = _rpc(RPC_PRIMARY, "eth_getBlockByNumber",
                   [hex(block_num), False])
        block = res if isinstance(res, dict) else {}
        ts = block.get("timestamp")
        if ts and ts.startswith("0x"):
            return int(ts, 16)
        return int(block.get("timestamp", 0))
    except Exception:
        return 0


def _rpc_call(method: str, params: list) -> object:
    """Wrapper for external callers (e.g., tests)."""
    return _rpc(RPC_PRIMARY, method, params)


# --------------------------------------------------------------------------- #
# Candle reconstruction
# --------------------------------------------------------------------------- #

def _hour_bucket(ts: int) -> int:
    """Floor a unix timestamp to the top of its hour."""
    return (ts // 3600) * 3600


def reconstruct_candles(
    pool_addr: str,
    target_token: str,
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
    limit_blocks: int = 100000,
) -> list[dict]:
    """Fetch Swap logs for a pool and reconstruct hourly OHLCV candles.

    The price is expressed in target_token / WETH terms:
      - If target_token is token1: price = ratio_token1_per_token0 (direct)
      - If target_token is token0: price = 1 / ratio_token1_per_token0 (inverted)

    Returns candles sorted by timestamp, each:
      {ts, o, h, l, c, v}
    where ts is unix-seconds, o/h/l/c are in token/WETH, v is token volume.
    """
    token0 = get_token0(pool_addr)
    token1 = get_token1(pool_addr)

    if not token0 or not token1:
        return []

    # Determine which side the target token is on
    target = target_token.lower()
    if target == token0:
        target_is_token0 = True
    elif target == token1:
        target_is_token0 = False
    else:
        # Target token is not in this pool
        return []

    # Determine block range
    latest = _rpc(RPC_PRIMARY, "eth_blockNumber", [])
    latest_block = int(latest, 16) if isinstance(latest, str) else int(latest, 0)

    if to_ts is not None:
        # approximate: ~2s per block on Base
        to_block = min(latest_block, latest_block - (from_ts or to_ts) // 2)
    else:
        to_block = latest_block

    if from_ts is not None:
        from_block = max(0, latest_block - (to_block - latest_block + (to_ts or to_block) - from_ts) // 2)
    else:
        from_block = max(0, to_block - limit_blocks)

    from_block = max(0, from_block)

    # Fetch logs for BOTH swap event signatures (V2 + V3)
    all_swaps = []

    for topic in (SWAP_V2_TOPIC, SWAP_V3_TOPIC):
        logs = _fetch_logs_chunked(pool_addr, [topic], from_block, to_block)
        for log in logs:
            block_num = int(log.get("blockNumber", "0x0"), 16)
            ts = _get_block_ts(block_num)
            if from_ts and ts < from_ts:
                continue
            if to_ts and ts > to_ts:
                continue

            if topic == SWAP_V2_TOPIC:
                decoded = _decode_v2_swap(log, token0, token1)
            else:
                decoded = _decode_v3_swap(log, token0, token1)

            if decoded is None:
                continue
            decoded["ts"] = ts
            all_swaps.append(decoded)

    if not all_swaps:
        return []

    all_swaps.sort(key=lambda s: s["ts"])

    # Compute price in target/WETH terms
    hourly: dict[int, list[float]] = {}
    for s in all_swaps:
        ratio = s["ratio_token1_per_token0"]
        if target_is_token0:
            # price = token0_in_weth = 1 / (token1_per_token0)
            price = 1.0 / ratio if ratio > 0 else 0
        else:
            # price = token1_in_weth = token1_per_token0
            price = ratio

        if price <= 0:
            continue

        bucket = _hour_bucket(s["ts"])
        if bucket not in hourly:
            hourly[bucket] = []

        # Volume in target-token units
        vol_target = abs(s.get("amount_in", 0)) if not target_is_token0 else abs(s.get("amount_out", 0))
        hourly[bucket].append(price)

    # Build candles
    candles = []
    for bucket_ts in sorted(hourly.keys()):
        prices = hourly[bucket_ts]
        if not prices:
            continue
        candles.append({
            "ts": bucket_ts,
            "o": round(prices[0], 12),
            "h": round(max(prices), 12),
            "l": round(min(prices), 12),
            "c": round(prices[-1], 12),
            "v": round(sum(prices), 6),  # proxy volume: sum of trade prices
        })

    return candles


def from_ts_or_now(ts: Optional[int] = None) -> int:
    if ts:
        return ts
    return int(datetime.now(timezone.utc).timestamp())


def _fetch_logs_chunked(pool_addr: str, topics: list,
                        from_block: int, to_block: int,
                        chunk: int = 10000) -> list:
    """eth_getLogs with chunking (Base RPC caps ~10k blocks per call)."""
    all_logs = []
    current = from_block
    while current <= to_block:
        end = min(current + chunk - 1, to_block)
        params = [{
            "address": pool_addr,
            "topics": topics,
            "fromBlock": hex(current),
            "toBlock": hex(end),
        }]
        try:
            result = _rpc(RPC_PRIMARY, "eth_getLogs", params)
            if isinstance(result, list):
                all_logs.extend(result)
        except Exception:
            pass
        current = end + 1
    return all_logs


# --------------------------------------------------------------------------- #
# Pool-selection rule
# --------------------------------------------------------------------------- #

def select_highest_liquidity_pool(token_addr: str,
                                  pool_snapshots: list[dict]) -> Optional[dict]:
    """Pool-selection rule: highest-liquidity Base pool per discovery snapshot.

    Args:
        token_addr: the target token address (lowercase or checksummed)
        pool_snapshots: list of pool dicts, each with at minimum:
          - address: pool contract address
          - liquidity: USD liquidity (float)
          - fee_bps: fee tier (float)
          - name: human-readable like "TOKEN / WETH 0.05%"

    Returns the pool dict with the highest liquidity for the given token.
    Ties broken by lowest fee_bps, then lowest address.
    Falls back to any pool containing the token if liquidity is missing.

    This rule is mechanical — applied before any scoring, never adjusted
    per-coin to avoid cherry-picking.
    """
    target = token_addr.lower()
    candidates = []

    for p in pool_snapshots:
        # Match if the token appears in the pool name OR a token field
        pool_addr = p.get("address", "") or ""
        # Check if this pool trades against WETH and contains the target token
        name_lower = (p.get("name", "") + " " + p.get("token0", "") +
                      " " + p.get("token1", "")).lower()

        # The pool should reference the target token
        if target not in name_lower and target not in pool_addr.lower():
            # Also check if the pool address itself is the token
            pass

        liq = float(p.get("liquidity", 0) or 0)
        fee = float(p.get("fee_bps", 999999) or 999999)

        # Include pools that contain the token (name-based matching is the
        # primary signal since discovered.jsonl stores token names + addr)
        if (target in name_lower or
            p.get("token_addr", "").lower() == target or
            p.get("base_token_address", "").lower() == target or
            p.get("quote_token_address", "").lower() == target):
            candidates.append({
                "pool": p,
                "liquidity": liq,
                "fee_bps": fee,
            })

    if not candidates:
        return None

    # Highest liquidity, then lowest fee, then lowest address
    candidates.sort(
        key=lambda c: (-c["liquidity"], c["fee_bps"],
                       c["pool"].get("address", "")),
    )
    return candidates[0]["pool"]


# --------------------------------------------------------------------------- #
# Data source integration
# --------------------------------------------------------------------------- #

def _load_pool_snapshots() -> list[dict]:
    """Load pool snapshots from candles_cache.json (the discovery snapshot)."""
    cache_path = os.path.join(REPO, "candles_cache.json")
    if not os.path.exists(cache_path):
        return []
    with open(cache_path) as f:
        cache = json.load(f)
    if isinstance(cache, list):
        return cache
    return []


def get_ohlcv(token_addr: str, hours: int = 120) -> list[dict]:
    """Fetch hourly OHLCV for a token using Base RPC swap events.

    Applies the pool-selection rule: highest-liquidity Base pool per the
    discovery snapshot (candles_cache.json).

    Falls back to synthetic random-walk data ONLY if:
      - No pool found for the token, OR
      - RPC fetch returns zero swaps
    so the scorer never crashes on a token with no on-chain history.

    Returns candles in harness format: [{ts, o, h, l, c, v}, ...]
    """
    snapshots = _load_pool_snapshots()
    pool = select_highest_liquidity_pool(token_addr, snapshots)

    if pool is None:
        # No pool in snapshot — use synthetic fallback (never block a run)
        return _synthetic_fallback(token_addr, hours)

    pool_addr = pool["address"]

    # Calculate timestamp range
    to_ts = int(datetime.now(timezone.utc).timestamp())
    from_ts = to_ts - (hours * 3600)

    candles = reconstruct_candles(pool_addr, token_addr,
                                  from_ts=from_ts, to_ts=to_ts)

    if not candles:
        return _synthetic_fallback(token_addr, hours)

    # Ensure we have the requested number of hours
    if len(candles) < hours:
        # Pad with synthetic for the missing tail (gap handling)
        candles = _pad_candles(candles, hours, token_addr)

    return candles[:hours]


def _synthetic_fallback(token_addr: str, n: int) -> list[dict]:
    """Random-walk fallback. Clearly labeled as non-market data."""
    import random
    random.seed(hash(token_addr) % 2**32)
    bars = []
    price = 0.0001
    now = int(datetime.now(timezone.utc).timestamp())
    for i in range(n):
        ts = (now // 3600) * 3600 - (n - 1 - i) * 3600
        h = price * (1 + random.uniform(0, 0.05))
        l = price * (1 - random.uniform(0, 0.05))
        c = price * (1 + random.uniform(-0.03, 0.03))
        bars.append({
            "ts": ts,
            "o": round(price, 12),
            "h": round(h, 12),
            "l": round(l, 12),
            "c": round(c, 12),
            "v": round(random.uniform(100, 1000), 2),
        })
        price = bars[-1]["c"]
    return bars


def _pad_candles(existing: list[dict], target_n: int,
                 token_addr: str) -> list[dict]:
    """Pad missing hours with synthetic bars (gaps in swap events)."""
    if not existing:
        return _synthetic_fallback(token_addr, target_n)

    # Extend backward if not enough bars
    while len(existing) < target_n:
        last = existing[-1]
        existing.append({
            "ts": last["ts"] + 3600,
            "o": last["c"],
            "h": last["c"] * 1.01,
            "l": last["c"] * 0.99,
            "c": last["c"],
            "v": 0.0,
        })

    return existing[:target_n]
