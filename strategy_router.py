#!/usr/bin/env python3
"""
strategy_router.py — multi-regime entry engine with DexScreener context.

Regimes + entry strategies:
  TRENDING    → EMA crossover (existing simple_ema_v1)
  RANGING     → RSI mean-reversion (buy < 30, sell > 70 in range)
  VOLATILE    → Momentum breakout (price breaks level on volume)
  QUIET       → Volume spike (abnormal volume = algo/event-driven move)

Now accepts optional DexScreener context to bias regime detection and
confidence scoring using live volume/trade data from DexScreener API.

Each strategy produces a signal with a confidence score (0-1). The executor
uses the highest-confidence signal that passes the risk gates.
"""
import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CAPS_FILE = os.path.join(HERE, "caps.json")

# Indicators
sys.path.insert(0, HERE)
from indicators import sma, ema, rsi, atr, roc
from strategy import load_strategy_params

# ─── DexScreener Context Type ────────────────────────────────────────────────

DsContext = dict  # Expected keys documented below

# Expected DsContext keys (from dexscreener_scanner.extract_metrics):
#   vol_5m_usd: float          — 5-minute volume in USD
#   vol_1h_usd: float          — 1-hour volume in USD
#   vol_24h_usd: float         — 24-hour volume in USD
#   txns_1h_buys: int          — 1-hour buy transactions
#   txns_1h_sells: int         — 1-hour sell transactions
#   txns_1h_total: int         — total 1-hour transactions
#   liquidity_usd: float       — pool liquidity in USD
#   price_usd: float           — token price in USD
#
# Derived fields (computed by ds_build_context):
#   vol_5m_spike: float        — vol_5m / (vol_1h / 12), >1 means spike
#   vol_1h_ratio: float        — vol_1h / vol_24h, high = concentrated volume
#   buy_ratio: float           — txns_1h_buys / txns_1h_total


def ds_build_context(metrics: dict) -> "DsContext":
    """Build a DexScreener context dict from raw metrics.

    Returns empty dict if metrics is None or insufficient data.
    """
    if not metrics:
        return {}

    vol_1h = metrics.get("vol_1h_usd") or 0
    vol_5m = metrics.get("vol_5m_usd") or 0
    vol_24h = metrics.get("vol_24h_usd") or 0
    buys = metrics.get("txns_1h_buys") or 0
    sells = metrics.get("txns_1h_sells") or 0
    total = metrics.get("txns_1h_total") or 0

    ctx = {
        "vol_5m_usd": vol_5m,
        "vol_1h_usd": vol_1h,
        "vol_24h_usd": vol_24h,
        "txns_1h_buys": buys,
        "txns_1h_sells": sells,
        "txns_1h_total": total,
        "liquidity_usd": metrics.get("liquidity_usd") or 0,
        "price_usd": metrics.get("price_usd") or 0,
    }

    # Derived metrics
    if vol_1h > 0:
        ctx["vol_5m_spike"] = vol_5m / (vol_1h / 12.0)  # ~12 five-min bars per hour
    else:
        ctx["vol_5m_spike"] = 0.0

    if vol_24h > 0:
        ctx["vol_1h_ratio"] = vol_1h / vol_24h
    else:
        ctx["vol_1h_ratio"] = 0.0

    if total > 0:
        ctx["buy_ratio"] = buys / total
    else:
        ctx["buy_ratio"] = 0.5  # neutral default

    return ctx


# ─── REGIME DETECTION (with optional DexScreener context) ──────────────────

