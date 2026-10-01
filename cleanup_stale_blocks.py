#!/usr/bin/env python3
"""One-off cleanup: drop stale anti-reentry blocks for tokens with zero on-chain balance."""
import json

P = "/home/kenzodadon/base-trend-bot/risk_state.json"
dead = {"0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",
        "0xc0634090f2fe6c6d75e61be2b949464abb498973"}
s = json.load(open(P))
before = len(s["blocked"])
s["blocked"] = [b for b in s["blocked"]
                if not ({b.get("proposal", {}).get("token_in"),
                         b.get("proposal", {}).get("token_out")} & dead)]
json.dump(s, open(P, "w"), indent=2)
print("removed", before - len(s["blocked"]), "stale blocks")
