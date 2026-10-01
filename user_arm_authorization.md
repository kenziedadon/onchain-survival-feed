USER AUTHORIZATION (verified, recorded before any caps change)
- User statement: 'Armed with gaps' (explicit authorization)
- Verified gaps acknowledged: replay unrecovered (DROPPED); v8 reconstructed (not real); harness fixtures PASS (no real-router soak verified); edge=False (caps verified); settlements.log=3 lines ($0 organic)
- DISARMED → ARM: user overrides; no on-chain verification of gap fix; authorization is manual override per user preference (accepts manual override with authorization)
- Action: set kill_switch=false, live_trading=true, edge_evidence.pass stays false (user accepts false edge with authorization)
- Trade constraints (preserved): max_trade_eth=0.0005, WETH-only, caps source of truth; live-exec crons d9433af15759 / 5918ef85f0b9 may submit (caps allows)
- NO fabricated trades reported; authorization is user-stated, not system-verified safe
