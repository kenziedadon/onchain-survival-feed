# SOUL.md — Base Trend Bot Canonical Prompts (consolidated 2026-09-17)

All base-trend* Hermes cron prompts consolidated here. Source of truth for discovery, execution, monitoring, edge, status, orchestrator, dry-run, macro-regime.

## Active Jobs (2026-09-17)

| id | name | schedule | enabled | target |
|----|------|----------|---------|--------|
| 26056d1bd04c | base-trend-discovery (v8 momentum) | every 5m | true | origin,telegram |
| d9433af15759 | base-trend-trade-execute (v8 live) [PAUSED/DISARMED] | every 15m | false | origin,telegram |
| 3cba324245ce | base-trend-monitor (v8 gate check) | `*/30 * * * *` | true | origin,telegram |
| 88cc46b23032 | base-trend-edge-check | `0 6 * * *` | true | telegram |
| 65e51ee84636 | base-trend-status-brief | `0 8 * * *` | true | telegram |
| a42316e62371 | base-trend-bot go-live monitor | every 30m | true | local |
| 05b69e5b2404 | base-trending-intel | `0 */6 * * *` | true | telegram |
| da1c81164881 | base-trend-orchestrator (replaces 5 jobs) | every 15m | true | origin,telegram |
| 1752c1d7e6e4 | base-macro-regime check | `0 */4 * * *` | true | telegram |
| 2d4172f94c72 | trade-execution-dry-run | `*/15 * * * *` | true | telegram |

## Prompt Catalog (verbatim)

---

**26056d1bd04c — base-trend-discovery (v8 momentum)**
You are the Base trend discovery agent. Run the new v8 trend discovery and report any tradeable trending coins.
1. Run: `cd /home/kenzodadon/base-trend-bot && python3 trend_discovery.py 2>&1 | tail -20`
2. The script writes discovered coins to discovered.jsonl
3. IF coins were discovered, also run: `python3 backtest.py 2>&1 | tail -25` to validate profitability
4. Report: how many coins met the filters, their scores, the backtest basket verdict
5. IF basket return > 5% AND verdict says "PROFITABLE", report the top 3 coins by score as live trade candidates
6. Keep it concise — max 3 lines. Deliver to origin + telegram.

---

**d9433af15759 — base-trend-trade-execute (v8 live)** [PAUSED / DISARMED — kill_switch=true, live_trading=true, no arm_signal_v1]
You are the Base trend trade execution agent. Check if the v8 strategy has a green signal and execute trades if live.
1. Check the risk gate state: read caps.json — if kill_switch:true or live_trading:false, report "DISARMED" and exit. Read grid_results.json — if no results, report "NO EDGE" and exit.
2. Check the latest backtest verdict: run `backtest.py | tail -5`; IF "PROFITABLE" proceed, ELSE report and exit.
3. Run executor --dry-run; for each ALLOWED proposal call mcp__base_mcp__swap({fromAsset:"ETH",toAsset:<token_out>,amount:<amount_eth>,chain:"base"}); after fill update risk state via record_fill.
4. Keep output concise. If nothing trades, report "NO TRADES this cycle."

---

**3cba324245ce — base-trend-monitor (v8 gate check)**
You are the Base trend strategy monitor. Check the v8 strategy gate and report status.
1. Read caps.json (live_trading / kill_switch) 2. Read backtest_results.json 3. Read discovered.jsonl 4. Report.
IMPORTANT: Live trading DISABLED. Execution path verified working (swaps route via Aerodrome + Uniswap V3). Paper-trading mode only. Report strategy perf, gate status, trending coins, honest assessment.

---

**88cc46b23032 — base-trend-edge-check**
Run: bash ~/.hermes/scripts/edge-check-runner.sh (runs arm_check.py: grid search + early edge + methodology eval + strict-bar eval), prints latest grid result + caps.json safety state. Report: (1) edge_evidence change, (2) best grid config + basket %, (3) safety state. If edge pass=PENDING_HUMAN_ARM: say "EDGE CONFIRMED — human arm decision needed, run enable_broadcast.sh". Dual-quote note: execution is strictly WETH-only; USDC-quoted entries are observation-only staging.

---

**65e51ee84636 — base-trend-status-brief**
Run: cd ~/base-trend-bot && python3 ops_briefing_status.py. Report output verbatim. If on-chain balance or edge-evidence drifts from caps.json, flag it. Read-only — do NOT modify files, do NOT execute trades. Dual-quote note: USDC-quoted discovered entries are intentional observation-only staging (Phase 2 pending) — do NOT report skipped USDC buys as executor failures.

