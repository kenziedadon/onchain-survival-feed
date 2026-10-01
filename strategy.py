#!/usr/bin/env python3
"""
UNIFIED STRATEGY ENGINE — single source of truth for entry/exit/sizing.
Used by backtest_pullback_v2.py, executor.py, and future strategies.

v1 upgrade over hardcoded executor exits:
  - ATR-based dynamic trailing stop (volatility-adjusted, not fixed %)
  - ATR-based position sizing (smaller size when volatility is high)
  - Unified pullback_v2 and momentum_v8 entry checks
  - Strategy params from caps.json strategy block + runtime overrides
"""
import json
from indicators import sma, rsi, roc, atr, macd, stochastic_rsi


# ---------- MOMENTUM CONFIRM FILTER (momentum momentum) ----------
def momentum_confirm_filter(closes, params):
    """Optional momentum overlay: reject entries when MACD is bearish or
    Stochastic RSI is overextended. Gated behind the evidence check — only
    blocks when data is present. Returns (pass, reasons).

    Designed as a secondary confirmation behind the EMA crossover / pullback
    entry, not a standalone signal. Keeps cinar-style breadth (MACD + StochRSI)
    without pulling in the external Go/AGPL dependency.
    """
    reasons = []
    if len(closes) < 40:
        return True, reasons  # insufficient data, defer to primary entry

    macd_line, signal_line, hist = macd(closes)
    srsi = stochastic_rsi(closes)

    # MACD: require bullish momentum (line above signal) when flag enabled
    if params.get("use_macd_filter", True):
        if macd_line is None or signal_line is None:
            reasons.append("macd_na")
        elif macd_line <= signal_line:
            return False, ["macd_bearish"]

    # Stochastic RSI: reject if overextended (> overbought threshold)
    if params.get("use_stochrsi_filter", True):
        srsi_hi = params.get("stochrsi_hi", 80)
        if srsi is None:
            reasons.append("stochrsi_na")
        elif srsi >= srsi_hi:
            return False, ["stochrsi_overbought"]

    return True, reasons


# ---------- PULLBACK v2 ----------
def check_entry_pullback(closes, candles, params):
    """Pullback entry: dip in an uptrend."""
    fast = params.get("fast", 8)
    slow = params.get("slow", 32)
    rsi_lo = params.get("rsi_lo", 20)
    rsi_hi = params.get("rsi_hi", 55)
    roc8_lo = params.get("roc8_lo", -0.10)
    roc8_hi = params.get("roc8_hi", 0.0)
    adx_min = params.get("adx_min", 20)

    if len(closes) < slow + 14:
        return False

    px = closes[-1]
    sf = sma(closes, fast)
    ss = sma(closes, slow)
    rs = rsi(closes, 14)
    r8 = roc(closes, 8)
    r24 = roc(closes, 24)

    if sf is None or ss is None:
        return False
    if not (px > ss):
        return False
    if not (px < sf):
        return False
    if not (rsi_lo < rs < rsi_hi):
        return False
    if not (roc8_lo < r8 < roc8_hi):
        return False
    if r24 <= 0:
        return False

    # ADX filter (uses full candle list)
    if len(candles) >= 14:
        a, pdi, mdi = _adx(candles, 14)
        if a < adx_min or pdi < mdi:
            return False
    return True


# ---------- MOMENTUM v8 ----------
def check_entry_momentum(closes, candles, params):
    """Momentum entry: trend confirmation with moving-average stack."""
    fast = params.get("fast", 8)
    slow = params.get("slow", 32)
    rsi_lo = params.get("rsi_lo", 40)
    rsi_hi = params.get("rsi_hi", 75)
    entry_roc = params.get("entry_roc", 0.03)
    adx_min = params.get("adx_min", 20)
    overbought = params.get("overbought", 85)

    if len(closes) < slow + 14:
        return False

    px = closes[-1]
    sf = sma(closes, fast)
    ss = sma(closes, slow)
    rs = rsi(closes, 14)
    r8 = roc(closes, 8)

    if sf is None or ss is None:
        return False
    if not (px > ss and sf > ss):
        return False
    if not (rsi_lo < rs < rsi_hi):
        return False
    if not (r8 > entry_roc):
        return False

    if len(candles) >= 14:
        a, pdi, mdi = _adx(candles, 14)
        if a < adx_min or pdi < mdi + 10:
            return False
    return True


# ---------- ATR-BASED EXIT ----------
def atr_stop(entry_price, current_price, atr_val, params):
    """Return True if ATR-based stop is triggered."""
    if atr_val <= 0:
        return False
    stop_mult = params.get("stop_atr_mult", 2.0)
    trail_mult = params.get("trail_atr_mult", 1.5)
    highest_since_entry = params.get("highest_since_entry", entry_price)

    # Update highest
    highest_since_entry = max(highest_since_entry, current_price)

    # Initial stop from entry
    initial_stop = entry_price - stop_mult * atr_val
    # Trailing stop from highest
    trailing_stop = highest_since_entry - trail_mult * atr_val
    # Use the tighter stop
    stop_level = max(initial_stop, trailing_stop)

    triggered = current_price <= stop_level
    return triggered, stop_level, highest_since_entry


