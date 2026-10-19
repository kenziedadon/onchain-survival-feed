#!/usr/bin/env python3
"""
auto_trade.py — EMA crossover scanner using DexScreener + GeckoTerminal + Strategy Router.

Architecture:
- DexScreener: Pool metrics (liquidity, volume, price, txns) — 300 req/min
- GeckoTerminal: OHLCV candles (when available, 60-min cache)
- Strategy Router: Multi-strategy entry engine with regime detection
- Router Weights: Dynamic strategy weight adjustment

Flow:
1. DexScreener pool metrics → activity gate
2. If active: attempt GT OHLCV
3. If GT available: run full strategy_router with candles
4. If GT unavailable: run volume-only strategies from DexScreener data
5. Best signal passes through risk gates → execute

Cron: */30 * * * * cd /home/kenzodadon/base-trend-bot && /usr/bin/python3 auto_trade.py
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from _atomic_write import atomic_jsonl_append

HERE = os.path.dirname(os.path.abspath(__file__))

# ── Watchlist ────────────────────────────────────────────────────────────────

WATCHLIST = [
    ("0xB20000000000000000000074deAC544E1E9a1f33", "0x8c86e46defb25d481f6d85aeb4c4aa30fda06e5c330afb0803f5d42cda65069c", "STONKFLY"),
    ("0xb095274743941e953c746f9c228da9c18bb6ec29", "0x99cf3E8bfB02c300312c53Aac5D0B082e3D5975C", "LAPTOP"),
    ("0x9b5e262cf9bb04869ab40b19af91d2dc85761722", "0x85f1Aa3a70fEdd1C52705c15BAEd143e675CD626", "NOCK"),
    ("0xb2000000000000000000004884b426556b92883d", "0x8b27f626ab668197000BC722A1012022CAeD10E2", "MSTRc"),
    ("0x8b67f2e56139ca052a7ec49cbcd1aa9c83f2752a", "0xb87d3CB5E3fAfeCd35B7C63e7c74441d8151DfB4", "MUTATIO"),
    # Basecat: previously stored a POOL id as the token address (viem rejected it).
    # Real token: 0xB2000000000000000000004c27f6523082f41D01
    ("0xB2000000000000000000004c27f6523082f41D01", "0x215b2146c3164aba3f5b18e8619b737c7f7dd11e5ba693646dcd9a336c5a30ba", "Basecat"),
    ("0x532f27101965dd16442E59d40670FaF5eBB142E4", "0x4e829F8A5213c42535AB84AA40BD4aDCCE9cBa02", "BRETT"),  # token=pool bug fixed: real BRETT token + aerodrome pool
    ("0x9a26F5433671751C3276a065f57e5a02D2817973", "0x377FeeeD4820B3B28D1ab429509e7A0789824fCA", "KEYCAT"),  # token=pool bug fixed: real KEYCAT token + uniswap V3 pool
    ("0xB1a03EdA10342529bBF8EB700a06C60441fEf25d", "0x17A3Ad8c74c4947005aFEDa9965305ae2EB2518a", "MIGGLES"),  # token=pool bug fixed: real MIGGLES token + uniswap pool
]

# ── DexScreener (pool metrics, 5-min cache) ─────────────────────────────────

DS_BASE = "https://api.dexscreener.com"
DS_CACHE = {}
DS_CACHE_TTL = 300  # 5 min


def ds_fetch(pair_addr, force=False):
    if not force and pair_addr in DS_CACHE:
        ts, data = DS_CACHE[pair_addr]
        if time.time() - ts < DS_CACHE_TTL:
            return data
        del DS_CACHE[pair_addr]

    url = f"{DS_BASE}/latest/dex/pairs/base/{pair_addr}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        DS_CACHE[pair_addr] = (time.time(), data)
        return data
    except Exception:
        return None


def ds_get_metrics(pair_addr):
    """Get standardized pool metrics from DexScreener."""
    data = ds_fetch(pair_addr)
    if not data or not data.get("pairs"):
        return None

    pool = data["pairs"][0]
    base = pool.get("baseToken", {})
    quote = pool.get("quoteToken", {})
    vol = pool.get("volume", {})
    liq = pool.get("liquidity", {})
    txns = pool.get("txns", {})

    return {
        "pair_addr": pair_addr,
        "base_symbol": base.get("symbol", "?"),
        "quote_symbol": quote.get("symbol", "?"),
        "price_usd": float(pool.get("priceUsd", 0) or 0),
        "liquidity_usd": float(liq.get("usd", 0) or 0),
        "vol_1h_usd": float(vol.get("h1", 0) or 0),
        "vol_5m_usd": float(vol.get("m5", 0) or 0),
        "vol_24h_usd": float(vol.get("h24", 0) or 0),
        "txns_1h_buys": int(txns.get("h1", {}).get("buys", 0) or 0),
        "txns_1h_sells": int(txns.get("h1", {}).get("sells", 0) or 0),
        "txns_1h_total": int(txns.get("h1", {}).get("buys", 0) or 0) + int(txns.get("h1", {}).get("sells", 0) or 0),
        "dex_id": pool.get("dexId", ""),
    }


# ── GeckoTerminal (OHLCV, 60-min cache) ─────────────────────────────────────

GT_CACHE = {}
GT_BLOCKED_UNTIL = 0
GT_CACHE_TTL = 3600  # 60 min


def gt_fetch(url, force=False):
    global GT_BLOCKED_UNTIL

    if not force and url in GT_CACHE:
        ts, data = GT_CACHE[url]
        if time.time() - ts < GT_CACHE_TTL:
            return data
        del GT_CACHE[url]

    now = time.time()
    if now < GT_BLOCKED_UNTIL:
        return None

    req = urllib.request.Request(url, headers={
        "Accept": "application/json;version=20230302",
        "User-Agent": "Mozilla/5.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        GT_CACHE[url] = (time.time(), data)
        GT_BLOCKED_UNTIL = 0
        return data
    except urllib.error.HTTPError as e:
        if e.code == 429:
            GT_BLOCKED_UNTIL = time.time() + 60
            return None
        return None
    except Exception:
        return None


def gt_get_ohlcv(pool_addr):
    """Get OHLCV candles. Returns list or None."""
    url = f"https://api.geckoterminal.com/api/v2/networks/base/pools/{pool_addr}/ohlcv/hour?aggregate=1&limit=168"
    data = gt_fetch(url)
    if not data:
        return None
    return data.get("data", {}).get("attributes", {}).get("ohlcv_list", [])


def gt_cache_age(pool_addr):
    """Return cache age in seconds, or None if not cached."""
    url = f"https://api.geckoterminal.com/api/v2/networks/base/pools/{pool_addr}/ohlcv/hour?aggregate=1&limit=168"
    if url in GT_CACHE:
        return time.time() - GT_CACHE[url][0]
    return None


# ── Strategy Router Integration ──────────────────────────────────────────────

def build_candles_from_ohlcv(ohlcv_list):
    """Convert GT OHLCV list to candle dicts for strategy_router."""
    if not ohlcv_list:
        return []
    return sorted([
        {"ts": r[0], "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
         "c": float(r[4]), "v": float(r[5])}
        for r in ohlcv_list
    ], key=lambda x: x["ts"])


def run_strategy_router(candles, ds_metrics=None):
    """Run the strategy router with optional DexScreener context.

    Returns (best_signal, regime, sources_used) or (None, regime, sources_used).
    """
    sys.path.insert(0, HERE)

    try:
        from strategy_router import get_best_signal as router_get_best, detect_regime
        from router_weights import load_shadow_weights, apply_weights_to_signal
    except ImportError:
        # Router modules not available — fall back to simple EMA
        return _fallback_simple_ema(candles, ds_metrics), "simple_ema", ["ema_manual"]

    # Detect regime from candles
    regime, regime_conf, regime_details = detect_regime(candles)

    # Get best signal from router
    signal, _ = router_get_best(candles, regime)

    # Apply shadow weights if available
    weights = load_shadow_weights()
    if signal and isinstance(signal, dict):
        signal = apply_weights_to_signal(signal, weights)

    sources = ["strategy_router"]

    # If we have DexScreener data, supplement with volume/txns context
    if ds_metrics:
        total_txns = ds_metrics["txns_1h_total"]
        if total_txns > 0:
            buy_ratio = ds_metrics["txns_1h_buys"] / total_txns
            if signal:
                signal["ds_buy_ratio"] = buy_ratio
            # Could attach to other structures too, but keep simple

    return signal, regime, sources


def _fallback_simple_ema(candles, ds_metrics=None):
    """Fallback when strategy_router isn't available — simple EMA check."""
    if not candles or len(candles) < 30:
        return None

    closes = [c["c"] for c in candles]

    # EMA 9/21
    ema9 = sum(closes[-9:]) / 9
    ema21 = sum(closes[-21:]) / 21

    # Simple crossover check
    if len(closes) >= 22:
        prev_ema9 = sum(closes[-10:-1]) / 9
        prev_ema21 = sum(closes[-22:-2]) / 20
        crossover = prev_ema9 <= prev_ema21 and ema9 > ema21
    else:
        crossover = ema9 > ema21

    if crossover:
        diff = ((ema9 - ema21) / ema21 * 100) if ema21 > 0 else 0
        confidence = min(0.85, abs(diff) / 5 + 0.3)

        signal = {
            "signal": "BUY",
            "strategy": "ema_crossover_simple",
            "confidence": confidence,
            "fast_ema": ema9,
            "slow_ema": ema21,
            "reason": f"EMA 9/21 bullish (diff={diff:+.1f}%)",
        }

        # Supplement with DexScreener data
        if ds_metrics:
            signal["ds_buy_ratio"] = (
                ds_metrics["txns_1h_buys"] / max(ds_metrics["txns_1h_total"], 1)
            )

        return signal

    return None


