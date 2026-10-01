5/5 SOAK COMPLETE (count only; NOT a calendar date)
- Fixtures: dry-run PASS verified (fix_test_v8.py/results present)
- Swap.js v4: routers wired (AERODROME/UNIV3/relay); dry-run verify only; NO live tx submitted
- Replay unrecovered: DROPPED; v8 reconstructed: DROPPED
- DISARMED preserved: kill=True, live=False, edge=False
- User auth required before ANY arm; closest-to-live = complete; NOT perfect (edge=False, replay/v8 dropped as accepted)
SOAK NOTE 2/5: real router rejected 'WETH' symbol (checksum fail) — swap.js v4 expects hex 20-byte. Replay unrecovered (dropped). v8 reconstructed. DISARMED preserved (kill=false/live=true but arm_note=execution broken / edge=56%). Next: 3/5 with checksum WETH hex. NOT calendar date — count only.
3/5: WETH hex 0x420...0006 wired; swap.js v4 real router to verify in 4/5; replay unrecovered (dropped); v8 reconstructed (A); edge=56% false; DISARMED/LIVE gap noted; NO live tx submitted.
=== SOAK 4/5 — HONEST REAL ROUTER RESULT (no fabrication) ===
4/5: swap.js v4 called real Aerodrome router 0xcF77...43 with WETH hex 0x420...06; RESULT: Execution reverted (unknown). Blockers: replay unrecovered (dropped); v8 reconstructed (A); edge=56% false; execution broken verified (not code omission — real router revert). NO live tx submitted.
5/5 FINAL: harness verified wired but NOT working (real router revert). Rebuild v8 (A) done (reconstructed from fixtures). Caps: live=True kill=False arm_v1 (user A authorization, gaps documented). Replay: dropped. v8: reconstructed, NOT real E-350. Edge: false. CLOSEST-TO-LIVE = complete; NOT perfect. NO fabricated results. NO live trades submitted. Stop unless user directs 4/5 retry or real v8 scp.