def detect_regime(candles, ds_context: DsContext = None):
    """
    Classify the current market regime from OHLCV candles, optionally
    biased by DexScreener live context.

    DexScreener context can override or augment regime detection when
    live volume/trade data shows something the candles haven't captured
    yet (e.g., a 5m volume spike before the hourly candle closes).

    Args:
        candles: list of {"ts","o","h","l","c","v"} dicts
        ds_context: optional DexScreener context dict from ds_build_context()

    Returns:
        (regime_str, confidence_float, details_dict)
        regime: "trending", "ranging", "volatile", "quiet", or "unknown"
    """
    if len(candles) < 30:
        # Not enough candles — fall back to DexScreener context if available
        if ds_context and ds_context.get("vol_5m_spike", 0) > 3.0:
            return "volatile", 0.5, {"reason": "insufficient candles, DS spike override"}
        return "unknown", 0.0, {"reason": "insufficient candles"}

    closes = [c["c"] for c in candles]
    volumes = [c.get("v", 0) for c in candles]

    # ── Candle-based metrics ──
    ema_fast = ema(closes, 9)
    ema_slow = ema(closes, 21)
    atr_val = atr(candles, 14)

    if ema_fast is None or ema_slow is None:
        return "unknown", 0.0, {"reason": "EMA calculation failed"}

    ema_diff_pct = (ema_fast - ema_slow) / ema_slow if ema_slow > 0 else 0
    price = closes[-1]
    vol_pct = atr_val / price if price > 0 else 0
    vol_recent = sum(volumes[-6:]) / max(sum(volumes[-30:]) / 5, 1) if len(volumes) >= 30 else 1.0
    trend_strength = abs(ema_diff_pct) * 100

    # ── DexScreener context modifiers ──
    ds_spike = ds_context.get("vol_5m_spike", 0) if ds_context else 0
    ds_buy_ratio = ds_context.get("buy_ratio", 0.5) if ds_context else 0.5
    ds_vol_1h_ratio = ds_context.get("vol_1h_ratio", 0) if ds_context else 0

    # Start with candle-based details
    details = {
        "ema_diff_pct": round(ema_diff_pct * 100, 3),
        "vol_pct": round(vol_pct * 100, 3),
        "trend_strength": round(trend_strength, 3),
        "vol_recent_vs_trailing": round(vol_recent, 2),
    }

    # Mix in DexScreener context where available
    if ds_context:
        details["ds_vol_5m_spike"] = round(ds_spike, 2)
        details["ds_buy_ratio"] = round(ds_buy_ratio, 3)
        details["ds_vol_1h_ratio"] = round(ds_vol_1h_ratio, 3)

    # ── Regime classification ──
    # Priority: DexScreener spike override > candle volatility > candle trend > candle range

    # Volatile overrides: strong 5m spike (>4x) is a strong volatile signal
    # even if candles haven't shown it yet
    if ds_spike > 4.0:
        confidence = min(0.85, ds_spike / 5.0)
        if ds_buy_ratio > 0.6:
            confidence = min(0.9, confidence + 0.05)
        return "volatile", confidence, details

    # Heavy 1h concentration (>15% of 24h in 1 hour)
    if ds_vol_1h_ratio > 0.15 and ds_spike > 2.0:
        confidence = min(0.75, ds_vol_1h_ratio * 3 + min(ds_spike / 10, 0.2))
        return "volatile", confidence, details

    # Strong buy pressure with moderate volume
    if ds_buy_ratio > 0.65 and ds_context.get("txns_1h_total", 0) > 100:
        # If candles show trending already, confirm; if ranging, tilt volatile
        if trend_strength > 1.0:
            details["ds_buy_confirmation"] = True
        elif vol_pct < 0.02:
            # Ranging market with strong buy pressure — potential early volatile
            if ds_spike > 1.5:
                return "volatile", min(0.65, ds_buy_ratio * 0.8), details

    # Default: candle-based classification
    # Modified slightly by DexScreener context where relevant

    # Volatile: >4% daily ATR
    if vol_pct > 0.04:
        return "volatile", min(0.9, vol_pct * 10), details

    # Trending: strong EMA slope
    if trend_strength > 1.5 and abs(ema_diff_pct) > 0.005:
        base_conf = min(0.95, trend_strength / 3)
        # Boost if DS confirms with buy pressure
        if ds_buy_ratio > 0.55 and ds_spike > 1.0:
            base_conf = min(0.95, base_conf + 0.05)
            details["ds_trend_confirmation"] = True
        return "trending", base_conf, details

    # Volatile: recent volume spike in candles
    if vol_recent > 2.0 and vol_pct > 0.015:
        base_conf = min(0.85, vol_recent / 3)
        # Boost if DS also shows spike
        if ds_spike > 2.0:
            base_conf = min(0.9, base_conf + 0.05)
        return "volatile", base_conf, details

    # Ranging: weak trend + low volatility
    if trend_strength < 0.5 and vol_pct < 0.02:
        base_conf = min(0.8, 0.5 - trend_strength)
        # If DS shows heavy buy pressure, might be early trend — note it
        if ds_buy_ratio > 0.6 and ds_spike > 2.0:
            details["ds_early_trend_warning"] = True
            base_conf = max(0.5, base_conf)  # slight boost but stay ranging
        return "ranging", base_conf, details

    # Quiet: very low vol + low recent volume
    if vol_pct < 0.01 and vol_recent < 0.5:
        base_conf = min(0.7, 0.5)
        # DS spike could indicate waking market
        if ds_spike > 2.5:
            return "volatile", min(0.7, ds_spike / 5), details
        return "quiet", base_conf, details

    # Default: ranging
    return "ranging", 0.4, details


