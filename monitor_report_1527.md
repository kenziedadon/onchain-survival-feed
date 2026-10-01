Cronjob Response: base-trend-monitor (v8 gate check)
(job_id: 3cba324245ce)
-------------

Base Trend v8 Strategy — Monitor Report
Generated 2026-08-20 (cron) · Wallet 0xe7a1...540F

1. Gate Status — RED (paper-trading only)
live_trading: false
kill_switch: true (instant off)
edge_evidence.pass: false (grid_pass=false, early_edge_pass=false)
min_score / max_daily_loss / gas_buffer: 35 / 0.15 ETH / 0.0005 ETH
risk_state: spent_eth=0, pnl_eth=0, trades=[]

No capital deployed. Correctly fully disarmed.

2. Strategy Performance (backtest_results.json — 11 pairs)
- Basket avg return ~ +7.9% (passes the >5% check), but thin and outlier-driven:
  DRB/WETH +50.1% (3 trades) and OVPP/WETH +29.8% (1 trade) carry the number.
- Per-trade win rate = 11/18 (61%), profit_factor mixed.
- Losers: Surplus/WETH -10.6% (5t, 2W/3L), Basecat/ETH -7.2%, KTA/WETH -3.1%.
- Winners: DRB +50%, OVPP +30%, KEYCAT +8.1%, VIRTUAL 0.05% +8.5%,
  VIRTUAL 0.7% +7.1%, BRETT +3.9%.
- cbBTC x2: 0 trades (no signal) — high-liq but non-trending for the entry filter.
- Realistic read: promising direction, not yet statistically robust (small N, 2 outliers).

3. Trending Coins Discovered (latest scan 15:27:33)
Healthy discovery (6-10 coins/scan, GeckoTerminal): cbBTC/WETH 0.05% (95, $8.2M, +5.6%),
cbBTC/WETH 0.01% (82, $5.5M), KTA/WETH (75, $4.1M, +3.3%), BRETT/WETH (55, +12.6%),
Basecat/ETH (55, +2.4%), VIRTUAL/WETH 0.05% (55). Recurring names: KTA, BRETT, VIRTUAL,
Basecat, Surplus, KEYCAT, DRB, OVPP, VVV, BNKR, GITLAWB. One transient empty scan at 14:53.

4. Honest Assessment — Execution Path
The stated blocker is misdiagnosed. caps.json claims "Swaps revert on all routers /
no accessible liquidity." The actual risk_state.json logs contradict this:
- All early proposals were blocked by kill_switch active — never reached a router.
- The 13:37 test batch that did attempt broadcast failed with:
    ERROR: Value '5.9429378317010284e-05' is not a valid decimal number.
  That is viem's parseEther rejecting scientific-notation — a Python->JS number-formatting
  bug, not a liquidity/router problem. No swap ever touched a router, so
  "no accessible liquidity" is unverified.
- The current executor.py (line 35) already uses f"{amount_eth:.18f}".rstrip("0"), which
  emits plain decimals — so the format bug appears already fixed in code, but it has never
  been re-tested against a live router because the kill-switch blocks execution first.

To resume live trading (in order):
1. Fix/confirm amount formatting -> plain decimal (no e- notation) reaching swap.js.
2. Verify swap.js ABI actually matches Aerodrome V3 SlipStream (it currently declares
   swapExactETHForTokens, a V2-style Uniswap fn — needs on-chain confirmation).
3. Run a single tiny real swap (e.g. 0.00005 ETH) in a controlled test to prove the
   router path works.
4. Only then set edge_evidence.pass=true and flip kill_switch=false + live_trading=true.

Bottom line: Gate is correctly RED. Discovery and backtest are healthy; the real execution
blocker is an unverified code/ABI path, not liquidity. Do not re-arm on the current
"liquidity" rationale — it isn't supported by the logs.
