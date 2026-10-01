#!/usr/bin/env python3
"""
Discovery adapter: write signals.py opportunities to discovered.jsonl
in the format executor.py expects.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from signals import scan_opportunities

BASE_DIR = Path(__file__).resolve().parent
OUT_PATH = BASE_DIR / "discovered.jsonl"


def main():
    # Pull macro regime from caps for filtering
    caps_path = BASE_DIR / "caps.json"
    try:
        caps = json.loads(caps_path.read_text())
        macro_cfg = caps.get("macro_regime", {})
        regime = "neutral"
        if macro_cfg.get("enabled"):
            from macro_regime import evaluate_macro_regime
            allowed, reason = evaluate_macro_regime()
            if not allowed:
                print(f"Macro filter BLOCK: {reason}")
                return
    except Exception:
        regime = "neutral"

    min_score = caps.get("min_score", 50)
    opportunities = scan_opportunities(macro_regime=regime, min_score=min_score)

    if not opportunities:
        print("No qualifying opportunities.")
        return

    # Write as JSONL
    lines = []
    for opp in opportunities:
        entry = {
            "name": opp.get("token_symbol", "?"),
            "symbol": opp.get("token_symbol", "?"),
            "address": opp.get("pool_address", ""),
            "token_addr": opp.get("token_address") or opp.get("pool_address", ""),
            "price_usd": opp.get("price_usd", 0),
            "liquidity": opp.get("liquidity_usd", 0),
            "liquidity_usd": opp.get("liquidity_usd", 0),
            "vol_24h": opp.get("volume_24h_usd", 0),
            "volume_24h_usd": opp.get("volume_24h_usd", 0),
            "score": opp.get("score", 0),
            "price_change_5m": opp.get("price_change_5m", 0),
            "price_change_1h": opp.get("price_change_1h", 0),
            "ts": datetime.now(timezone.utc).isoformat(),
            "source": "signals_scanner",
        }
        lines.append(json.dumps(entry))

    OUT_PATH.write_text("\n".join(lines) + "\n")
    print(f"Wrote {len(lines)} opportunities to {OUT_PATH.name}")
    for line in lines[:5]:
        obj = json.loads(line)
        print(f"  {obj['name']} score={obj['score']:.1f} liq=${obj['liquidity_usd']:,.0f} "
              f"vol=${obj['volume_24h_usd']:,.0f} chg5m={obj['price_change_5m']:.1f}%")


if __name__ == "__main__":
    main()