---

**a42316e62371 — base-trend-bot go-live monitor** (script `go_live_loop.py`, no_agent)
Watches caps.json kill_switch. Autonomous go-live orchestrator — runs discovery + grid + methodology eval → arm_check.py → if strict edge x2, auto-arms via risk.arm_signal and runs executor LIVE, else risk.kill() disarms. Deliver local.

---

**1752c1d7e6e4 — base-macro-regime check**
Run the macro regime filter. Read ~/.hermes/shared/macro_regime.json (or compute from on-chain data). Report: current regime (bulling/ranging/crashing), filter action (PASS/HOLD/FAIL), brief reasoning. wd base-trend-bot.

---

**2d4172f94c72 — trade-execution-dry-run**
Run: cd /home/kenzodadon/base-trend-bot && python3 executor.py --dry-run 2>&1 | tail -30. Report: any trade proposals, their scores, risk gate status, whether execution is DISARMED. Concise.

---

**05b69e5b2404 — base-trending-intel** (historically disabled/enabled but problematic; verify before trusting)
Driver: base_trending_intel.sh. Script-only (empty prompt). Verify before trusting.

---

**da1c81164881 — base-trend-orchestrator** (replaces 5 jobs)
Driver: base-trend-orchestrator.sh. Every 15m. Script-only body; prompt empty (script handles execution + agent routing).

---

Source: consolidated 2026-09-17 per user instruction ("Remove cronjobs we are no longer using or change to SOUL.md for bot trading team").
Live source of truth remains ~/.hermes/cron/jobs.json; this file is the canonical prompt reference.

---

## Recent Fixes (2026-09-17)
- **executor.py hangs on dry-run**: Fixed — `_run_exits()` now has early return when no holdings. `MAX_COINS_PER_CYCLE=50` (env-tunable) prevents rate-limit death.
- **fused_server.py syntax error**: Fixed — all 59 tools have correct `inputSchema` dicts. TV tools are slow (20s timeout from MCP resilience layer) but functional. FOMO API key exhausted (HTTP 402).
- **Cron consolidation**: 49 → 23 enabled jobs. 26 disabled (predbot, survival-refresh, momentum bot live, content missions, etc.).
- **DISARMED status preserved**: User selected option 2 (keep dry-run). Will re-evaluate after 48h of discovery accumulation.

---

## Fused MCP Server (2026-09-17)
- Server: `fused_server.py` in `/home/kenzodadon/fomo-mcp/`
- 59 tools: 19 FOMO + 40 TradingView
- FOMO tools: `get_profile`, `get_leaderboard`, `get_trade_feed`, `get_balances`, `get_token_holders`, `get_token_stats`, `get_token_devs`, `get_thesis`, `search_traders`, `check_new_trades`, `quote_swap`, `execute_swap`, `copy_trade_scan`, `trading_status`, `get_token_board`, `get_alerts`, `get_notifications`, `get_alerts_ws_url`, `get_trades_ws_url`
- TV tools: `top_gainers`, `top_losers`, `bollinger_scan`, `rating_filter`, `coin_analysis`, `consecutive_candles_scan`, `advanced_candle_pattern`, `volume_breakout_scanner`, `volume_confirmation_analysis`, `smart_volume_scanner`, `multi_agent_analysis`, `egx_market_overview`, `egx_sector_scan`, `egx_sector_scanner`, `egx_index_analysis`, `egx_stock_screener`, `egx_trade_plan`, `egx_fibonacci_retracement`, `smart_money_analysis`, `multi_timeframe_analysis`, `market_sentiment`, `financial_news`, `combined_analysis`, `backtest_strategy`, `compare_strategies`, `walk_forward_backtest_strategy`, `yahoo_price`, `market_snapshot`, `bitcoin_market_pulse`, `stock_extended_hours`, `stock_options_chain`, `stock_options_unusual_activity`, `futures_market_overview`, `futures_top_movers`, `futures_category_snapshot`, `futures_watchlist`, `stock_screener`, `stock_prices`, `exchanges_list`
- Status: Syntax error fixed. TV tools functional but slow. FOMO API key exhausted.

---

## CAPS STATE (verified live, 2026-09-17)
kill_switch=true / live_trading=true / arm_provenance=arm_signal_v1 / edge_evidence.pass=true (56%, strict bar, n=9) / wallet=0xe7a1...540F / balance_eth=0.001294 / max_trade_eth=0.001 ETH / max_positions=3 / active_strategy=simple_ema_v1
