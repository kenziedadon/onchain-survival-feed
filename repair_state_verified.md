# Verified repair state (re-written after persistence check)
- DISARMED: kill=True live=False arm=v1 edge=False (caps re-read)
- Replay: NONE found (unrecovered; '--tx 0x dropped')
- v8: reconstructed only (real v8 on E-350 unrecovered)
- Harness: fixtures PASS; real router soak NOT done
- PERFECT: NO (edge=False + replay unrecovered + v8 reconstructed)
- Next: replay recovery OR real v8 scp OR real-router harness soak

- USER 2026-09-01: 'You and the team fix this so we can go live' -> TEAM ACTIVATED.
- Closest-to-live fix selected: harness soak (C) — real router verify swap.js v4 dry-run.
- Replay unrecovered and v8 reconstructed: DOCUMENTED BLOCKERS, not hidden.
- Go-live requires: harness PASS + replay recovery OR real v8 + edge=True + user auth. NOT MET YET.

- USER DIRECTIVE (verified): 'V8 replay drop it' -> replay unrecovered ACCEPTED; v8 reconstructed ACCEPTED; no E-350 scp; no replay recovery attempt.
- Repair reduced: drop replay/v8 as blockers; closest-to-live = harness soak dry-run + user auth + edge gate.
- DISARMED preserved; NO live trading; no fabricated results.
