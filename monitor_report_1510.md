Cronjob Response: base-trend-monitor (v8 gate check)
(job_id: 3cba324245ce)
-------------

Base Trend Strategy Monitor — v8 Gate Status

Report time: 2026-08-20 ~15:10 UTC | Mode: PAPER-TRADING ONLY

1. Gate status (caps.json)
live_trading
• Value: false

kill_switch
• Value: true (engaged)

edge_evidence.pass
• Value: false (grid_pass=false, early_edge_pass=false)

auto_confirm
• Value: false

min_score / max_loss_eth
• Value: 35 / 0.15 ETH

The gate is correctly closed. Caps detail reads "EXECUTION PATH BROKEN: swaps revert on all routers." Live trading must NOT resume until that is fixed.

2. Backtest performance (backtest_results.json — 12 coins, 21 trades)
- Win rate: 57.1% (12W / 9L)
- Mean net return: +7.11% (all 12) / +8.53% (10 coins that actually traded) -> clears the skill's >5% check
- Isolated basket equity: +104.9% — inflated & not a real portfolio number (per-coin isolated runs; DRB +45% and OVPP +30% are single-trade outliers)
- Winners: DRB/WETH +45.3% (PF 5.65), OVPP +29.8%, VIRTUAL 0.05% +8.6%, KEYCAT +8.1%, BRETT +4.5%
- Losers (PF<1): Surplus/WETH -10.56% (PF 0.37, 18.8% DD — worst), Basecat -7.2%, KTA -2.3%, BNKR +1.9% net
- Caveat: tiny samples (1-6 trades/coin); aggregate is driven by a couple of lucky single trades. Treat +7-8% mean as directional only, not a live expectation.

3. Trending coins discovered (discovered.jsonl — 14 scans, 15 unique)
Latest scan (15:08 UTC) had 13 active coins. Sorted by score:

cbBTC/WETH 0.05% — score 95, liq $8.2M, 24h +8.7%
VVV/WETH 0.05% — score 95, liq $2.6M, 24h +0.4%
cbBTC/WETH 0.01% — score 82, liq $5.5M, 24h +8.5%
KTA/WETH — score 75, liq $4.1M, 24h +8.8%
BRETT/WETH 1% — score 55, liq $0.7M, 24h +17.3%
DRB/WETH 1% — score 45, liq $0.8M, 24h +89.9%
OVPP/WETH — score 28, liq $0.3M, 24h +32.9%
+ Basecat, VIRTUAL x2, Surplus, BNKR, KEYCAT, THEO, GITLAWB — score <=55, liq $0.3-1.7M

Discovery is healthy — 10+ liquid, trending Base pairs found every scan.

4. Honest assessment — execution path must be fixed first
- The coins are genuinely liquid ($0.3M-$8M in on-chain liquidity). The caps.json "no accessible liquidity" note is a misdiagnosis — the problem is the bot's own self-signed swap.js (viem) execution path reverting on every router, not the tokens.
- Discovery + backtest are working in paper mode. The strategy signal is plausibly positive but unproven at portfolio scale (small samples, outlier-driven).
- Do NOT flip kill_switch to false / live_trading to true until swap.js reliably fills on at least one high-liquidity pair (e.g. KTA or cbBTC/WETH) and a fresh monitor run sets edge_evidence.pass=true.
- Recommended next step: debug the swap execution path (router/calldata/slippage routing in swap.js) against a liquid pair on a read-only dry run, then re-run the monitor gate for real evidence.

Bottom line: Gate is correctly closed. Discovery OK, backtest OK (cautiously positive), execution BROKEN. Paper-trading only until the swap path is fixed and the monitor gate passes on real fills.
