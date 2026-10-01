# Replay unrecoverable (user-approved 2026-09-04)
- replay_state.json: recovered=False (original + .bak missing)
- rebuild_caps.json excludes replay (dropped)
- Reconstruction exists (backtest + grid) but is not original replay
- Recommendation (applied): skip replay recovery; proceed harness soak -> v8 unblock -> un-pause execution
- DISARMED preserved until un-pause is explicit