# ── Volume-only strategies from DexScreener (no OHLCV needed) ────────────────

def ds_volume_spike_signal(ds_metrics):
    """Volume spike detection using DexScreener aggregate data.

    Uses 1h vs 24h volume ratio as proxy for volume spike.
    Works without OHLCV candles.
    """
    if not ds_metrics:
        return None

    vol_1h = ds_metrics["vol_1h_usd"]
    vol_24h = ds_metrics["vol_24h_usd"]
    vol_5m = ds_metrics["vol_5m_usd"]

    if vol_24h <= 0:
        return None

    # 1h/24h ratio: if 1h volume is >15% of 24h, that's a spike
    hour_ratio = vol_1h / vol_24h

    # 5m volume spike within the hour
    vol_5m_avg = vol_1h / 12  # rough average per 5m
    spike_5m = vol_5m / vol_5m_avg if vol_5m_avg > 0 else 0

    if hour_ratio > 0.15 or spike_5m > 3.0:
        confidence = min(0.7, hour_ratio * 2 + min(spike_5m / 10, 0.3))

        signal = {
            "signal": "BUY",
            "strategy": "volume_spike_ds",
            "confidence": confidence,
            "vol_1h_usd": vol_1h,
            "vol_24h_usd": vol_24h,
            "hour_ratio": round(hour_ratio, 3),
            "spike_5m": round(spike_5m, 2),
            "reason": f"Volume spike: 1h=${vol_1h:,.0f} ({hour_ratio:.0%} of 24h), 5m spike={spike_5m:.1f}x",
        }

        # Add buy/sell direction
        total = ds_metrics["txns_1h_total"]
        if total > 0:
            signal["ds_buy_ratio"] = ds_metrics["txns_1h_buys"] / total
            signal["ds_txns_1h"] = total

        return signal

    return None


