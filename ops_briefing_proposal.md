# ops/briefing proposal (rebuilt gate layer) — DISARMED

caps_struct: kill_switch=false (STAYS FALSE), live_trading=false, allowed_actions=[swap], max_positions=3, arm_provenance=null (no arm until harness soak + replay + auth — replay dropped per exclusion, so gate = router verified + size_ladder set + no v8 refs).

brief_format: concise, ground-truth only. Header: timestamp, caps_hash, kill/live/state, router_verified(Y/N), size_ladder_step, v8_refs=0. Body: 3 bullet facts (last on-chain router quote or verify result; cap file hash; open positions count). No fabricated trades; no projected PnL; no live-tx claims. Footer: gate status = PASS/FAIL with one-line reason.

team_handoff (4-stage): (1) base-research discovery → discovery.json / scout_state; (2) trade-execution proposal → execution proposal JSON (action, token, router address, size_eth ≤ ladder); (3) ops gate check → caps.json read + router basescan verify + v8-ref scan + size ladder; (4) brief output → brief_format above. No skip: execution blocked if gate FAIL.

verification_checklist: (a) basescan / on-chain router address matches swap target (e.g., aerodrome/getAmountsOut quote OK with real factory); (b) caps.json sha256 hash recorded in brief; (c) grep for v8 references in source/execution path = 0 hits; (d) replay file verified present or explicitly noted MISSING (excluded = FAIL); (e) size_ladder step set and current_size_eth within step; (f) kill_switch remains false, arm_provenance null until team confirms harness soak + replay (replay excluded) + auth.

best_pick: A — rebuilt ops/gate with DISARMED preserved; brief = numeric/letter pick (PASS/FAIL + hash + router + v8=0); no arm; no replay dependency satisfied; router verify + ladder set required before any live change.
