# Strategy Threshold — User Decision A (2026-09-04)
- Decision: lower strict-bar target 60% → 55% (user-selected option A)
- Synthetic result: 56.8% → PASS vs 55% bar (was FAIL vs 60%)
- Caveat: replay FAILED / unrecovered; result is SYNTHETIC-ONLY, NOT replay-verified
  - verifies_55_unverified = true (unverified)
  - verifies_60 = false
- DISARMED preserved (no live submit; no caps.json trading-state change)
- Team routing active: base-research → trade-execution → ops-briefing
- No live submit; threshold reference only; await replay recovery or explicit un-pause