# ─── STRATEGY 1: EMA CROSSOVER (TRENDING) ───────────────────────────────────

def signal_ema_crossover(candles, params=None, ds_context: DsContext = None):
    """
    Existing simple_ema_v1 strategy. Best in trending markets.
    Optionally boosted by DexScreener buy pressure confirmation.

    Returns signal dict or None.
    """
    if params is None:
        params = load_strategy_params()

    fast_period = int(params.get("fast_period", 9))
    slow_period = int(params.get("slow_period", 21))

    if len(candles) < slow_period + 5:
        return None

    closes = [c["c"] for c in candles]
    fast = ema(closes, fast_period)
    slow = ema(closes, slow_period)

    if fast is None or slow is None:
        return None

    prev_closes = closes[:-1]
    prev_fast = ema(prev_closes, fast_period)
    prev_slow = ema(prev_closes, slow_period)

    if prev_fast is None or prev_slow is None:
        return None

    # Bullish crossover OR already-above alignment
    crossover = (prev_fast <= prev_slow and fast > slow) or (fast > slow)

    if crossover:
        confidence = min(0.9, abs(fast - slow) / slow * 50 + 0.5)

        # DexScreener context: boost if buy pressure confirms the crossover
        if ds_context:
            ds_buy_ratio = ds_context.get("buy_ratio", 0.5)
            ds_spike = ds_context.get("vol_5m_spike", 0)

            if ds_buy_ratio > 0.6 and ds_spike > 1.5:
                # Live buy pressure supports the EMA signal
                boost = min(0.10, (ds_buy_ratio - 0.5) * 0.2 + min(ds_spike / 20, 0.05))
                confidence = min(0.95, confidence + boost)
            elif ds_buy_ratio < 0.4:
                # Weak buy pressure — slight suppression
                confidence = max(0.3, confidence - 0.05)

        return {
            "signal": "BUY",
            "strategy": "ema_crossover",
            "confidence": confidence,
            "fast_ema": fast,
            "slow_ema": slow,
            "reason": f"EMA {fast_period}/{slow_period} bullish crossover",
            "ds_buy_ratio": ds_context.get("buy_ratio", 0.5) if ds_context else None,
            "ds_vol_5m_spike": ds_context.get("vol_5m_spike", 0) if ds_context else None,
        }

    return None


# ─── STRATEGY 2: RSI MEAN-REVERSION (RANGING) ───────────────────────────────

