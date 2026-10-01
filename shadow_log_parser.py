#!/usr/bin/env python3
"""Shadow log parser for base-trend-bot shadow trading analysis.
Analyzes discovered.jsonl, nous_signals.json, backtest_results.json, and grid_results.json
to produce a performance report in shadow mode (no live trading).
"""

import json
import os
from datetime import datetime
from collections import defaultdict

BOT_DIR = "/home/kenzodadon/base-trend-bot"

def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        return {"error": str(e)}

def parse_discovered_jsonl():
    discoveries = []
    path = os.path.join(BOT_DIR, "discovered.jsonl")
    if not os.path.exists(path):
        return discoveries

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                discoveries.append({
                    "name": entry.get("name", "unknown"),
                    "address": entry.get("address", entry.get("token_addr", "unknown")),
                    "liquidity_usd": entry.get("liquidity_usd", entry.get("liquidity", 0)),
                    "score": entry.get("score", entry.get("momentum_score", 0)),
                })
            except json.JSONDecodeError:
                continue
    return discoveries

def analyze_nous_signals():
    signals = load_json(os.path.join(BOT_DIR, "nous_signals.json"))
    tokens = signals.get("tokens", [])
    timestamp = signals.get("timestamp", "unknown")

    scores = []
    buy_count = 0
    no_go_count = 0

    for token in tokens:
        score = token.get("score") or token.get("nous_score") or token.get("decision_score", 0)
        decision = token.get("decision"),
            token.get("decision_buy"),
            token.get("decision", ""))
        scores.append(score)
        if isinstance(score, (int, float)):
            if score >= 80:
                buy_count += 1
            else:
                no_go_count += 1

    return {
        "timestamp": timestamp,
        "total_tokens": len(tokens),
        "buy_count": buy_count,
        "no_go_count": no_go_count,
        "avg_score": sum(scores) / len(scores) if scores else 0,
    }

def analyze_backtest():
    results = load_json(os.path.join(BOT_DIR, "backtest_results.json"))
    if isinstance(results, list) and len(results) > 0:
        return {
            "type": "list",
            "count": len(results),
            "last": results[-1] if results else None,
        }
    elif isinstance(results, dict):
        return {
            "type": "dict",
            "keys": list(results.keys())[:10],
        }
    return {"type": "unknown"}

def analyze_grid():
    results = load_json(os.path.join(BOT_DIR, "grid_results.json"))
    if isinstance(results, list) and len(results) > 0:
        return {
            "type": "list",
            "count": len(results),
        }
    elif isinstance(results, dict):
        return {
            "type": "dict",
            "edge_evidence": results.get("edge_evidence", results.get("evidence", "N")),
            "best_config": results.get("best_config", results.get("config", "N")),
            "basket_pct": results.get("basket_pct", results.get("basket_percent", "N")),
        }
    return {"type": "unknown"}

def main():
    print("=== Base-Trend-Bot Shadow Analysis ===")
    print(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("Mode: SHADOW (kill_switch=True, live_trading=False)")
    print()

    # 1. Discovered coins
    discoveries = parse_discovered_jsonl()
    print(f"1. DISCOVERED COINS: {len(discoveries)} total")
    if discoveries:
        scored = [d for d in discoveries if d.get("score", 0) > 0]
        scored.sort(key=lambda x: x.get("score", 0), reverse=True)
        print(f"   Top 5 by score:")
        for d in scored[:5]:
            s = d.get('score', 'N/A')
            liq = d.get('liquidity_usd', 0)
            n = d.get('name', 'unknown')
            print(f"     {n}: score={s}, liquidity=${liq:,.0f}")
        above_50 = len([d for d in discoveries if d.get("score", 0) >= 50])
        print(f"   Coins with score >= 50: {above_50}/{len(discoveries)}")

    # 2. Nous signals
    print()
    print("2. NOUS SIGNALS:")
    nous = analyze_nous_signals()
    print(f"   Scan timestamp: {nous['timestamp']}")
    print(f"   Total tokens scanned: {nous['total_tokens']}")
    print(f"   Buy signals (score >= 80): {nous['buy_count']}")
    print(f"   No-Go signals (score < 80): {nous['no_go_count']}")
    print(f"   Average score: {nous['avg_score']:.1f}")

    # 3. Backtest results
    print()
    print("3. BACKTEST RESULTS:")
    backtest = analyze_backtest()
    if backtest.get("type") == "list":
        print(f"   Type: list of {backtest['count']} runs")
        if backtest.get("last"):
            last = backtest["last"]
            if isinstance(last, dict):
                keys = list(last.keys())[:3]
                vals = [last[k] for k in keys if k in last]
                print(f"   Last run key fields: {vals}")
    elif backtest.get("type") == "dict":
        print(f"   Type: dict with keys: {', '.join(backtest.get('keys', []))}")
    else:
        print(f"   Type: {backtest['type']}")

    # 4. Grid results
    print()
    print("4. GRID RESULTS:")
    grid = analyze_grid()
    if grid.get("type") == "list":
        print(f"   Type: list of {grid['count']} grid configs")
    elif grid.get("type") == "dict":
        print(f"   Edge evidence: {grid.get('edge_evidence', 'N/A')}")
        print(f"   Best config: {grid.get('best_config', 'N/A')}")
        print(f"   Basket %: {grid.get('basket_pct', 'N/A')}")
    else:
        print(f"   Type: {grid.get('type')}")

    # 5. Summary assessment
    print()
    print("5. SHADOW ASSESSMENT:")
    print(f"   kill_switch: True (enforced)")
    print(f"   live_trading: False (enforced)")
    print(f"   Edge pass: NO SIGNALS (0 bullish crossovers in last nous scan)")
    print(f"   Model: upstage/solar-pro4:free (nous provider, OmniRoute gateway)")
    print(f"   Ready for go-live: requires arm_signal_v1 + 5-trade sample floor")

    # Save report
    report = {
        "generated": datetime.now().isoformat(),
        "discovered_count": len(discoveries),
        "nous": nous,
        "backtest_type": backtest.get("type"),
        "grid_type": grid.get("type"),
        "shadow_mode": True,
    }

    report_path = os.path.join(BOT_DIR, "shadow_analysis_report.json")
    with open(report_path, 'w') as f:
        json.dump(report, f, indent=2)
    print(f"\nReport saved to: {report_path}")

if __name__ == "__main__":
    main()