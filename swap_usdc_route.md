USDC swap route rebuild proposal (NOT EXECUTED — DISARMED, no live tx):
- USDC token verified (basescan, free curl 2026-09-02): 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
- Router primary (verified swap.js): AERODROME 0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43
- Router fallback (verified swap.js): UNISWAP V3 0x2626664c2603336E57B271c5C0b26F421741e481
- Relay aggregator (checksum-normalized fallback, verified swap.js line 33/554): api.relay.link/quote
- Current skeleton (swap_usdc_skeleton.js): STUB ONLY; native-ETH swapExactETHForTokens NOT compatible with USDC quote
- REBUILD NEEDED: replace native-ETH exactETH call with USDC-exactInput/quote call; maintain max_slippage_bps=100; size ladder step 1 = 0.0005 ETH (convert to USDC equivalent at quote time); NATIVE ETH pair dropped per user override
- Gaps: router code path not rebuilt; dry-run NOT performed; harness soak NOT done; replay EXCLUDED; v8_refs=[]; closest-to-live remains armed-not-live
- Next before any arm/full execution: (a) rebuild call path, (b) quote dry-run, (c) verify gas/slippage clamp, (d) harness soak, (e) only then consider full arm (currently B-partial only)
