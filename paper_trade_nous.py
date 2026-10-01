#!/usr/bin/env python3
"""
paper_trade_nous.py - Paper trade simulation with Nous sentiment gate
Shows what would have happened historically with the full MoA pipeline.
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from simple_ema import generate_ema_signal
from indicators import atr

# Nous API
NOUS_TOKEN_FILE = os.path.join(os.path.expanduser("~"), ".hermes", "shared", "nous_auth.json")
NOUS_API_URL = "https://inference-api.nousresearch.com/v1/chat/completions"

_ema_cache = {}

def get_token():
    try:
        with open(NOUS_TOKEN_FILE) as f:
            auth = json.load(f)
        return auth.get("access_token") or auth.get("token")
    except Exception:
        return None

def evaluate_narrative(telemetry, model="upstage/solar-pro4:free"):
    """Call Nous LLM for sentiment scoring."""
    token = get_token()
    if not token:
        return {"score": 0, "decision": "NO_GO", "reason": "No token"}
    
    prompt = (
        "You are the Sentiment Proposer for a Base chain trading system. "
        "Analyze this token telemetry for a fast scalp. Focus on narrative strength, "
        "volume velocity, and liquidity. "
        "Output ONLY valid JSON with keys: 'score' (0-100), 'decision' ('BUY' or 'NO_GO'), and 'reason'. "
        f"Data: {json.dumps(telemetry)}"
    )
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "Mozilla/5.0"
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 150
    }
    
    try:
        req = urllib.request.Request(NOUS_API_URL, data=json.dumps(payload).encode(), headers=headers)
        with urllib.request.urlopen(req, timeout=20) as r:
            result = json.loads(r.read().decode())
        content = result['choices'][0]['message']['content']
        content = content.replace('```json', '').replace('```', '').strip()
        return json.loads(content)
    except Exception as e:
        return {"score": 0, "decision": "NO_GO", "reason": str(e)}


def fetch_ohlcv(addr, limit=300):
    """Fetch OHLCV from GeckoTerminal."""
    url = f"https://api.geckoterminal.com/api/v2/networks/base/pools/{addr}/ohlcv/hour?aggregate=1&limit={limit}"
    for _ in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json;version=20230302"})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode())
            rows = data.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
            return sorted([{"ts": r[0], "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]), "v": float(r[5])} for r in rows], key=lambda x: x["ts"])
        except Exception:
            time.sleep(1.5)
    return []


def backtest_ema_with_nous(candles, coin_data, use_nous=True):
    """Backtest EMA strategy with optional Nous gate."""
    closes = [c["c"] for c in candles]
    if len(closes) < 24:
        return None
    
    liq = coin_data.get("liquidity", 500000)
    fee_bps = coin_data.get("fee_bps", 30)
    score = coin_data.get("score", 50)
    eth_usd = 2500.0
    trade_eth = 0.0005
    actual_trade = min(trade_eth * (score / 50.0), 0.0005)
    slip = min(max((actual_trade * eth_usd) / liq * 10000 * 0.5, 1.0), 80.0)
    gas = 0.04 / (actual_trade * eth_usd) * 10000
    ec = (fee_bps + slip + gas) / 10000
    xc = ec
    
    pos = 0.0; ep = 0.0; cash = 1.0; peak = 1.0; mdd = 0.0
    wins = 0; loss = 0; gw = 0.0; gl = 0.0
    trades = []
    
    for i in range(21, len(closes)):
        px = closes[i]
        window = closes[:i+1]
        ema_result = generate_ema_signal([{"c": c} for c in window], fast_period=9, slow_period=21)
        
        if pos > 0:
            ret = px / ep - 1
            if ret >= 0.05 or ret <= -0.025:
                pnl = pos * (px / ep - 1) - xc
                cash = pos * (px / ep) * (1 - xc)
                pos = 0.0
                if pnl > 0: wins += 1; gw += pnl
                else: loss += 1; gl += -pnl
                peak = max(peak, cash); mdd = max(mdd, 1 - cash / peak)
                trades.append({"type": "sell", "px": px, "ret": ret, "pnl": pnl})
        elif ema_result.get("signal") == "BUY":
            # Nous gate
            if use_nous:
                telemetry = {
                    "symbol": coin_data.get("symbol", "?"),
                    "liquidity_usd": liq,
                    "price_change_24h": coin_data.get("change_24h", 0),
                    "ema_fast": ema_result.get("fast_ema"),
                    "ema_slow": ema_result.get("slow_ema"),
                }
                sentiment = evaluate_narrative(telemetry)
                if sentiment.get("decision") != "BUY" or sentiment.get("score", 0) < 80:
                    continue  # Filtered out by Nous gate
            
            pos = cash * (1 - ec)
            ep = px
            trades.append({"type": "buy", "px": px, "nous_score": sentiment.get("score") if use_nous else None})
    
    if pos > 0:
        pnl = pos * (closes[-1] / ep - 1) - xc
        cash = pos * (closes[-1] / ep) * (1 - xc)
        if pnl > 0: wins += 1; gw += pnl
        else: loss += 1; gl += -pnl
    
    n = wins + loss
    return {
        "net_return_pct": (cash - 1) * 100,
        "trades": n,
        "wins": wins,
        "losses": loss,
        "win_rate": (wins / n * 100) if n else 0,
        "profit_factor": (gw / gl) if gl else (9.99 if gw else 0),
        "max_drawdown_pct": mdd * 100,
        "trades_log": trades,
    }


def main():
    print("=" * 60)
    print("PAPER TRADE SIMULATION: EMA vs EMA+Nous Gate")
    print("=" * 60)
    
    # Load backtest coins
    with open("backtest_results.json") as f:
        coins = json.load(f)
    
    results_ema = []
    results_nous = []
    
    for coin in coins:
        name = coin.get("name", "?")
        addr = coin.get("address", "")
        if not addr:
            continue
        
        print(f"\n--- {name} ---")
        print(f"  Fetching OHLCV for {addr[:12]}...")
        candles = fetch_ohlcv(addr, limit=300)
        if len(candles) < 50:
            print(f"  Insufficient data ({len(candles)} candles)")
            continue
        
        # EMA only
        print(f"  Running EMA-only backtest...")
        bt_ema = backtest_ema_with_nous(candles, coin, use_nous=False)
        if bt_ema:
            results_ema.append({"name": name, **bt_ema})
            print(f"  EMA-only: {bt_ema['trades']} trades, {bt_ema['net_return_pct']:+.2f}%")
        
        # EMA + Nous (rate limited)
        print(f"  Running EMA+Nous backtest (rate-limited)...")
        time.sleep(2)  # Avoid 429
        bt_nous = backtest_ema_with_nous(candles, coin, use_nous=True)
        if bt_nous:
            results_nous.append({"name": name, **bt_nous})
            print(f"  EMA+Nous: {bt_nous['trades']} trades, {bt_nous['net_return_pct']:+.2f}%")
    
    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    
    if results_ema:
        ema_trades = sum(r["trades"] for r in results_ema)
        ema_avg = sum(r["net_return_pct"] for r in results_ema) / len(results_ema)
        ema_prof = sum(1 for r in results_ema if r["net_return_pct"] > 0)
        print(f"EMA-only:  {ema_trades} total trades, {ema_prof}/{len(results_ema)} coins profitable, avg {ema_avg:+.2f}%")
    
    if results_nous:
        nous_trades = sum(r["trades"] for r in results_nous)
        nous_avg = sum(r["net_return_pct"] for r in results_nous) / len(results_nous)
        nous_prof = sum(1 for r in results_nous if r["net_return_pct"] > 0)
        print(f"EMA+Nous:  {nous_trades} total trades, {nous_prof}/{len(results_nous)} coins profitable, avg {nous_avg:+.2f}%")
    
    if results_ema and results_nous:
        improvement = nous_avg - ema_avg
        reduction = ema_trades - nous_trades
        print(f"\nImprovement: {improvement:+.2f}% avg return, {reduction} fewer trades (noise reduction)")


if __name__ == "__main__":
    main()
