#!/usr/bin/env python3
"""
RSI/SMA50/ATR/Kelly v1 Strategy
"""
import json, sys, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path
from indicators import sma, rsi, atr, eth_usd_price, CANDLE_LIMIT

# ========================= CONFIGURATION =========================
# Strategy core
RSI_PERIOD = 14
SMA_PERIOD = 50
ATR_PERIOD = 14

# Entry
RSI_ENTRY_MIN = 40
RSI_ENTRY_MAX = 75
PRICE_ABOVE_SMA50 = True  # Only long when above 50-day MA

# Exit
RSI_EXIT_OVERBOUGHT = 85
ATR_STOP_MULTIPLIER_BASE = 2.0

# Sizing
KELLY_FRACTION = 0.25  # 0.25-0.5 recommended
MAX_POSITION_SIZE_ETH = 0.05

# Risk gates
MIN_TRADES_FOR_STATS = 5
MIN_TRADES_FOR_GRID = 10
MAX_DRAWDOWN_THRESHOLD = 20.0  # %

# Volatility tiers (ATR/price ratio)
VOLATILITY_TIERS = {
    'low': (0, 0.15),      # ATR/price < 15% -> 2.0x multiplier
    'medium': (0.15, 0.30), # 15-30% -> 1.5x multiplier
    'high': (0.30, float('inf'))  # >30% -> 1.0x multiplier (LFI gets this)
}

VOLATILITY_MULTIPLIERS = {
    'low': 2.0,
    'medium': 1.5,
    'high': 1.0
}

# Backtest cost model
DEFAULT_FEE_BPS = 30.0
GAS_USD = 0.04
# =================================================================

BACKTESTS_DIR = Path(__file__).resolve().parent / "backtests"
BACKTESTS_DIR.mkdir(parents=True, exist_ok=True)

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json;version=20230302"}

def get(url, timeout=25, retries=5):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode()
        except Exception as e:
            if "429" in str(e) and i < retries - 1:
                time.sleep(2 + i * 2)
                continue
            if i < retries - 1:
                time.sleep(1.5)
                continue
            raise

def discover_trending(limit=30):
    url = "https://api.geckoterminal.com/api/v2/networks/base/trending_pools?page=1"
    data = json.loads(get(url)).get("data", [])
    out = []
    for p in data:
        a = p.get("attributes", {})
        addr = p["id"].split("_")[-1]
        name = a.get("name", "?")
        if not ("ETH" in name.upper() or "WETH" in name.upper()):
            continue
        if "USDC" in name.upper() or "USDT" in name.upper() or "DAI" in name.upper():
            continue
        liq = float(a.get("reserve_in_usd") or 0)
        v24h = float(a.get("volume_usd", {}).get("h24") or 0)
        price_change = a.get("price_change_percentage", {})
        h24_change = float(price_change.get("h24", 0))
        if h24_change <= 2:
            continue
        if v24h < 50000:
            continue
        fee = 30.0
        for tok in name.upper().replace("%", " %").split():
            if tok.replace(".", "").isdigit() or "%" in tok:
                try:
                    fee = float(tok.replace("%", "")) * 100
                except:
                    pass
        score = 0
        if liq >= 500000:
            score += min(liq/500000, 8) * 5
        if v24h > 0:
            score += min(v24h/50000, 10) * 5
        if fee <= 30:
            score += 5
        if h24_change > 5:
            score += 10
        elif h24_change > 2:
            score += 5
        out.append({"name": name, "address": addr, "liquidity": liq, "fee_bps": fee,
                    "vol_24h": v24h, "score": int(score), "change_24h": h24_change})
    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:limit]

def fetch_ohlcv(addr, tf="hour", limit=300):
    url = (f"https://api.geckoterminal.com/api/v2/networks/base/pools/"
           f"{addr}/ohlcv/{tf}?aggregate=1&limit={limit}")
    rows = json.loads(get(url)).get("data", {}).get("attributes", {}).get("ohlcv_list", [])
    c = sorted(({"ts": r[0], "o": float(r[1]), "h": float(r[2]),
                 "l": float(r[3]), "c": float(r[4]), "v": float(r[5])} for r in rows),
              key=lambda x: x["ts"])
    return c