def ds_momentum_signal(ds_metrics):
    """Momentum signal from DexScreener trade direction.

    Uses buy/sell ratio + volume as momentum indicator.
    Works without OHLCV candles.
    """
    if not ds_metrics:
        return None

    total = ds_metrics["txns_1h_total"]
    if total < 20:  # Need minimum activity
        return None

    buy_ratio = ds_metrics["txns_1h_buys"] / total
    vol_1h = ds_metrics["vol_1h_usd"]

    # Strong bullish momentum: >60% buys AND significant volume
    if buy_ratio > 0.60 and vol_1h > 10000:
        confidence = min(0.7, (buy_ratio - 0.5) * 2 + min(vol_1h / 100000, 0.2))

        return {
            "signal": "BUY",
            "strategy": "momentum_ds",
            "confidence": confidence,
            "ds_buy_ratio": buy_ratio,
            "ds_total_txns": total,
            "vol_1h_usd": vol_1h,
            "reason": f"Buy pressure: {buy_ratio:.0%} buys ({ds_metrics['txns_1h_buys']}/{total}), vol=${vol_1h:,.0f}",
        }

    return None


# ── Multi-source signal aggregation ──────────────────────────────────────────

def scan_token(token, pool, name):
    """Scan a single token using DexScreener + GT + Strategy Router.

    Returns signal dict or None.
    """
    print(f"  [{name}] ", end="")

    # ── Step 1: DexScreener pool metrics ──
    ds_metrics = ds_get_metrics(pool)
    if not ds_metrics:
        print("DS not found")
        return None

    liq = ds_metrics["liquidity_usd"]
    v1h = ds_metrics["vol_1h_usd"]
    v5m = ds_metrics["vol_5m_usd"]
    price = ds_metrics["price_usd"]
    dex = ds_metrics["dex_id"]

    # ── Step 2: Activity gate ──
    if liq < 5000 or v1h < 100:
        print(f"inactive (liq=${liq:,.0f} v1h=${v1h:,.0f})")
        return None

    print(f"active ({dex}, liq=${liq:,.0f} v1h=${v1h:,.0f})", end=" -> ")

    # ── Step 3: Collect signals from all available sources ──

    signals = []
    sources_used = []

    # Source A: Strategy router (needs GT OHLCV)
    ohlcv = gt_get_ohlcv(pool)
    if ohlcv:
        candles = build_candles_from_ohlcv(ohlcv)
        if len(candles) >= 30:
            signal, regime, sources = run_strategy_router(candles, ds_metrics)
            if signal:
                signals.append(signal)
                sources_used.extend(sources)
                cache_age = gt_cache_age(pool)
                cache_note = f" [GT cached {cache_age:.0f}s old]" if cache_age and cache_age > 60 else ""
                print(f"router:{regime} -> {signal['strategy']} (conf={signal['confidence']:.2f}){cache_note}")
        else:
            print(f"GT: insufficient candles ({len(candles)})")
    else:
        print("GT unavailable", end=" -> ")

    # Source B: Volume spike from DexScreener (no OHLCV needed)
    vol_signal = ds_volume_spike_signal(ds_metrics)
    if vol_signal:
        signals.append(vol_signal)
        sources_used.append("volume_spike_ds")
        print(f"vol_spike_ds (conf={vol_signal['confidence']:.2f})", end=" | ")

    # Source C: Momentum from DexScreener trade direction (no OHLCV needed)
    mom_signal = ds_momentum_signal(ds_metrics)
    if mom_signal:
        signals.append(mom_signal)
        sources_used.append("momentum_ds")
        print(f"momentum_ds (conf={mom_signal['confidence']:.2f})", end=" | ")

    if not signals:
        if ohlcv:
            # Had candles but no signal
            closes = [c["c"] for c in candles[-21:]]
            ema9 = sum(closes[-9:]) / 9 if len(closes) >= 9 else 0
            ema21 = sum(closes[-21:]) / 21 if len(closes) >= 21 else 0
            if ema9 and ema21:
                diff = ((ema9 - ema21) / ema21 * 100) if ema21 > 0 else 0
                print(f"no signal (EMA9/21 diff={diff:+.1f}%)")
            else:
                print("no signal")
        else:
            print("no signal (GT down, no DS volume spike)")
        return None

    # ── Step 4: Pick best signal ──
    best = max(signals, key=lambda s: s.get("confidence", 0))

    if best.get("signal") == "BUY":
        cache_note = " [CACHED]" if best.get("cached") else ""
        ds_note = ""
        if "ds_buy_ratio" in best:
            ds_note = f" | buys={best['ds_buy_ratio']:.0%}"
        print(f"🟢 {best['strategy']}: conf={best['confidence']:.2f}{cache_note}{ds_note}")
        best["name"] = name
        best["token"] = token
        best["pool"] = pool
        best["price"] = price
        best["liquidity"] = liq
        best["vol_1h"] = v1h
        best["dex_id"] = dex
        best["ds_metrics"] = ds_metrics
        return best

    print(f"best={best['strategy']} conf={best['confidence']:.2f} (not BUY)")
    return None