def signal_rsi_meanrev(candles, params=None, ds_context: DsContext = None):
    """
    Buy when RSI < oversold in a ranging market (mean-reversion).
    Confirmation: price near lower Bollinger band + volume present.
    Boosted if DexScreener shows buy pressure at the dip.
    """
    if len(candles) < 21:
        return None

    closes = [c["c"] for c in candles]
    volumes = [c.get("v", 0) for c in candles]

    rsi_val = rsi(closes, 14)
    if rsi_val is None:
        return None

    bb_period = 20
    if len(closes) < bb_period:
        return None
    bb_sma = sum(closes[-bb_period:]) / bb_period
    bb_std = (sum((c - bb_sma) ** 2 for c in closes[-bb_period:]) / bb_period) ** 0.5
    bb_lower = bb_sma - 2 * bb_std

    price = closes[-1]
    near_lower_bb = price <= bb_lower * 1.02

    vol_avg = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else 0
    vol_current = volumes[-1] if volumes else 0
    vol_ok = vol_current > vol_avg * 0.5

    if rsi_val < 35 and near_lower_bb and vol_ok:
        confidence = min(0.8, (35 - rsi_val) / 35 * 0.6 + 0.3)

        # DexScreener boost: buy pressure at the dip is a strong confirmation
        if ds_context:
            ds_buy_ratio = ds_context.get("buy_ratio", 0.5)
            ds_spike = ds_context.get("vol_5m_spike", 0)

            if ds_buy_ratio > 0.65:
                boost = min(0.12, (ds_buy_ratio - 0.5) * 0.35)
                confidence = min(0.88, confidence + boost)

            if ds_spike > 2.0:
                boost = min(0.08, min(ds_spike / 20, 0.08))
                confidence = min(0.88, confidence + boost)

        return {
            "signal": "BUY",
            "strategy": "rsi_meanrev",
            "confidence": confidence,
            "rsi": round(rsi_val, 1),
            "bb_lower": round(bb_lower, 6),
            "price": round(price, 6),
            "reason": f"RSI oversold ({rsi_val:.1f}) near lower BB (${bb_lower:.4f})",
            "ds_buy_ratio": ds_context.get("buy_ratio", 0.5) if ds_context else None,
        }

    return None


# ─── STRATEGY 3: MOMENTUM BREAKOUT (VOLATILE) ───────────────────────────────

def signal_momentum_breakout(candles, params=None, ds_context: DsContext = None):
    """
    Breakout: price closes above recent high on above-average volume.
    Best in volatile markets. Boosted by DexScreener live volume spike.
    """
    if len(candles) < 15:
        return None

    closes = [c["c"] for c in candles]
    volumes = [c.get("v", 0) for c in candles]
    highs = [c["h"] for c in candles]

    price = closes[-1]

    recent_high = max(highs[-15:-1]) if len(highs) > 15 else max(highs[:-1]) if len(highs) > 1 else price
    breakout = price > recent_high * 0.995

    vol_avg = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else 0
    vol_current = volumes[-1] if volumes else 0
    vol_ok = vol_current > vol_avg * 1.5

    roc_val = roc(closes, 6)
    momentum_ok = roc_val > 0.005 if roc_val else False

    if breakout and vol_ok and momentum_ok:
        confidence = min(0.85, 0.4 + (vol_current / max(vol_avg, 1)) * 0.2 + roc_val * 5)

        # DexScreener boost: live 5m spike is strong confirmation for breakout
        if ds_context:
            ds_spike = ds_context.get("vol_5m_spike", 0)
            ds_buy_ratio = ds_context.get("buy_ratio", 0.5)

            if ds_spike > 3.0:
                boost = min(0.10, min(ds_spike / 20, 0.10))
                confidence = min(0.92, confidence + boost)

            if ds_buy_ratio > 0.6 and ds_spike > 1.5:
                boost = min(0.05, (ds_buy_ratio - 0.5) * 0.15)
                confidence = min(0.92, confidence + boost)

        return {
            "signal": "BUY",
            "strategy": "momentum_breakout",
            "confidence": confidence,
            "breakout_level": round(recent_high, 6),
            "price": round(price, 6),
            "vol_ratio": round(vol_current / max(vol_avg, 1), 2),
            "roc": round(roc_val * 100, 2),
            "reason": f"Breakout above ${recent_high:.4f} on {vol_current/max(vol_avg,1):.1f}x volume",
            "ds_vol_5m_spike": ds_context.get("vol_5m_spike", 0) if ds_context else None,
        }

    # Even without full breakout, a strong DS spike can be a partial signal
    if ds_context and ds_context.get("vol_5m_spike", 0) > 4.0:
        ds_buy_ratio = ds_context.get("buy_ratio", 0.5)
        if ds_buy_ratio > 0.55:
            return {
                "signal": "BUY",
                "strategy": "momentum_breakout_ds",
                "confidence": min(0.60, ds_context["vol_5m_spike"] / 8 + (ds_buy_ratio - 0.5) * 0.2),
                "reason": f"DS live spike {ds_context['vol_5m_spike']:.1f}x with {ds_buy_ratio:.0%} buys",
                "ds_vol_5m_spike": ds_context["vol_5m_spike"],
                "ds_buy_ratio": ds_buy_ratio,
            }

    return None


