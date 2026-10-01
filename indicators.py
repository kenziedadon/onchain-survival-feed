#!/usr/bin/env python3
"""
Shared technical indicators + helpers for the Base trend bot.

Single source of truth for SMA/RSI/ROC so backtest.py, grid_search.py and
early_edge_test.py don't drift. Also exposes:
  * eth_usd_price()  — live WETH price from GeckoTerminal (no API key), with a
    hard-coded fallback so the cost model never crashes on a transient 429.
  * CANDLE_LIMIT      — smallest hourly-candle window that satisfies the widest
    lookback (slow=48), so we stop over-fetching 300 rows per coin.
"""
import json
import time
import urllib.request

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json;version=20230302"}

# Widest slow window in the grid is 48; +5 margin per the entry guard.
CANDLE_LIMIT = 120

# Fallback used only if the price fetch fails (kept intentionally conservative;
# the live fetch overrides it on every successful run).
_FALLBACK_ETH_USD = 2500.0
_CACHE = {"eth_usd": _FALLBACK_ETH_USD, "ts": 0.0}
_PRICE_TTL = 300.0  # cache the price 5 min so repeated backtests don't re-hit RPC


def eth_usd_price(fallback=_FALLBACK_ETH_USD) -> float:
    """Live WETH/USD from GeckoTerminal (keyless). Cached 5 min.

    Replaces the hardcoded 1900 constant that used to silently distort the
    slippage + gas-in-bps cost model. We read the WETH price off the first
    page of Base pools (any WETH/USDC pair exposes base_token_price_usd),
    which costs a single keyless call and is reused across backtests.
    """
    now = time.time()
    if now - _CACHE["ts"] < _PRICE_TTL:
        return _CACHE["eth_usd"]
    try:
        url = "https://api.geckoterminal.com/api/v2/networks/base/pools?page=1"
        data = json.loads(_get(url))
        for p in data.get("data", []):
            a = p.get("attributes", {})
            name = a.get("name", "")
            if "WETH" in name.upper() and "USDC" in name.upper():
                px = a.get("base_token_price_usd")
                if px and float(px) > 0:
                    close = float(px)
                    _CACHE.update(eth_usd=close, ts=now)
                    return close
    except Exception:
        pass
    return _CACHE["eth_usd"]


def _get(url, timeout=20, retries=3):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode()
        except Exception:
            if i < retries - 1:
                time.sleep(1.0)
                continue
            raise
    raise RuntimeError("unreachable")


def sma(vals, n):
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def rsi(closes, n=14):
    if len(closes) <= n:
        return 50.0
    gains = []
    losses = []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    g = sum(gains[:n]) / n
    l = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        g = (g * (n - 1) + gains[i]) / n
        l = (l * (n - 1) + losses[i]) / n
    if l == 0:
        return 100.0
    rs = g / l
    return 100 - (100 / (1 + rs))


def rsi_ewma(closes, n=14):
    """RSI using Wilder's smoothing (EMA-style) — more responsive than SMA-based.

    Use when you need lower-lag momentum for short timeframes (hourly scalp).
    The default rsi() above uses the older SMA initialization then Wilder update;
    this helper starts Wilder smoothing from the first bar for faster convergence.
    """
    if len(closes) < n:
        return 50.0
    k = 1.0 / n
    g = 0.0
    l = 0.0
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gi = max(d, 0)
        li = max(-d, 0)
        if i <= n:
            g = (g + (gi - g) / n)
            l = (l + (li - l) / n)
        else:
            g = k * gi + (1 - k) * g
            l = k * li + (1 - k) * l
    if l == 0:
        return 100.0
    rs = g / l
    return 100 - (100 / (1 + rs))


def roc(closes, n):
    if len(closes) <= n:
        return 0.0
    return closes[-1] / closes[-n] - 1

def atr(candles, n=14):
    if len(candles) <= n:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h = candles[i]["h"]
        l = candles[i]["l"]
        prev_c = candles[i-1]["c"]
        tr = max(h-l, abs(h-prev_c), abs(l-prev_c))
        trs.append(tr)
    return sum(trs[-n:]) / n


def ema(vals, n):
    """Exponential moving average of a close/value series. Returns last EMA or None."""
    if len(vals) < n:
        return None
    k = 2.0 / (n + 1)
    # seed with SMA of first n values
    prev = sum(vals[:n]) / n
    for v in vals[n:]:
        prev = v * k + prev * (1 - k)
    return prev


def macd(closes, fast=12, slow=26, signal=9):
    """Return (macd_line, signal_line, histogram) or None if insufficient data.

    Computed on close prices. macd_line = EMA(fast) - EMA(slow);
    signal_line = EMA(macd_line, signal); histogram = macd_line - signal_line.
    """
    if len(closes) < slow + signal:
        return None, None, None

    # Build full EMA series for fast and slow, then subtract per-point.
    def ema_series(vals, n):
        k = 2.0 / (n + 1)
        out = []
        prev = None
        for i, v in enumerate(vals):
            if i + 1 < n:
                out.append(None)
                continue
            if prev is None:
                prev = sum(vals[:n]) / n
            else:
                prev = v * k + prev * (1 - k)
            out.append(prev)
        return out

    ef = ema_series(closes, fast)
    es = ema_series(closes, slow)
    macd_series = []
    for a, b in zip(ef, es):
        if a is not None and b is not None:
            macd_series.append(a - b)
    if len(macd_series) < signal:
        return macd_series[-1] if macd_series else None, None, None
    macd_line = macd_series[-1]
    signal_line = ema(macd_series, signal)
    if signal_line is None:
        return macd_line, None, None
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def stochastic_rsi(closes, rsi_n=14, stoch_n=14):
    """Stochastic RSI of close prices. Returns last %K value (0-100) or None.

    Computes RSI series, then Stochastic over that RSI series:
    StochRSI = (RSI_t - min(RSI)) / (max(RSI) - min(RSI)) * 100.
    """
    if len(closes) < rsi_n + stoch_n:
        return None
    rsi_series = []
    for j in range(len(closes)):
        window = closes[:j+1]
        if len(window) >= rsi_n:
            rsi_series.append(rsi(window, rsi_n))
        else:
            rsi_series.append(None)
    rsi_series = [x for x in rsi_series if x is not None]
    if len(rsi_series) < stoch_n:
        return None
    window = rsi_series[-stoch_n:]
    lo = min(window)
    hi = max(window)
    if hi - lo == 0:
        return 50.0  # flat RSI -> neutral
    return (window[-1] - lo) / (hi - lo) * 100.0


def bollinger_pct_b(closes, period=20, num_std=2.0):
    """Bollinger %B: (price - lower) / (upper - lower). Returns 0-1 or None.

    Values below 0.5 mean price is under the middle band (on sale).
    Below 0.0 means price is at or below the lower band.
    Above 1.0 means price is at or above the upper band (extended).
    """
    if len(closes) < period:
        return None
    window = closes[-period:]
    sma = sum(window) / period
    variance = sum((x - sma) ** 2 for x in window) / period
    std = variance ** 0.5
    upper = sma + num_std * std
    lower = sma - num_std * std
    if upper == lower:
        return 0.5
    return (closes[-1] - lower) / (upper - lower)
