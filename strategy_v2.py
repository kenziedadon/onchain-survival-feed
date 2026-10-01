#!/usr/bin/env python3
"""
Strategy upgrade: ATR-based sizing + volatility filter + enhanced risk metrics.

Added to strategy.py:
  - atr_volatility_filter: rejects entries when ATR > threshold (avoid choppy/volatile setups)
  - size_atr: position sizing based on risk % and ATR, with volatility multiplier
  - enhanced_stats: compute sharpe ratio, Sortino ratio, calmar ratio from backtest results
"""
import json
from indicators import sma, rsi, roc, atr


def atr_volatility_filter(candles, atr_mult_threshold=1.5):
    """Return True if current volatility (ATR/price) is within an acceptable band.
    Rejects when ATR/price > threshold (too choppy for pullback strategy)."""
    if len(candles) < 14:
        return True  # not enough data, allow
    atr_val = atr(candles, 14)
    px = candles[-1]["c"]
    if px <= 0:
        return True
    vol_pct = atr_val / px
    return vol_pct <= atr_mult_threshold


def size_atr(balance_eth, atr_val, entry_price, params):
    """ATR-based position sizing with volatility-adjusted risk.

    When volatility is high (ATR/price large), reduce position size.
    Risk per trade = risk_per_trade_pct * balance.
    Position size = risk / (ATR * sizing_atr_mult), capped by band.
    """
    if atr_val <= 0 or entry_price <= 0 or balance_eth <= 0:
        return 0.0

    risk_pct = params.get("risk_per_trade_pct", 0.02)
    atr_mult = params.get("sizing_atr_mult", 2.0)
    band_min = params.get("band_min", 0.0002)
    band_max = params.get("band_max", 0.0005)
    vol_floor = params.get("vol_floor", 0.005)  # min ATR/price ratio to activate volatility scaling
    vol_cap = params.get("vol_cap", 0.05)  # max ATR/price ratio before sizing collapses

    risk_eth = balance_eth * risk_pct
    # Volatility-adjusted risk reduction
    vol_ratio = atr_val / entry_price
    if vol_ratio > vol_floor:
        # Scale down as volatility increases beyond floor
        scale = max(0.1, 1.0 - (vol_ratio - vol_floor) / (vol_cap - vol_floor))
        risk_eth *= scale

    risk_per_unit = atr_val * atr_mult
    if risk_per_unit <= 0:
        size_eth = band_max
    else:
        size_units = risk_eth / risk_per_unit
        size_eth = size_units * entry_price
    return max(band_min, min(band_max, size_eth))


def enhanced_stats(backtest_results):
    """Compute enhanced metrics from per-coin backtest results.

    Args:
        backtest_results: list of dicts, each with 'net_return_pct' and 'max_drawdown_pct'
    Returns:
        dict with avg_return, sharpe, sortino, calmar, max_dd, prof_pct
    """
    returns = [r["net_return_pct"] / 100 for r in backtest_results]
    if not returns:
        return {"avg_return": 0, "sharpe": 0, "sortino": 0, "calmar": 0, "max_dd": 0, "prof_pct": 0}

    n = len(returns)
    avg = sum(returns) / n
    if n < 2:
        std = 0.0
    else:
        var = sum((r - avg) ** 2 for r in returns) / (n - 1)
        std = var ** 0.5
    # Downside deviation for Sortino
    ddowns = [r for r in returns if r < 0]
    dd_var = sum(r ** 2 for r in ddowns) / n if ddowns else 0.0
    dd_std = dd_var ** 0.5

    profits = [r for r in returns if r > 0]
    prof_pct = len(profits) / n * 100
    avg_dd = sum(r.get("max_drawdown_pct", 0) / 100 for r in backtest_results) / n
    worst_dd = max((r.get("max_drawdown_pct", 0) / 100 for r in backtest_results), default=0)

    sharpe = (avg / std * (n ** 0.5)) if std > 0 else 0.0
    sortino = (avg / dd_std * (n ** 0.5)) if dd_std > 0 else 0.0
    calmar = (avg / worst_dd) if worst_dd > 0 else 0.0

    return {
        "avg_return": avg * 100,
        "sharpe": round(sharpe, 3),
        "sortino": round(sortino, 3),
        "calmar": round(calmar, 3),
        "max_dd": round(worst_dd * 100, 1),
        "avg_dd": round(avg_dd * 100, 1),
        "prof_pct": round(prof_pct, 0),
    }