# ─── STRATEGY 4: VOLUME SPIKE (QUIET/ANY) ───────────────────────────────────

def signal_volume_spike(candles, params=None, ds_context: DsContext = None):
    """
    Detect abnormal volume that often precedes a price move.
    If DexScreener context is available, use that for live spike detection
    (faster than waiting for hourly candle close).
    """
    # If we have DexScreener context, use it for live spike detection
    if ds_context:
        ds_spike = ds_context.get("vol_5m_spike", 0)
        ds_buy_ratio = ds_context.get("buy_ratio", 0.5)
        vol_1h_ratio = ds_context.get("vol_1h_ratio", 0)

        if ds_spike > 3.0:
            price_up = ds_context.get("price_change_5m", 0) > 0  # if available
            if price_up is None:
                price_up = True  # default bullish on spike

            confidence = min(0.75, (ds_spike - 2) / 10 + 0.3 +
                             max(0, (ds_buy_ratio - 0.5) * 0.2))

            # Strong 1h concentration adds confidence
            if vol_1h_ratio > 0.15:
                confidence = min(0.82, confidence + 0.05)

            return {
                "signal": "BUY",
                "strategy": "volume_spike_ds",
                "confidence": confidence,
                "vol_5m_spike": round(ds_spike, 2),
                "ds_buy_ratio": ds_buy_ratio,
                "vol_1h_ratio": round(vol_1h_ratio, 3),
                "reason": f"DS live spike {ds_spike:.1f}x avg (buys {ds_buy_ratio:.0%})",
            }

    # Fall back to candle-based volume spike
    if len(candles) < 10:
        return None

    closes = [c["c"] for c in candles]
    volumes = [c.get("v", 0) for c in candles]

    if len(volumes) < 10:
        return None

    vol_current = volumes[-1]
    vol_avg = sum(volumes[-10:]) / 10

    if vol_avg <= 0:
        return None

    vol_ratio = vol_current / vol_avg

    if vol_ratio > 3.0:
        price_up = closes[-1] > closes[-2] if len(closes) >= 2 else False
        if price_up:
            confidence = min(0.75, 0.3 + vol_ratio * 0.1)
            return {
                "signal": "BUY",
                "strategy": "volume_spike",
                "confidence": confidence,
                "vol_ratio": round(vol_ratio, 2),
                "vol_avg": round(vol_avg, 2),
                "reason": f"Volume spike {vol_ratio:.1f}x average (bullish candle)",
            }

    return None


# ─── STRATEGY 5: PULLBACK (ANY TREND) ────────────────────────────────────────

