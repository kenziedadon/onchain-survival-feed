#!/usr/bin/env python3
"""Inspect trending_raw.json structure to nail the right JSON paths."""
import json

with open("/home/kenzodadon/base-trend-bot/trending_raw.json") as f:
    data = json.load(f)

print(f"Total pools: {len(data['data'])}\n")
for i, p in enumerate(data["data"][:5]):
    attrs = p["attributes"]
    rel = p["relationships"]
    b_id = rel.get("base_token", {}).get("data", {}).get("id", "?")
    q_id = rel.get("quote_token", {}).get("data", {}).get("id", "?")
    b_sym = b_id.split("_", 1)[-1] if b_id != "?" else "?"
    q_sym = q_id.split("_", 1)[-1] if q_id != "?" else "?"
    print(f"[{i}] name={attrs.get('name','?')}")
    print(f"    pool_id={p.get('id','?')}")
    print(f"    base_token.id={b_id}  hex={b_sym}")
    print(f"    quote_token.id={q_id}  hex={q_sym}")
    print(f"    reserve_in_usd={attrs.get('reserve_in_usd','?')}")
    print(f"    attrs keys: {sorted(attrs.keys())}")
    print()