def get_atr_multiplier(atr_ratio):
    for tier, (low, high) in VOLATILITY_TIERS.items():
        if low <= atr_ratio < high:
            if tier == 'low':
                return VOLATILITY_TIERS['low'][1]  # 2.0
            elif tier == 'medium':
                return 1.5
            else:
                return 1.0
    return 2.0

def kelly_size(win_rate, profit_factor, balance_eth, atr_ratio):
    if win_rate <= 0 or profit_factor <= 0:
        return 0.0
    # Kelly formula: f* = (p * b - q) / b where p=win_rate, q=1-p, b=profit_factor
    p = win_rate / 100.0
    b = profit_factor
    q = 1.0 - p
    kelly = (p * b - q) / b
    if kelly <= 0:
        return 0.0
    # Apply fraction
    kelly = kelly * KELLY_FRACTION
    # Volatility adjustment: reduce size for high vol
    mult = get_atr_multiplier(atr_ratio)
    kelly = kelly * (mult / 2.0)  # normalize by base 2.0x
    # Cap
    max_size = min(MAX_POSITION_SIZE_ETH, balance_eth * 0.1)
    return max(0.0, min(kelly, max_size))

def backtest(candles, fee_bps, liquidity, score=50, trade_eth=0.03, gas_usd=GAS_USD):
    closes = [c["c"] for c in candles]
    highs = [c["h"] for c in candles]
    lows = [c["l"] for c in candles]
    if len(closes) < SMA_PERIOD + ATR_PERIOD + 5:
        return None

    eth_usd = eth_usd_price()
    sma50_vals = [sma(closes[:i+1], SMA_PERIOD) for i in range(len(closes))]
    rsi_vals = [rsi(closes[:i+1], RSI_PERIOD) for i in range(len(closes))]
    atr_vals = [atr(candles[:i+1], ATR_PERIOD) for i in range(len(closes))]

    pos = 0.0
    entry_px = 0.0
    entry_hi = 0.0
    entry_atr = 0.0
    cash = 1.0
    peak = 1.0
    maxdd = 0.0
    wins = 0
    loss = 0
    gw = 0.0
    gl = 0.0
    trades = []

    for i in range(len(closes)):
        px = closes[i]
        if i < SMA_PERIOD + ATR_PERIOD:
            continue

        sma50 = sma50_vals[i]
        r = rsi_vals[i]
        a = atr_vals[i]
        if sma50 is None or r is None or a is None:
            continue

        atr_ratio = a / px if px > 0 else 0
        stop_mult = get_atr_multiplier(atr_ratio)
        stop_pct = (a * stop_mult) / px if px > 0 else 0.10

        slip_each = min(max((trade_eth * eth_usd) / liquidity * 10000 * 0.5, 1.0), 80.0)
        gas_each_bps = gas_usd / (trade_eth * eth_usd) * 10000 if trade_eth > 0 else 0
        enter_cost = (fee_bps + slip_each + gas_each_bps) / 10000
        exit_cost = (fee_bps + slip_each + gas_each_bps) / 10000

        if pos > 0:
            entry_hi = max(entry_hi, px)
            dd_from_peak = (px / entry_hi - 1) if entry_hi > 0 else 0
            ret = px / entry_px - 1
            if (dd_from_peak < -stop_pct) or (r > RSI_EXIT_OVERBOUGHT) or (ret < -stop_pct):
                before = cash
                cash = pos * (px / entry_px) * (1 - exit_cost)
                pnl = cash - before
                pos = 0.0
                entry_hi = 0.0
                entry_atr = 0.0
                if pnl > 0:
                    wins += 1
                    gw += pnl
                else:
                    loss += 1
                    gl += -pnl
                peak = max(peak, cash)
                maxdd = max(maxdd, 1 - cash / peak)
                trades.append({"pnl_pct": (px / entry_px - 1) * 100, "win": pnl > 0})

        elif PRICE_ABOVE_SMA50 and px > sma50 and (RSI_ENTRY_MIN < r < RSI_ENTRY_MAX):
            pos = cash * (1 - enter_cost)
            entry_px = px
            entry_hi = px
            entry_atr = a

    if pos > 0:
        before = cash
        cash = pos * (closes[-1] / entry_px) * (1 - exit_cost)
        pnl = cash - before
        if pnl > 0:
            wins += 1
            gw += pnl
        else:
            loss += 1
            gl += -pnl
        peak = max(peak, cash)
        maxdd = max(maxdd, 1 - cash / peak)
        trades.append({"pnl_pct": (closes[-1] / entry_px - 1) * 100, "win": pnl > 0})

    n = wins + loss
    net = cash - 1.0
    win_rate = (wins / n * 100) if n else 0
    pf = (gw / gl) if gl else (9.99 if gw else 0)
    return {
        "net_return_pct": net * 100,
        "trades": n, "wins": wins, "losses": loss,
        "win_rate": win_rate,
        "profit_factor": pf,
        "max_drawdown_pct": maxdd * 100,
        "cost_bps_roundtrip": (enter_cost + exit_cost) * 10000,
        "final_equity": cash,
        "trade_eth": trade_eth,
        "trades_detail": trades,
    }