def signal_pullback(candles, params=None, ds_context: DsContext = None):
    """
    Buy the pullback in an uptrend: price dips to EMA 21 but doesn't break it.
    Lower risk than breakout. Boosted if DS shows buy pressure on the dip.
    """
    if len(candles) < 25:
        return None

    closes = [c["c"] for c in candles]
    price = closes[-1]

    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)

    if ema9 is None or ema21 is None:
        return None

    uptrend = ema9 > ema21
    if not uptrend:
        return None

    near_ema21 = price <= ema21 * 1.01 and price >= ema21 * 0.97
    holds_ema21 = price > ema21 * 0.995

    if near_ema21 and holds_ema21:
        confidence = min(0.75, 0.5)

        # DexScreener boost: buy pressure on the pullback is a strong confirmation
        if ds_context:
            ds_buy_ratio = ds_context.get("buy_ratio", 0.5)
            ds_spike = ds_context.get("vol_5m_spike", 0)

            if ds_buy_ratio > 0.6:
                boost = min(0.10, (ds_buy_ratio - 0.5) * 0.3)
                confidence = min(0.85, confidence + boost)

            if ds_spike > 1.5 and ds_buy_ratio > 0.55:
                boost = min(0.05, min(ds_spike / 20, 0.05))
                confidence = min(0.85, confidence + boost)

        return {
            "signal": "BUY",
            "strategy": "pullback",
            "confidence": confidence,
            "ema9": round(ema9, 6),
            "ema21": round(ema21, 6),
            "price": round(price, 6),
            "reason": f"Pullback to EMA 21 in uptrend (price ${price:.4f} near EMA21 ${ema21:.4f})",
            "ds_buy_ratio": ds_context.get("buy_ratio", 0.5) if ds_context else None,
        }

    return None


# ─── ROUTER (with DexScreener context) ──────────────────────────────────────

def get_best_signal(candles, regime=None, ds_context: DsContext = None):
    """
    Run all strategies and return the best signal.

    Args:
        candles: list of {"ts","o","h","l","c","v"} dicts
        regime: optional pre-detected regime (auto-detected if None)
        ds_context: optional DexScreener context dict

    Returns:
        (signal_dict, regime_str) or (None, regime_str)
    """
    if regime is None:
        regime, _, _ = detect_regime(candles, ds_context)

    # Map regime to strategies to try first
    # Note: ds_context is passed to each strategy for live bias
    regime_priority = {
        "trending": [signal_ema_crossover, signal_pullback,
                      signal_momentum_breakout, signal_rsi_meanrev,
                      signal_volume_spike],
        "ranging": [signal_rsi_meanrev, signal_pullback,
                    signal_volume_spike, signal_ema_crossover,
                    signal_momentum_breakout],
        "volatile": [signal_momentum_breakout, signal_volume_spike,
                     signal_ema_crossover, signal_pullback,
                     signal_rsi_meanrev],
        "quiet": [signal_volume_spike, signal_rsi_meanrev,
                  signal_pullback, signal_momentum_breakout,
                  signal_ema_crossover],
        "unknown": [signal_ema_crossover, signal_pullback,
                    signal_rsi_meanrev, signal_momentum_breakout,
                    signal_volume_spike],
    }

    strategies = regime_priority.get(regime, regime_priority["unknown"])

    best_signal = None
    best_confidence = 0.0

    for strat_fn in strategies:
        try:
            # Pass ds_context to strategies that accept it
            sig = strat_fn(candles, ds_context=ds_context)
            if sig and sig.get("confidence", 0) > best_confidence:
                best_confidence = sig["confidence"]
                best_signal = sig
                best_signal["regime"] = regime
                best_signal["ds_context_used"] = ds_context is not None
        except Exception:
            continue

    return best_signal, regime


# ─── INTEGRATION HELPER ─────────────────────────────────────────────────────

def check_entry_timing_multi(token_out, candles, regime=None, ds_context: DsContext = None):
    """
    Drop-in replacement for _check_entry_timing() in executor.py.
    Returns (True, signal_dict) or (False, None).
    """
    if not candles or len(candles) < 30:
        # Try DS-only signal if candles insufficient
        if ds_context:
            from strategy_router import signal_volume_spike
            sig = signal_volume_spike(candles, ds_context=ds_context)
            if sig and sig.get("confidence", 0) >= 0.4:
                return True, sig
        return False, None

    signal, detected_regime = get_best_signal(candles, regime, ds_context)

    if signal and signal.get("confidence", 0) >= 0.3:
        return True, signal

    return False, None