def scan():
    """Scan all watchlist tokens."""
    signals = []
    for token, pool, name in WATCHLIST:
        signal = scan_token(token, pool, name)
        if signal:
            signals.append(signal)
        time.sleep(0.15)
    return signals


# ── Execution ────────────────────────────────────────────────────────────────

GAS_BUFFER_ETH = 0.00025  # reserve for gas; current bal ~0.0008 ETH, leave enough to trade
MAX_POSITION_ETH = 0.003  # hard ceiling regardless of tier (wallet is ~$13)

# ── Dust guard ───────────────────────────────────────────────────────────────
# A "successful" swap can still be a total loss if the token amount is dust.
# Real example: 0.001 ETH -> 3.8e-9 LAPTOP (~$0.000000001) — burned $2.45 in
# gas to acquire nothing. Reject any expected output worth less than MIN_USD.
MIN_OUTPUT_USD = 0.10   # reject outputs worth less than this
GAS_COST_USD = 0.04     # ~cost of one Base swap
GAS_SAFETY_MULT = 2.0   # output must clear 2x round-trip gas


def _output_is_dust(expected_out_tokens, price_usd, amt_eth):
    """True if the expected token output is too small to be worth the gas.

    Returns (is_dust, reason). Guards against both tiny token amounts and
    absurd implied prices (which signal a bad/unreliable quote).

    PRIMARY CHECK: USD value of the ETH spent (amt_eth x ETH price). The token
    output of a fair swap is worth what you paid minus slippage/fees, so this
    is the reliable floor. expectedOut from swap.js is formatEther (18dp
    assumption) — wrong for tokens with other decimals (NOCK=16dp gave a 100x
    understatement), so token-count x price is only a SECONDARY sanity check.
    """
    try:
        amt = float(amt_eth or 0)
    except (TypeError, ValueError):
        amt = 0.0

    # Primary: value of what we're spending (source: ETH price oracle)
    if amt > 0:
        try:
            from executor import get_eth_price
            raw_px = get_eth_price()
            # get_eth_price() may return a float or a (price, source) tuple
            if isinstance(raw_px, (tuple, list)):
                raw_px = raw_px[0] if raw_px else 0
            eth_px = float(raw_px or 0)
        except Exception:
            eth_px = 0.0
        if eth_px > 0:
            usd_in = amt * eth_px
            floor = max(MIN_OUTPUT_USD, GAS_COST_USD * GAS_SAFETY_MULT)
            if usd_in < floor:
                return True, (
                    f"dust: {amt:.6f} ETH x ${eth_px:.2f} = ${usd_in:.8f} "
                    f"< ${floor:.2f} floor (input value)"
                )
            return False, f"ok (${usd_in:.4f} input value)"

    # Secondary fallback (no ETH price): token-count x token-price sanity check
    try:
        out = float(expected_out_tokens)
    except (TypeError, ValueError):
        return True, "unparseable expected output"

    if out <= 0:
        return True, "zero expected output"

    try:
        px = float(price_usd or 0)
    except (TypeError, ValueError):
        px = 0.0

    if px <= 0:
        # No reliable price — fall back to raw amount sanity check.
        # Sub-nanotoken outputs are dust regardless of price.
        if out < 1e-6:
            return True, f"dust amount {out:.3e} tokens (no price available)"
        return False, "no price; amount plausible"

    usd_value = out * px
    floor = max(MIN_OUTPUT_USD, GAS_COST_USD * GAS_SAFETY_MULT)
    if usd_value < floor:
        return True, (
            f"dust: {out:.6e} tokens x ${px:.6f} = ${usd_value:.8f} "
            f"< ${floor:.2f} floor"
        )
    return False, f"ok (${usd_value:.4f})"