def analyze_results(results):
    # Filter coins with MIN_TRADES_FOR_STATS
    valid = [r for r in results if r["trades"] >= MIN_TRADES_FOR_STATS]
    if not valid:
        return {"status": "no_valid_coins", "detail": f"All coins had < {MIN_TRADES_FOR_STATS} trades"}

    prof = [r for r in valid if r["net_return_pct"] > 0]
    avg = sum(r['net_return_pct'] for r in valid) / len(valid)
    basket_eq = 1.0
    for r in valid:
        basket_eq *= (0.5 + 0.5 * r["final_equity"])
    basket_return = (basket_eq - 1) * 100
    avg_dd = sum(r['max_drawdown_pct'] for r in valid) / len(valid)
    best = max(valid, key=lambda x: x['net_return_pct'])
    worst = min(valid, key=lambda x: x['net_return_pct'])

    # Grid check: need MIN_TRADES_FOR_GRID + 60% win rate + PF >= 1.5
    grid_pass = len(valid) >= MIN_TRADES_FOR_GRID and avg > 0 and len(prof)/len(valid) >= 0.60
    if grid_pass:
        pf_vals = [r["profit_factor"] for r in valid if r["trades"] >= MIN_TRADES_FOR_GRID]
        avg_pf = sum(pf_vals) / len(pf_vals) if pf_vals else 0
        grid_pass = grid_pass and avg_pf >= 1.5

    return {
        "valid_coins": len(valid),
        "total_coins": len(results),
        "profitable": len(prof),
        "pct_positive": (len(prof)/len(valid)*100) if valid else 0,
        "avg_return_pct": avg,
        "basket_return_pct": basket_return,
        "avg_drawdown_pct": avg_dd,
        "best": best["name"],
        "best_return": best["net_return_pct"],
        "worst": worst["name"],
        "worst_return": worst["net_return_pct"],
        "grid_pass": grid_pass,
        "status": "PROFITABLE" if basket_return > 5.0 and avg > 0 else "NOT_RELIABLY_PROFITABLE"
    }

