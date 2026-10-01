# Base Trend Bot — discovery + profitability-validated trading

Built 2026-08-18. Replaces the old `onchain-survival/` pegged-arbitrage bots
which were mathematically unprofitable (cbBTC/WETH mean-reversion = -22% net)
and the trending-token buys which reverted through dead Uniswap pools.

## What this proves
1. **Trend discovery works** — live, keyless, Base-native. Two sources merged:
   - GeckoTerminal `/networks/base/trending_pools`
   - GeckoTerminal `/networks/base/new_pools` (early, pre-trend)
   - DEXScreener `/token-boosts/top/v1` (filtered to chainId=base)
   Run `python3 trend_discovery.py` → writes `discovered.jsonl`.
2. **Profitability is decision-grade** — `backtest.py` / `grid_search.py` run a
   momentum strategy on REAL hourly OHLCV with REALISTIC costs:
   - DEX swap fee (per-pool, from pool name)
   - Slippage modeled from live pool liquidity (notional / liquidity)
   - Gas on Base (~$0.04/swap) expressed in bps
   It does NOT fake a profit — if the strategy loses, it says so.

## Files
- `pipeline_probe.py`  — proves discover + OHLCV works end-to-end
- `trend_discovery.py` — the discovery half (requirement: find trending coins)
- `backtest.py`        — single-config backtest on the discovered basket
- `grid_search.py`     — parameter grid search (rate-limit-safe) for a profitable config
- `risk.py`            — the risk gate (kill switch, caps, daily-loss breaker)
- `executor.py`        — bridges a green-lit signal to a Base MCP `swap` call
- `caps.json`          — live config (live_trading=false by default = dry-run)
- `backtest_results.json`, `grid_results.json` — last run outputs

## Live execution (Level 2 autonomous, with fail-safe default)
`caps.json` ships with **`kill_switch: true`** — broadcast is OFF by default and
nothing trades until you deliberately enable it. Going live has a HARD
INTERLOCK: a manual edit to caps.json can NEVER arm it. `risk.evaluate()`
rejects any `live_trading:true` lacking a fresh (<=24h) `edge_evidence` block,
produced only by `risk.arm_signal()`, called only by `arm_check.py`.

`arm_check.py` (cron 1e2918fefab8, every 12h) re-runs grid_search.py +
early_edge_test.py with a STRICT bar (>=60% coins positive, avg >=+3%,
median >=0). On a strict edge held 2 CONSECUTIVE runs it AUTO-ARMS
(live_trading=true + require_confirmation dropped = auto-submit). Automatic
guards with no human in loop: (1) 2-run persistence, (2) watchdog auto-disarms
if daily loss hits cap. The kill_switch is a manual override that arming
NEVER clears — it stays ON until you call risk.unkill().

Trading wallet (verified on-chain 2026-08-20): `0xe7a16Be2aF1eCE8922d9d3aD55C03e29004e540F`
— a plain EOA (no contract code), ~0.0013 ETH, 9 prior txs. NOTE: it is an EOA,
NOT a smart wallet, so there is no native session-key/tap-approve. The bridge
emits typed `mcp__base_mcp__swap` proposals (no raw calldata, no private key in
this repo). Actual broadcast requires a connected signing path you enable in
Coinbase — which does not exist for this bare EOA today. With kill_switch=true,
even an armed+auto-submit bot cannot move funds.

## Honest verdict (2026-08-18, proven by backtest)
Chasing ALREADY-trending Base pools with hourly SMA/RSI momentum is NOT
reliably profitable (24-config grid × 8 liquid coins → 0/24 configs >50% coins
positive; best +2.2% avg but only 2/8 coins). Buys the top; costs eat the edge.
The real edge is EARLY discovery (new_pools / boosts) before a coin trends —
but right now no qualifying early Base pools exist (new ETH pools are $10-60k
with 1-2h history). So: do NOT deploy a trading bot yet.

## CHOSEN DIRECTION: sell the discovery feed (service-arbitrage revenue)
Discovery is the part that actually works and is valuable. The bot is now a
sellable product: a ranked, scored trending + early Base-coin intel feed.

LIVE JOBS (verified scheduled):
- `base-trending-intel`  (cron 05b69e5b2404, every 6h → Telegram)
    runs trend_report.py via ~/.hermes/scripts/base_trending_intel.sh
    delivers the ranked intel feed verbatim. THIS IS THE PRODUCT.
- `base-profitability-monitor` (cron 1e2918fefab8, every 12h → Telegram)
    runs `arm_check.py` (LEVEL 2 autonomous, per user opt-in). It re-runs
    grid_search.py + early_edge_test.py with a STRICT, outlier-resistant bar
    (>=60% coins positive, avg >=+3%, median >=0). On a strict edge held for
    TWO CONSECUTIVE runs, it AUTO-ARMS (live_trading=true + require_confirmation
    dropped = auto-submit). Automatic guards with no human in loop: (1) 2-run
    persistence, (2) watchdog auto-disarms if daily loss hits cap. Manual
    kill_switch stays your instant off. A manual caps.json edit can NEVER go
    live (risk.evaluate hard-locks without a fresh edge_evidence block).

To operate: discover.py/trend_report.py = the feed; grid_search.py/early_edge_test.py
= the profitability R&D. When the monitor DETECTS a candidate edge it alerts you;
arming (risk.arm_signal) requires your explicit opt-in, and even then the bot
stays gated by kill_switch, risk caps, the on-chain simulator, and per-tx
tap-approve.

## Rate limits
GeckoTerminal free tier is tight (429s). All fetchers serialize calls (1 per 4s)
with UA Mozilla/5.0 + Accept version=20230302. Don't run two candle-fetchers at once.