def _size_trade(signal=None):
    """Capital-aware position size in ETH.

    tier_pct % of balance, scaled by signal confidence, clamped by the active
    size-ladder step and an absolute ceiling, always reserving a gas buffer.
    Falls back to the ladder step if balance/confidence are unreadable.
    """
    import capital_manager

    conf = 1.0
    if isinstance(signal, dict):
        try:
            conf = max(0.0, min(1.0, float(signal.get("confidence", 1.0))))
        except Exception:
            conf = 1.0

    # Ladder step size is the floor for step 1 and the ramp target later
    try:
        state = capital_manager.load_state()
        step_size = float(capital_manager.get_ladder_step(state).get("size_eth", 0.0005))
    except Exception:
        step_size = 0.0005

    # Live balance
    try:
        from risk import _live_balance
        bal = _live_balance()
    except Exception:
        bal = None
    if not bal or bal <= 0:
        return round(step_size, 6)

    # Tier % of balance
    try:
        _, risk_pct = capital_manager.get_tier(bal)
    except Exception:
        risk_pct = 0.5

    # Respect caps.json hard ceiling — evaluate() blocks anything above it,
    # so sizing that ignores this cap produces trades that can never fire.
    try:
        caps_ceiling = float(json.load(open(os.path.join(HERE, "caps.json"))).get("max_trade_eth") or 0)
    except Exception:
        caps_ceiling = 0.0
    ceiling = min(MAX_POSITION_ETH, caps_ceiling) if caps_ceiling > 0 else MAX_POSITION_ETH

    affordable = max(0.0, bal - GAS_BUFFER_ETH)
    sized = bal * risk_pct * conf

    # Clamp: not above ceiling, not above affordable, not below ladder step
    size = min(sized, ceiling, affordable)
    if size < step_size:
        # undersized vs ladder — only trade if we can afford the ladder step
        size = step_size if step_size <= affordable else 0.0

    return round(size, 6)


