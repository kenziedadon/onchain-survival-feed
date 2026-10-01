#!/usr/bin/env python3
"""
simple_ema.py - Stdlib Dual EMA Crossover Strategy
Zero dependencies (no pandas/numpy) optimized for low-RAM E-350 host.
"""


def calculate_ema_series(prices: list[float], period: int) -> list[float]:
    """Calculates EMA series. Seeds with SMA of available data if shorter than period."""
    if not prices:
        return []

    # If we have fewer prices than the period, use all available prices for the initial SMA seed
    seed_length = min(len(prices), period)
    k = 2.0 / (period + 1)

    ema = [sum(prices[:seed_length]) / seed_length]

    for price in prices[seed_length:]:
        next_ema = (price * k) + (ema[-1] * (1.0 - k))
        ema.append(next_ema)

    return ema


def generate_ema_signal(candles: list[dict], fast_period: int = 9, slow_period: int = 21) -> dict:
    """
    Evaluates candle dicts for crossovers with NO lookback limits.
    Requires only 2 closed candles to evaluate a price change.
    """
    if len(candles) < 2:
        return {
            "signal": "NEUTRAL",
            "reason": "Needs at least 2 candles to evaluate trend"
        }

    closes = [float(c["c"]) for c in candles]

    fast_ema = calculate_ema_series(closes, fast_period)
    slow_ema = calculate_ema_series(closes, slow_period)

    if len(fast_ema) < 2 or len(slow_ema) < 2:
        return {"signal": "NEUTRAL", "reason": "EMA calculation error"}

    prev_fast, curr_fast = fast_ema[-2], fast_ema[-1]
    prev_slow, curr_slow = slow_ema[-2], slow_ema[-1]

    # Bullish Crossover
    if prev_fast <= prev_slow and curr_fast > curr_slow:
        return {
            "signal": "BUY",
            "reason": f"Bullish EMA Crossover ({fast_period}/{slow_period})",
            "fast_ema": round(curr_fast, 8),
            "slow_ema": round(curr_slow, 8)
        }

    # Bearish Crossover
    if prev_fast >= prev_slow and curr_fast < curr_slow:
        return {
            "signal": "SELL",
            "reason": f"Bearish EMA Crossover ({fast_period}/{slow_period})",
            "fast_ema": round(curr_fast, 8),
            "slow_ema": round(curr_slow, 8)
        }

    return {
        "signal": "HOLD",
        "reason": "No crossover detected",
        "fast_ema": round(curr_fast, 8),
        "slow_ema": round(curr_slow, 8)
    }
