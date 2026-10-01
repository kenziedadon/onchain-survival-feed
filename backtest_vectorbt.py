#!/usr/bin/env python3
"""
Vectorbt backtester — Quant Science stack (vectorbt 0.27.3) wired to the
base-trend-bot candles_cache.json. Runs the v8/v9 momentum + ATR logic as
vectorbt signal portfolios so results are comparable with backtest.py.

Usage: .quant-venv/bin/python backtest_vectorbt.py [--refresh]
Output: vbt_results.json + prints per-coin and basket stats.
"""
import json, sys, subprocess, time
from pathlib import Path

import numpy as np
import pandas as pd
import vectorbt as vbt

HERE = Path(__file__).resolve().parent
CACHE = HERE / "candles_cache.json"
RESULTS = HERE / "vbt_results.json"

# ---- params (mirror strategy defaults; override via grid later) ----------
FAST, SLOW = 9, 21          # EMA cross
RSI_LEN, RSI_MAX = 14, 75   # don't buy overbought
ATR_LEN = 14
SL_ATR = 2.0                # stop = entry - 2*ATR (v9-style)
TP_ATR = 4.0                # take profit = entry + 4*ATR
FEE = 0.0030                # 30 bps round-trip proxy for micro-cap slippage+gas
INIT_CASH = 1000.0


def refresh_cache():
    """Reuse the existing candle fetcher to refresh candles_cache.json."""
    r = subprocess.run(
        [sys.executable.replace(".quant-venv", ""), str(HERE / "_cache_candles.py")],
        capture_output=True, text=True, timeout=240)
    if r.returncode != 0:
        print("cache refresh failed (using stale cache):", r.stderr[-300:])


def load_candles():
    coins = json.loads(CACHE.read_text())
    out = []
    for c in coins:
        df = pd.DataFrame(c.get("candles") or [])
        if len(df) < max(SLOW + RSI_LEN, ATR_LEN * 2):
            continue
        df["ts"] = pd.to_datetime(df["ts"], unit="s")
        df = df.set_index("ts").sort_index()
        close = df["c"].astype(float)
        high = df["h"].astype(float)
        low = df["l"].astype(float)
        out.append({"name": c.get("name", c.get("address", "?")),
                    "close": close, "high": high, "low": low})
    return out


def atr(h, l, c, n=ATR_LEN):
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def run_coin(coin):
    c, h, l = coin["close"], coin["high"], coin["low"]
    ema_f = c.ewm(span=FAST, adjust=False).mean()
    ema_s = c.ewm(span=SLOW, adjust=False).mean()
    rsi = vbt.RSI.run(c, window=RSI_LEN).rsi
    a = atr(h, l, c)

    entries = (ema_f > ema_s) & (ema_f.shift() <= ema_s.shift()) & (rsi < RSI_MAX)
    exits = (ema_f < ema_s) & (ema_f.shift() >= ema_s.shift())

    pf = vbt.Portfolio.from_signals(
        c, entries, exits,
        sl_stop=(a * SL_ATR / c),           # ATR-scaled stops (fraction of price)
        tp_stop=(a * TP_ATR / c),
        fees=FEE, init_cash=INIT_CASH,
        freq="1h")
    st = pf.stats()
    return {
        "name": coin["name"],
        "candles": int(len(c)),
        "trades": int(st["Total Trades"]) if "Total Trades" in st else int(pf.trades.count()),
        "return_pct": round(float(pf.total_return()) * 100, 2),
        "max_dd_pct": round(float(pf.max_drawdown()) * 100, 2),
        "win_rate_pct": round(float(st["Win Rate [%]"]) if "Win Rate [%]" in st else pf.trades.win_rate() * 100, 1),
        "sharpe": round(float(pf.sharpe_ratio()), 2) if not np.isnan(pf.sharpe_ratio()) else None,
    }


def main():
    if "--refresh" in sys.argv:
        print("refreshing candle cache…")
        refresh_cache()
    coins = load_candles()
    if not coins:
        print("no usable candle data"); sys.exit(1)
    results = []
    for coin in coins:
        try:
            results.append(run_coin(coin))
        except Exception as e:
            results.append({"name": coin["name"], "error": str(e)[:200]})
    ok = [r for r in results if "error" not in r]
    basket = 1.0
    for r in ok:
        basket *= 1 + r["return_pct"] / 200   # half-allocation per coin, like backtest.py
    summary = {
        "ts": time.time(),
        "engine": f"vectorbt {vbt.__version__}",
        "params": {"fast": FAST, "slow": SLOW, "rsi_len": RSI_LEN, "rsi_max": RSI_MAX,
                   "sl_atr": SL_ATR, "tp_atr": TP_ATR, "fee": FEE},
        "coins_tested": len(results),
        "profitable": sum(1 for r in ok if r["return_pct"] > 0),
        "avg_return_pct": round(np.mean([r["return_pct"] for r in ok]), 2) if ok else None,
        "basket_return_pct": round((basket - 1) * 100, 2) if ok else None,
        "results": results,
    }
    RESULTS.write_text(json.dumps(summary, indent=1))
    print(json.dumps({k: summary[k] for k in
                      ("engine", "coins_tested", "profitable", "avg_return_pct", "basket_return_pct")},
                     indent=1))
    for r in results:
        if "error" in r:
            print(f"  {r['name']:28} ERR {r['error'][:80]}")
        else:
            print(f"  {r['name']:28} ret={r['return_pct']:+7.2f}%  dd={r['max_dd_pct']:6.2f}%  "
                  f"wr={r['win_rate_pct']:5.1f}%  trades={r['trades']}")


if __name__ == "__main__":
    main()
