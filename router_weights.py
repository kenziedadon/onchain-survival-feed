#!/usr/bin/env python3
"""router_weights.py — dynamic strategy weight adjustment.

Minara Harness "Strategy Studio" integration: adjusts strategy_router's
behavior based on shadow learner insights and market conditions.

This module:
  1. Reads shadow_weights.json (from shadow_learner.py)
  2. Adjusts strategy priority in get_best_signal()
  3. Provides regime-specific weight overrides
  4. Can be called by executor before routing

Usage:
    python3 router_weights.py --test          # run self-test
    python3 router_weights.py --json          # show current weights
"""
import json
import os
import sys
from datetime import datetime, timezone

BOT = os.path.dirname(os.path.abspath(__file__))
WEIGHTS_FILE = os.path.join(BOT, "shadow_weights.json")
CAPS_FILE = os.path.join(BOT, "caps.json")

# Base strategy priority (from strategy_router.py)
BASE_PRIORITY = {
    "trending": ["ema_crossover", "pullback", "momentum_breakout", "rsi_meanrev", "volume_spike"],
    "ranging": ["rsi_meanrev", "pullback", "volume_spike", "ema_crossover", "momentum_breakout"],
    "volatile": ["momentum_breakout", "volume_spike", "ema_crossover", "pullback", "rsi_meanrev"],
    "quiet": ["volume_spike", "rsi_meanrev", "pullback", "momentum_breakout", "ema_crossover"],
    "unknown": ["ema_crossover", "pullback", "rsi_meanrev", "momentum_breakout", "volume_spike"],
}

# Strategy name mapping (strategy_router function names → weight keys)
STRATEGY_KEY_MAP = {
    "ema_crossover": "ema_crossover",
    "rsi_meanrev": "rsi_meanrev",
    "momentum_breakout": "momentum_breakout",
    "volume_spike": "volume_spike",
    "pullback": "pullback",
}


def load_shadow_weights():
    """Load shadow learner weights."""
    if not os.path.exists(WEIGHTS_FILE):
        return _default_weights()
    try:
        with open(WEIGHTS_FILE) as f:
            data = json.load(f)
        return data.get("weights", _default_weights())
    except Exception:
        return _default_weights()


def _default_weights():
    """Default equal weights."""
    return {
        "ema_crossover": 1.0,
        "rsi_meanrev": 1.0,
        "momentum_breakout": 1.0,
        "volume_spike": 1.0,
        "pullback": 1.0,
        "confidence_threshold": 0.3,
    }


def get_adjusted_priority(regime, weights=None):
    """
    Get strategy priority adjusted by shadow weights.

    Args:
        regime: market regime string
        weights: optional pre-loaded weights dict

    Returns:
        list of strategy names in priority order
    """
    if weights is None:
        weights = load_shadow_weights()

    base = BASE_PRIORITY.get(regime, BASE_PRIORITY["unknown"])

    # Sort by weight (descending), keeping original order for ties
    weighted = []
    for strat in base:
        key = STRATEGY_KEY_MAP.get(strat, strat)
        w = weights.get(key, 1.0)
        weighted.append((strat, w))

    # Sort by weight descending, then by original position for stability
    weighted.sort(key=lambda x: (-x[1], base.index(x[0])))
    return [s for s, _ in weighted]


def get_confidence_threshold(weights=None):
    """Get the adjusted confidence threshold."""
    if weights is None:
        weights = load_shadow_weights()
    return weights.get("confidence_threshold", 0.3)


def apply_weights_to_signal(signal, weights=None):
    """
    Apply shadow weights to a signal's confidence score.

    If the signal's strategy has a weight != 1.0, adjust confidence.
    """
    if weights is None:
        weights = load_shadow_weights()

    if not signal:
        return signal

    strategy = signal.get("strategy", "")
    key = STRATEGY_KEY_MAP.get(strategy, strategy)
    weight = weights.get(key, 1.0)

    # Adjust confidence
    original_conf = signal.get("confidence", 0)
    adjusted = original_conf * weight
    signal["confidence"] = max(0.1, min(0.95, adjusted))
    signal["weight_applied"] = weight
    signal["original_confidence"] = original_conf

    return signal


def get_regime_overrides(regime):
    """
    Get regime-specific parameter overrides.

    These are conservative adjustments based on what we know works
    in each regime.
    """
    overrides = {
        "trending": {
            "ema_fast_period": 9,
            "ema_slow_period": 21,
            "atr_stop_mult": 2.0,
            "take_profit_mult": 3.0,
        },
        "ranging": {
            "ema_fast_period": 5,
            "ema_slow_period": 13,
            "atr_stop_mult": 1.5,
            "take_profit_mult": 2.0,
        },
        "volatile": {
            "ema_fast_period": 12,
            "ema_slow_period": 26,
            "atr_stop_mult": 2.5,
            "take_profit_mult": 4.0,
        },
        "quiet": {
            "ema_fast_period": 5,
            "ema_slow_period": 13,
            "atr_stop_mult": 1.5,
            "take_profit_mult": 2.5,
        },
    }
    return overrides.get(regime, overrides["trending"])


if __name__ == "__main__":
    print("ROUTER WEIGHTS SELF-TEST")
    print("=" * 50)

    weights = load_shadow_weights()
    print(f"Loaded weights: {json.dumps(weights, indent=2)}")
    print()

    for regime in ["trending", "ranging", "volatile", "quiet"]:
        priority = get_adjusted_priority(regime, weights)
        print(f"{regime}: {' → '.join(priority)}")

    print()
    print(f"Confidence threshold: {get_confidence_threshold(weights)}")
    print()

    # Test signal weighting
    test_signal = {
        "strategy": "ema_crossover",
        "confidence": 0.7,
        "reason": "Test signal",
    }
    adjusted = apply_weights_to_signal(test_signal.copy(), weights)
    print(f"Signal: {test_signal['strategy']} @ {test_signal['confidence']}")
    print(f"After weighting: {adjusted['confidence']} (weight: {adjusted['weight_applied']})")

    if "--json" in sys.argv:
        output = {
            "weights": weights,
            "threshold": get_confidence_threshold(weights),
            "priorities": {r: get_adjusted_priority(r, weights) for r in BASE_PRIORITY},
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        print(json.dumps(output, indent=2))