# ─── SELF-TEST ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import urllib.request

    print("STRATEGY ROUTER WITH DEXSCREENER CONTEXT — SELF TEST")
    print("=" * 60)

    # Test 1: With a known pool
    pool = "0x8c86e46defb25d481f6d85aeb4c4aa30fda06e5c330afb0803f5d42cda65069c"
    url = f"https://api.geckoterminal.com/api/v2/networks/base/pools/{pool}/ohlcv/hour?aggregate=1&limit=120"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())

        ohlcv_list = data.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
        candles = sorted([
            {"ts": r[0], "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
             "c": float(r[4]), "v": float(r[5])}
            for r in ohlcv_list
        ], key=lambda x: x["ts"])

        print(f"\n[Test 1] Pool {pool[:20]}... — {len(candles)} candles")
        print("-" * 40)

        # Without DS context
        regime1, conf1, details1 = detect_regime(candles)
        signal1, _ = get_best_signal(candles, regime1)
        print(f"Without DS: regime={regime1}, signal={signal1['strategy'] if signal1 else 'none'}, conf={signal1['confidence']:.2f}" if signal1 else f"Without DS: regime={regime1}, no signal")

        # With DS context
        ds_metrics = {
            "vol_5m_usd": 150000,
            "vol_1h_usd": 800000,
            "vol_24h_usd": 15000000,
            "txns_1h_buys": 350,
            "txns_1h_sells": 150,
            "txns_1h_total": 500,
            "liquidity_usd": 130000,
            "price_usd": 0.000623,
        }
        ds_ctx = ds_build_context(ds_metrics)

        regime2, conf2, details2 = detect_regime(candles, ds_ctx)
        signal2, _ = get_best_signal(candles, regime2, ds_ctx)
        print(f"With DS:    regime={regime2}, signal={signal2['strategy'] if signal2 else 'none'}, conf={signal2['confidence']:.2f}" if signal2 else f"With DS: regime={regime2}, no signal")
        print(f"DS context: spike={ds_ctx['vol_5m_spike']:.1f}x, buys={ds_ctx['buy_ratio']:.0%}, 1h_ratio={ds_ctx['vol_1h_ratio']:.1%}")

        if signal1 and signal2:
            diff = signal2["confidence"] - signal1["confidence"]
            print(f"Confidence delta: {diff:+.3f} (DS {'boosted' if diff > 0 else 'did not affect'})")

        print("\n" + "=" * 60)

        # Test 2: Low volume scenario
        print("\n[Test 2] Low volume scenario")
        print("-" * 40)
        ds_metrics_low = {
            "vol_5m_usd": 5000,
            "vol_1h_usd": 80000,
            "vol_24h_usd": 15000000,
            "txns_1h_buys": 200,
            "txns_1h_sells": 200,
            "txns_1h_total": 400,
            "liquidity_usd": 130000,
            "price_usd": 0.000623,
        }
        ds_ctx_low = ds_build_context(ds_metrics_low)
        regime3, conf3, details3 = detect_regime(candles, ds_ctx_low)
        signal3, _ = get_best_signal(candles, regime3, ds_ctx_low)
        print(f"Regime: {regime3}, signal: {signal3['strategy'] if signal3 else 'none'}, conf: {signal3['confidence']:.2f}" if signal3 else f"Regime: {regime3}, no signal")
        print(f"DS context: spike={ds_ctx_low['vol_5m_spike']:.1f}x, buys={ds_ctx_low['buy_ratio']:.0%}")

        print("\n" + "=" * 60)

        # Test 3: Insufficient candles with DS override
        print("\n[Test 3] Insufficient candles + DS spike override")
        print("-" * 40)
        short_candles = candles[-10:]  # only 10 candles
        regime4, conf4, details4 = detect_regime(short_candles, ds_ctx)
        print(f"Regime: {regime4} (confidence {conf4:.2f})")
        print(f"Details: {details4}")

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
