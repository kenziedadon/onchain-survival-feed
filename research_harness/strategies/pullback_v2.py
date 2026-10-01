"""pullback_v2 — RSI 20-55 + ROC -10%..0%, SMA 8/32. LEGACY.

Retained specifically so the Aug 21 2026 result (5/9 coins passing, the only
positive number in the project's history) can be reproduced under the new
harness. If the new harness disagrees materially on the same input, that
discrepancy is a FINDING about the consolidation, not a nuisance to be
papered over.

Params and gates are transcribed from pullback_walkforward_fixed.py so this is
a faithful port, not a re-tuning.
"""
from __future__ import annotations

from ..no_lookahead import Signal, VisibleWindow

STRATEGY_ID = "pullback_v2"
STRATEGY_VERSION = "2.0.0-legacy"

DEFAULTS = {"fast": 8, "slow": 32, "rsi_lo": 20.0, "rsi_hi": 55.0,
            "roc_lo": -0.10, "roc_hi": 0.0, "roc_24_min": 0.0}


def min_history_bars(params: dict) -> int:
    return int(params.get("slow", DEFAULTS["slow"])) + 5


def _sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def _rsi(closes, n=14):
    if len(closes) <= n:
        return 50.0
    d = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    g = sum(max(x, 0.0) for x in d[:n]) / n
    l = sum(max(-x, 0.0) for x in d[:n]) / n
    for x in d[n:]:
        g = (g * (n - 1) + max(x, 0.0)) / n
        l = (l * (n - 1) + max(-x, 0.0)) / n
    if l == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + g / l)


def _roc(closes, n):
    if len(closes) <= n:
        return 0.0
    return closes[-1] / closes[-n] - 1.0


def generate_signals(window: VisibleWindow, params: dict) -> list:
    p = dict(DEFAULTS)
    p.update(params or {})
    i = window.bar
    if i < min_history_bars(p):
        return []
    c = window.closes
    px = c[-1]
    sf, ss = _sma(c, int(p["fast"])), _sma(c, int(p["slow"]))
    if sf is None or ss is None:
        return []
    r = _rsi(c)
    roc_f = _roc(c, int(p["fast"]))
    roc24 = _roc(c, 24)
    entry = (px > ss and px < sf
             and p["rsi_lo"] < r < p["rsi_hi"]
             and p["roc_lo"] < roc_f < p["roc_hi"]
             and roc24 > p["roc_24_min"])
    if not entry:
        return []
    return [Signal(entry_bar=i, exit_bar=i + 1, direction="long",
                   tag="pullback")]


def exit_rule(entry_price: float, bars, params: dict):
    """Legacy stop was score-dependent: 8% at score>=60, else 10%."""
    p = dict(DEFAULTS)
    p.update(params or {})
    stop = float(p.get("stop_pct", 0.08))
    for j in range(1, len(bars)):
        c = bars[j]["c"]
        if c <= entry_price * (1 - stop):
            return j, "stop_loss"
        if _rsi([b["c"] for b in bars[:j + 1]]) > 85:
            return j, "rsi_exit"
    return len(bars) - 1, "window_end"