def main():
    print("=== RSI/SMA50/ATR/Kelly v1 Strategy ===")
    print(f"Entry: SMA{SMA_PERIOD} + RSI{RSI_PERIOD} [{RSI_ENTRY_MIN}-{RSI_ENTRY_MAX}] + price>SMA50")
    print(f"Exit: RSI>{RSI_EXIT_OVERBOUGHT} + ATR trailing stop (tiered)")
    print(f"Sizing: {KELLY_FRACTION*100:.0f}% Kelly, capped at {MAX_POSITION_SIZE_ETH} ETH")
    print()

    pools = discover_trending(30)
    print(f"Found {len(pools)} ETH-paired trending pools")
    for i, p in enumerate(pools):
        print(f"  {i+1:2}. {p['name'][:30]:30} liq=${p['liquidity']:>11,.0f} fee={p['fee_bps']:.0f}bps score={p['score']}")
    print()

    print("=== BACKTEST ===")
    results = []
    skipped_low_score = 0
    for p in pools:
        if p["liquidity"] < 300_000:
            continue
        if p.get("score", 0) < 35:
            skipped_low_score += 1
            continue
        try:
            candles = fetch_ohlcv(p["address"], "hour", CANDLE_LIMIT)
            time.sleep(2.0)
        except Exception as e:
            print(f"  {p['name'][:28]:28} OHLCV ERROR: {e}")
            continue
        if len(candles) < 60:
            continue
        r = backtest(candles, p["fee_bps"], p["liquidity"], score=p.get("score", 50))
        if not r:
            continue
        r["name"] = p["name"]
        r["liq"] = p["liquidity"]
        r["score"] = p.get("score", 50)
        results.append(r)
        flag = "✅ PROFIT" if r["net_return_pct"] > 0 else "❌ LOSS"
        print(f"  {p['name'][:26]:26} {flag} net={r['net_return_pct']:+7.1f}% "
              f"trades={r['trades']:2} win={r['win_rate']:4.0f}% "
              f"PF={r['profit_factor']:.2f} DD={r['max_drawdown_pct']:.1f}% "
              f"cost={r['cost_bps_roundtrip']:.0f}bp")

    print(f"\n(skipped {skipped_low_score} coins below score<35)")

    if not results:
        print("NO BACKTESTABLE POOLS")
        sys.exit(1)

    summary = analyze_results(results)
    print(f"\n=== BASKET SUMMARY ({summary['valid_coins']}/{summary['total_coins']} coins with >= {MIN_TRADES_FOR_STATS} trades) ===")
    print(f"Profitable: {summary['profitable']}/{summary['valid_coins']} ({summary['pct_positive']:.0f}%)")
    print(f"Avg per-coin net return: {summary['avg_return_pct']:+.1f}%")
    print(f"Naive basket: {summary['basket_return_pct']:+.1f}%")
    print(f"Avg max drawdown: {summary['avg_drawdown_pct']:.1f}%")
    print(f"Best:  {summary['best']} {summary['best_return']:+.1f}%")
    print(f"Worst: {summary['worst']} {summary['worst_return']:+.1f}%")
    print(f"\n=== VERDICT ===")
    print(f"Grid pass (>= {MIN_TRADES_FOR_GRID} coins, >=60% win, PF>=1.5): {summary['grid_pass']}")
    print(f"Status: {summary['status']}")
    if summary['status'] == "PROFITABLE":
        print("Deploy with risk caps.")
    else:
        print("Tune params or hold.")

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    out = {
        "strategy": "rsi_sma50_atr_kelly_v1",
        "ts": ts,
        "config": {
            "rsi_period": RSI_PERIOD,
            "sma_period": SMA_PERIOD,
            "atr_period": ATR_PERIOD,
            "entry": f"price>SMA{SMA_PERIOD} + RSI{RSI_PERIOD} [{RSI_ENTRY_MIN}-{RSI_ENTRY_MAX}]",
            "exit": f"RSI>{RSI_EXIT_OVERBOUGHT} + ATR trailing stop (tiered)",
            "kelly_fraction": KELLY_FRACTION,
            "max_position_eth": MAX_POSITION_SIZE_ETH,
        },
        "summary": summary,
        "results": results,
    }
    out_path = BACKTESTS_DIR / f"kelly_v1_{ts}.json"
    out_path.write_text(json.dumps(out, indent=2))
    print(f"\nWrote results to {out_path}")

if __name__ == "__main__":
    main()
