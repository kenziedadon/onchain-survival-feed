"""simple_ema_v1 — EMA crossover (9/21), SL 2.5%, TP 5.0%.

Registered as the reference strategy for WO4. Exits are resolved at the bar
after the stop/target is touched, never at the touch bar itself: intrabar
sequence is unknowable from OHLC, and assuming the favourable ordering is
the same cheat in miniature.
"""
from __future__ import annotations

from ..no_lookahead import Signal, VisibleWindow

STRATEGY_ID = "simple_ema_v1"
STRATEGY_VERSION = "1.0.0"

DEFAULTS = {"fast": 9, "slow": 21, "stop_loss_pct": 0.025,
            "take_profit_pct": 0.05, "rsi_max": 80.0}


def min_history_bars(params: dict) -> int:
    return int(params.get("slow", DEFAULTS["slow"])) + 2


def _ema(vals, n):
    """EMA seeded with the first value, standard alpha=2/(n+1)."""
    if not vals:
        return 0.0
    a = 2.0 / (n + 1.0)
    e = float(vals[0])
    for v in vals[1:]:
        e += a * (float(v) - e)
    return e


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


def generate_signals(window: VisibleWindow, params: dict) -> list:
    """Entry: fast EMA crosses above slow EMA. Exit: SL or TP, whichever
    comes first, resolved on the FOLLOWING bar."""
    p = dict(DEFAULTS)
    p.update(params or {})
    i = window.bar
    if i < min_history_bars(p):
        return []

    closes = window.closes
    fast_n, slow_n = int(p["fast"]), int(p["slow"])
    ef, es = _ema(closes, fast_n), _ema(closes, slow_n)
    # Previous bar's EMAs, recomputed WITHOUT the current bar, to detect a
    # genuine cross rather than merely being above.
    pf = _ema(closes[:-1], fast_n)
    ps = _ema(closes[:-1], slow_n)
    if not (pf <= ps and ef > es):
        return []
    if _rsi(closes) > float(p["rsi_max"]):
        return []
    return [Signal(entry_bar=i, exit_bar=window.bar + 1, direction="long",
                   tag="ema_cross")]


def exit_rule(entry_price: float, bars, params: dict):
    """Given bars AFTER entry, return the exit bar index (absolute) and reason.

    First bar that touches SL or TP wins; fills one bar later. Kept separate
    from generate_signals because it operates on data the harness owns, and
    mixing them is how a strategy ends up seeing its own fill.
    """
    p = dict(DEFAULTS)
    p.update(params or {})
    sl = float(p["stop_loss_pct"])
    tp = float(p["take_profit_pct"])
    for j in range(1, len(bars)):
        c = bars[j]["c"]
        if c <= entry_price * (1 - sl):
            return j, "stop_loss"
        if c >= entry_price * (1 + tp):
            return j, "take_profit"
    return len(bars) - 1, "window_end"
