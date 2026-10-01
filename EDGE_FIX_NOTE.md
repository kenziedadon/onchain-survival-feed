Edge fix applied (2026-09-03) per RESEARCH bot proposal:
- Tighten EMA slope threshold (simple_ema_v1)
- Macro regime gate: bull-only (MACRO_FILTER_ON=True)
- Target: >=60% edge from current 56% (caps truth)
- Size ladder: step 1 = 0.0005 ETH / USDC equivalent; max_positions=3
- Discovery: USDC-quoted pairs preferred; vol>$100k; liq>$250k; 24h>+2%; stables excluded
- Note: live backtest verification NOT performed (DISARMED; closest-to-live=armed-not-live); no fabricated results.
- Replay: EXCLUDED; v8_refs=[]; cap_hash=f91e7148; no fabricated trades.
