#!/usr/bin/env python3
"""debate_gate.py — structured bull/bear evaluation before execution.

Minara Harness "Institution Mode" integration: a configurable roundtable of
specialist perspectives that investigate an opportunity, challenge assumptions,
and review risk before any capital is committed.

This module provides a lightweight version:
  1. BULL perspective — argues FOR the trade (thesis, momentum, upside)
  2. BEAR perspective — argues AGAINST the trade (risk, timing, downside)
  3. RECONCILER — weighs both sides and produces a final verdict

The executor calls evaluate_trade() before broadcasting. If the debate gate
rejects, the trade is shadow-logged for learning.

Usage:
    python3 debate_gate.py --test          # run self-test
    python3 debate_gate.py --json          # evaluate a sample signal
"""
import json
import os
import sys
from datetime import datetime, timezone

BOT = os.path.dirname(os.path.abspath(__file__))
CAPS_FILE = os.path.join(BOT, "caps.json")
SHADOW_FILE = os.path.join(BOT, "shadow_book.json")


def load_caps():
    """Load caps.json."""
    try:
        with open(CAPS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def evaluate_trade(signal, candles, regime="unknown"):
    """
    Run a structured debate on a proposed trade.

    Args:
        signal: dict from strategy_router with keys like:
            strategy, confidence, reason, price, etc.
        candles: list of OHLCV candles for context
        regime: detected market regime string

    Returns:
        dict with:
            verdict: "PASS" or "REJECT"
            confidence: adjusted confidence after debate
            bull_score: 0-1 bull case strength
            bear_score: 0-1 bear case strength
            reasoning: list of debate points
            risk_flags: list of identified risks
    """
    if not signal or not candles:
        return _reject("No signal or candle data provided")

    # ─── BULL PERSPECTIVE ──────────────────────────────────────────────────
    bull_points = []
    bull_score = 0.0

    # 1. Signal confidence
    conf = signal.get("confidence", 0)
    if conf >= 0.7:
        bull_score += 0.3
        bull_points.append(f"High confidence signal ({conf:.2f})")
    elif conf >= 0.5:
        bull_score += 0.2
        bull_points.append(f"Moderate confidence ({conf:.2f})")
    elif conf >= 0.3:
        bull_score += 0.1
        bull_points.append(f"Low confidence ({conf:.2f})")

    # 2. Strategy-regime alignment
    strategy = signal.get("strategy", "")
    alignment = _check_alignment(strategy, regime)
    bull_score += alignment["score"]
    if alignment["point"]:
        bull_points.append(alignment["point"])

    # 3. Trend confirmation (EMA alignment)
    if len(candles) >= 21:
        closes = [c["c"] for c in candles]
        ema9 = _ema(closes, 9)
        ema21 = _ema(closes, 21)
        if ema9 and ema21:
            if ema9 > ema21:
                bull_score += 0.15
                bull_points.append("EMA 9 > 21 (uptrend confirmed)")
            elif ema9 < ema21 * 0.99:
                bull_score -= 0.1
                bull_points.append("EMA 9 < 21 (downtrend — bearish)")

    # 4. Volume confirmation
    if len(candles) >= 6:
        vol_current = candles[-1].get("v", 0)
        vol_avg = sum(c.get("v", 0) for c in candles[-20:]) / min(20, len(candles))
        if vol_avg > 0 and vol_current > vol_avg * 1.2:
            bull_score += 0.1
            bull_points.append(f"Volume above average ({vol_current/vol_avg:.1f}x)")

    # ─── BEAR PERSPECTIVE ──────────────────────────────────────────────────
    bear_points = []
    bear_score = 0.0

    # 1. Low confidence risk
    if conf < 0.4:
        bear_score += 0.2
        bear_points.append(f"Low signal confidence ({conf:.2f})")

    # 2. Regime mismatch
    if strategy == "ema_crossover" and regime == "ranging":
        bear_score += 0.25
        bear_points.append("EMA crossover in ranging market (whipsaw risk)")
    elif strategy == "rsi_meanrev" and regime == "trending":
        bear_score += 0.2
        bear_points.append("Mean-reversion in trending market (catching falling knife)")
    elif strategy == "momentum_breakout" and regime == "quiet":
        bear_score += 0.2
        bear_points.append("Breakout in quiet market (false breakout risk)")

    # 3. Overextended price
    if len(candles) >= 20:
        closes = [c["c"] for c in candles]
        sma20 = sum(closes[-20:]) / 20
        price = closes[-1]
        if sma20 > 0:
            extension = (price - sma20) / sma20
            if extension > 0.10:
                bear_score += 0.15
                bear_points.append(f"Price {extension*100:.1f}% above SMA20 (overextended)")
            elif extension < -0.05:
                bear_score += 0.05
                bear_points.append(f"Price {extension*100:.1f}% below SMA20 (weak)")

    # 4. Volatility risk
    if len(candles) >= 14:
        atr_val = _atr(candles, 14)
        price = candles[-1]["c"]
        if price > 0 and atr_val / price > 0.05:
            bear_score += 0.15
            bear_points.append(f"High volatility ({atr_val/price*100:.1f}% ATR)")

    # 5. Recent loss streak (from risk state)
    risk_flags = _check_risk_flags()
    if risk_flags:
        bear_score += 0.1 * len(risk_flags)
        bear_points.extend(risk_flags)

    # ─── RECONCILER ────────────────────────────────────────────────────────
    # Clamp scores
    bull_score = max(0.0, min(1.0, bull_score))
    bear_score = max(0.0, min(1.0, bear_score))

    # Net score
    net = bull_score - bear_score

    # Verdict
    if net > 0.1 and bull_score > 0.4:
        verdict = "PASS"
    elif net > 0.0:
        verdict = "PASS"  # marginal pass
    else:
        verdict = "REJECT"

    # Adjusted confidence
    adjusted_conf = conf * (0.5 + 0.5 * bull_score) * (1 - 0.3 * bear_score)
    adjusted_conf = max(0.1, min(0.95, adjusted_conf))

    return {
        "verdict": verdict,
        "confidence": round(adjusted_conf, 3),
        "bull_score": round(bull_score, 3),
        "bear_score": round(bear_score, 3),
        "net_score": round(net, 3),
        "reasoning": {
            "bull": bull_points,
            "bear": bear_points,
        },
        "risk_flags": risk_flags,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }


def _check_alignment(strategy, regime):
    """Check if strategy is well-suited for the regime."""
    alignment_map = {
        ("ema_crossover", "trending"): (0.25, "EMA crossover in trending market (ideal)"),
        ("rsi_meanrev", "ranging"): (0.25, "Mean-reversion in ranging market (ideal)"),
        ("momentum_breakout", "volatile"): (0.25, "Breakout in volatile market (ideal)"),
        ("volume_spike", "quiet"): (0.2, "Volume spike in quiet market (ideal)"),
        ("pullback", "trending"): (0.2, "Pullback in trending market (good)"),
        ("pullback", "ranging"): (0.1, "Pullback in ranging market (acceptable)"),
    }
    key = (strategy, regime)
    if key in alignment_map:
        return {"score": alignment_map[key][0], "point": alignment_map[key][1]}
    return {"score": 0.0, "point": None}


def _check_risk_flags():
    """Check risk state for warning flags."""
    flags = []
    state_file = os.path.join(BOT, "risk_state.json")
    try:
        with open(state_file) as f:
            state = json.load(f)

        # Daily loss approaching limit
        caps = load_caps()
        max_daily = caps.get("max_daily_loss_eth", 0.001)
        daily_pnl = state.get("pnl_eth", 0)
        if max_daily > 0 and daily_pnl < -max_daily * 0.5:
            flags.append(f"Daily loss at {daily_pnl:.4f} ETH (limit: -{max_daily})")

        # Consecutive losses
        trades = state.get("trades", [])
        recent = trades[-5:] if len(trades) >= 5 else trades
        losses = sum(1 for t in recent if t.get("pnl_eth", 0) < 0)
        if losses >= 3:
            flags.append(f"{losses}/5 recent trades are losses")

    except Exception:
        pass

    return flags


def _ema(data, period):
    """Calculate EMA."""
    if len(data) < period:
        return None
    k = 2 / (period + 1)
    ema_val = sum(data[:period]) / period
    for val in data[period:]:
        ema_val = val * k + ema_val * (1 - k)
    return ema_val


def _atr(candles, period):
    """Calculate ATR."""
    if len(candles) < period + 1:
        return 0
    trs = []
    for i in range(1, len(candles)):
        h = candles[i]["h"]
        l = candles[i]["l"]
        pc = candles[i - 1]["c"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    if len(trs) < period:
        return sum(trs) / len(trs) if trs else 0
    return sum(trs[-period:]) / period


def _reject(reason):
    """Return a rejection verdict."""
    return {
        "verdict": "REJECT",
        "confidence": 0.0,
        "bull_score": 0.0,
        "bear_score": 0.0,
        "net_score": 0.0,
        "reasoning": {"bull": [], "bear": [reason]},
        "risk_flags": [],
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    # Self-test with sample data
    print("DEBATE GATE SELF-TEST")
    print("=" * 50)

    # Sample signal
    test_signal = {
        "signal": "BUY",
        "strategy": "ema_crossover",
        "confidence": 0.7,
        "reason": "EMA 9/21 bullish crossover",
    }

    # Sample candles (uptrending)
    test_candles = []
    price = 100.0
    for i in range(30):
        price *= 1.002  # slight uptrend
        test_candles.append({
            "ts": i,
            "o": price * 0.999,
            "h": price * 1.005,
            "l": price * 0.995,
            "c": price,
            "v": 1000 + i * 100,
        })

    result = evaluate_trade(test_signal, test_candles, "trending")
    print(f"Signal: {test_signal['strategy']} @ {test_signal['confidence']}")
    print(f"Regime: trending")
    print(f"Verdict: {result['verdict']}")
    print(f"Adjusted confidence: {result['confidence']}")
    print(f"Bull score: {result['bull_score']}")
    print(f"Bear score: {result['bear_score']}")
    print(f"Net score: {result['net_score']}")
    print(f"\nBull points:")
    for p in result["reasoning"]["bull"]:
        print(f"  + {p}")
    print(f"\nBear points:")
    for p in result["reasoning"]["bear"]:
        print(f"  - {p}")
    if result["risk_flags"]:
        print(f"\nRisk flags:")
        for f in result["risk_flags"]:
            print(f"  ⚠ {f}")
    print()

    if "--json" in sys.argv:
        print(json.dumps(result, indent=2))