# ---------- ATR-BASED SIZING ----------
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
    vol_floor = params.get("vol_floor", 0.005)
    vol_cap = params.get("vol_cap", 0.05)

    risk_eth = balance_eth * risk_pct
    vol_ratio = atr_val / entry_price
    if vol_ratio > vol_floor:
        scale = max(0.1, 1.0 - (vol_ratio - vol_floor) / (vol_cap - vol_floor))
        risk_eth *= scale

    risk_per_unit = atr_val * atr_mult
    if risk_per_unit <= 0:
        size_eth = band_max
    else:
        size_units = risk_eth / risk_per_unit
        size_eth = size_units * entry_price
    return max(band_min, min(band_max, size_eth))


# ---------- HELPERS ----------
def _adx(candles, n=14):
    """Compute ADX, +DI, -DI for the last bar."""
    m = len(candles)
    if m < n + 1:
        return 0.0, 0.0, 0.0
    plus_dm = []
    minus_dm = []
    trs = []
    for j in range(max(1, m - n), m):
        h = candles[j]["h"]
        l = candles[j]["l"]
        ph = candles[j - 1]["h"]
        pl = candles[j - 1]["l"]
        pc = candles[j - 1]["c"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
        up = h - ph
        down = pl - l
        plus_dm.append(max(up, 0) if up > down else 0)
        minus_dm.append(max(down, 0) if down > up else 0)
    pd = plus_dm[-n:]
    md = minus_dm[-n:]
    tr = trs[-n:]
    s_tr = sum(tr)
    if s_tr == 0:
        return 0.0, 0.0, 0.0
    pdi = sum(pd) / s_tr * 100
    mdi = sum(md) / s_tr * 100
    adx = abs(pdi - mdi) / (pdi + mdi) * 100 if (pdi + mdi) > 0 else 0.0
    return adx, pdi, mdi


def load_strategy_params():
    """Read strategy params from caps.json. Falls back to simple_ema_v1 defaults."""
    try:
        with open("caps.json") as f:
            caps = json.load(f)
        strat = caps.get("strategy", {})
        sparams = caps.get("strategy_params", {})
        params = {
            "name": strat.get("name", "simple_ema_v1"),
            "fast": sparams.get("fast_period", 9),
            "slow": sparams.get("slow_period", 21),
            "stop_loss_pct": sparams.get("stop_loss_pct", 0.025),
            "take_profit_pct": sparams.get("take_profit_pct", 0.05),
            "rsi_lo": sparams.get("rsi_lo", 40),
            "rsi_hi": sparams.get("rsi_hi", 75),
            "roc8_lo": sparams.get("roc8_lo", -0.10),
            "roc8_hi": sparams.get("roc8_hi", 0.0),
            "adx_min": sparams.get("adx_min", 20),
            "entry_roc": sparams.get("entry_roc", 0.03),
            "overbought": sparams.get("overbought", 85),
            "stop_atr_mult": sparams.get("stop_atr_mult", 2.0),
            "trail_atr_mult": sparams.get("trail_atr_mult", 1.5),
            "risk_per_trade_pct": sparams.get("risk_per_trade_pct", 0.02),
            "sizing_atr_mult": sparams.get("sizing_atr_mult", 2.0),
            "band_min": sparams.get("band_min", 0.0002),
            "band_max": sparams.get("band_max", 0.0005),
            "use_momentum_confirm": sparams.get("use_momentum_confirm", True),
            "use_macd_filter": sparams.get("use_macd_filter", True),
            "use_stochrsi_filter": sparams.get("use_stochrsi_filter", True),
            "stochrsi_hi": sparams.get("stochrsi_hi", 80),
            "use_dip_buy": sparams.get("use_dip_buy", False),
            "dip_buy_pct": sparams.get("dip_buy_pct", 0.025),
            "sell_high_pct": sparams.get("sell_high_pct", 0.05),
            "trailing_stop_pct": sparams.get("trailing_stop_pct", 0.02),
        }
        return params
    except Exception:
        return {
            "name": "simple_ema_v1", "fast": 9, "slow": 21,
            "stop_loss_pct": 0.025, "take_profit_pct": 0.05,
            "rsi_lo": 40, "rsi_hi": 75, "roc8_lo": -0.10, "roc8_hi": 0.0,
            "adx_min": 20, "entry_roc": 0.03, "overbought": 85,
            "stop_atr_mult": 2.0, "trail_atr_mult": 1.5,
            "risk_per_trade_pct": 0.02, "sizing_atr_mult": 2.0,
            "band_min": 0.0002, "band_max": 0.0005,
        }