def execute_trade(signal):
    sys.path.insert(0, HERE)
    try:
        from executor import (
            build_swap, evaluate, execute_swap, quote_swap,
            load_caps, _load_holdings,
        )
    except ImportError:
        print(f"  ERROR: Cannot import executor")
        return False

    caps = load_caps()
    if caps.get("kill_switch") or not caps.get("live_trading"):
        print(f"  BLOCKED: kill_switch={caps.get('kill_switch')}, live_trading={caps.get('live_trading')}")
        return False

    holdings = _load_holdings()
    if signal["token"] in holdings:
        print(f"  SKIP: {signal['name']} already held")
        return False

    coin = {
        "token_addr": signal["token"],
        "address": signal["pool"],
        "name": signal["name"],
        "source": "auto_trade",
        "liquidity": signal.get("liquidity", 50000),
        "vol_24h": signal.get("vol_24h_usd", signal.get("vol_1h", 10000) * 24),
        "score": int(signal.get("confidence", 0.5) * 100),
    }

    amt = _size_trade(signal)
    if amt <= 0:
        print(f"    SKIP: sizing returned {amt}")
        return False
    print(f"    Sized: {amt:.6f} ETH")
    prop = build_swap(coin, amt, signal["token"], score=coin["score"])
    allowed, reason = evaluate(prop)

    if not allowed:
        print(f"    BLOCKED: {reason}")
        return False

    result = quote_swap(signal["token"], amt, 100)
    if not result or result.get("error"):
        print(f"    BLOCKED (no route): {(result or {}).get('error', 'quote failed')}")
        return False

    expected_out = result.get("expectedOut") or result.get("minOut")
    is_dust, why = _output_is_dust(expected_out, signal.get("price"), amt)
    if is_dust:
        print(f"    BLOCKED (dust guard): {why}")
        return False
    print(f"    Dust guard: {why}")

    result = execute_swap(signal["token"], amt, 100)
    if result.get("hash"):
        print(f"    TX: {result['hash'][:20]}")
        return True
    else:
        print(f"    FAIL: {result}")
        return False


def write_discovered(signals):
    path = os.path.join(HERE, "discovered.jsonl")
    if signals:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "coins": signals,
        }
        atomic_jsonl_append(path, entry)


def main():
    dry_run = "--dry-run" in sys.argv
    t0 = time.time()

    lock_path = os.path.join(HERE, ".auto_trade.lock")
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        print("Another instance running. Exiting.")
        return 0

    try:
        now = datetime.now(timezone.utc)
        print(f"=== {now.isoformat()} ===")
        print(f"Mode: {'DRY-RUN' if dry_run else 'LIVE'}")
        print(f"Sources: DexScreener (pool) + GeckoTerminal (OHLCV, {GT_CACHE_TTL//60}min cache) + Strategy Router")
        print(f"Watchlist: {len(WATCHLIST)} tokens\n")

        # ── Gate 1: Minimum Viable Balance ──────────────────────────
        # Prevents the bot from running on a thin wallet where every
        # scan cycle burns gas with no viable trades (cf. 2026-09-14
        # postmortem: bot burned ~$5.55 on gas at 0.0007 ETH balance).
        from risk import _live_balance
        balance_eth = _live_balance()
        MIN_BALANCE_ETH = 0.001  # ~$12 at $1200/ETH — enough for several trades + gas
        if balance_eth is None:
            print(f"ERROR: could not read wallet balance")
            return 1
        if balance_eth < MIN_BALANCE_ETH:
            print(f"CRITICAL: Balance too low — {balance_eth:.6f} ETH < {MIN_BALANCE_ETH} ETH minimum.")
            print(f"  Refill wallet or lower MIN_BALANCE_ETH. Aborting.")
            return 1

        signals = scan()

        if not signals:
            elapsed = time.time() - t0
            print(f"\nNo signals ({elapsed:.1f}s)")
            return 0

        print(f"\nFound {len(signals)} signal(s). ({time.time()-t0:.1f}s)")

        # Always write discovered.jsonl so the executor (and dashboard) see fresh data,
        # even in dry-run mode where we skip actual swaps.
        write_discovered(signals)

        if dry_run:
            from risk import evaluate as risk_evaluate
            from executor import build_swap
            from auto_trade import _size_trade
            for s in signals:
                cache_note = " [CACHED]" if s.get("cached") else ""
                print(f"  DRY-RUN: Would execute {s['name']} @ ${s['price']:.6f} via {s['strategy']}{cache_note}")
                coin = next((c for c in signals if c['name'] == s['name']), s)
                amt = _size_trade(s)
                prop = build_swap(coin, amt, s['token'], score=coin.get('score', 50))
                allowed, reason = risk_evaluate(prop)
                if not allowed:
                    print(f"    BLOCKED: {reason}")
                else:
                    print(f"    Would execute: {amt:.6f} ETH -> {s['name']}")
            return 0

        executed = 0
        # ── Gate 2: Pre-Flight Feasibility ──────────────────────────
        # Before attempting any trades, verify the math works. If the
        # wallet can't afford gas + a minimum trade value, stop instead
        # of burning gas on impossible trades (cf. 2026-09-14
        # postmortem: bot burned ~$5.55 on gas with no viable trades).
        from risk import can_trade, _eth_price
        from signals import get_gas_price_gwei
        gas_px = get_gas_price_gwei()
        eth_px = _eth_price()
        ok, why = can_trade(balance_eth, gas_px, eth_px)
        if not ok:
            print(f"CRITICAL: Pre-flight feasibility failed — {why}")
            print(f"  balance={balance_eth:.6f} ETH, gas={gas_px} gwei, ETH=${eth_px:.2f}")
            print(f"  Aborting. Refill wallet or reduce min_trade_usd.")
            return 1
        print(f"  Pre-flight: OK (gas={gas_px} gwei, ETH=${eth_px:.2f}, min_trade_usd=5.0)")

        for s in signals:
            print(f"\nExecuting {s['name']} ({s['strategy']})...")
            try:
                if execute_trade(s):
                    executed += 1
            except Exception as e:
                print(f"  Error: {e}")

        print(f"\nExecuted {executed}/{len(signals)}")
        return executed
    finally:
        try:
            os.remove(lock_path)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
