# SEAL2 — simple_ema_v1 (2026-10-06)

> **Sealed at:** 2026-10-06T23:53:00Z
> **By:** McKenzie (0xe7a16Be2aF1eCE8922d9d3aD55C03e29004e540F)
> **Prior seal:** 2026-09-27 (8 of 9 params diverged; verdict 0/1700 does NOT transfer)

## 1. Sealed Code Digests (sha256)

| File | Path | sha256 |
|------|------|--------|
| Strategy code | `/home/kenzodadon/base-trend-bot/simple_ema.py` | `c05e0244fa36690a722d5c5b443845d39d8be06f23108a196b25e4bdc9bd7b40` |
| Config | `/home/kenzodadon/base-trend-bot/caps.json` | `0bb3c9a36f42e0f6ab2c6c37671c5b5baf56cf4e6f0b0da5182cd8419b5cca55` |
| Strategy loader | `/home/kenzodadon/base-trend-bot/strategy.py` | `1cb789bc8633d41bc71359e745f0a4a39a9a003c3cc49ec9e494767e978c7e57` |
| Executor | `/home/kenzodadon/base-trend-bot/executor.py` | `1fdfce7b82c75f300a4230d08dddf2d54550c601f13b2cac1a461de65209aa36` |

**Repo HEAD commit:** `543686e13b541f0004c22b3209bddcd0b71aaa26` (git rev-parse HEAD)

**Captured:** 2026-10-06T23:53:00Z

## 2. Frozen Parameter Table (post unit-fix)

All values are **as on disk** after the unit-normalization fix applied on 2026-10-06.

| Parameter | Value | Units | Source |
|-----------|-------|-------|--------|
| active_strategy | simple_ema_v1 | — | caps.json:2 |
| strategy.name | simple_ema_v1 | — | caps.json:4 |
| strategy.source | simple_ema.py | — | caps.json:5 |
| fast_period | 9 | bars | caps.json:13 |
| slow_period | 21 | bars | caps.json:14 |
| stop_loss_pct | 0.02 | fraction | caps.json:15 |
| take_profit_pct | 0.05 | fraction | caps.json:16 |
| use_momentum_confirm | true | bool | caps.json:17 |
| use_macd_filter | true | bool | caps.json:18 |
| use_stochrsi_filter | true | bool | caps.json:19 |
| stochrsi_hi | 80 | RSI scale (0-100) | caps.json:20 |
| min_vol_24h_usd | 50000 | USD | caps.json:21 |
| min_liquidity_usd | 50000 | USD | caps.json:22 |
| use_dip_buy | true | bool | caps.json:23 |
| dip_buy_pct | 0.02 | fraction | caps.json:24 |
| sell_high_pct | 0.04 | fraction | caps.json:25 |
| trailing_stop_pct | 0.015 | fraction | caps.json:26 |
| max_trade_eth | 0.0003 | ETH | caps.json:31 |
| max_positions | 2 | count | caps.json:35 |
| min_score | 25 | score (0-100) | caps.json:34 |
| max_slippage_bps | 100 | bps | caps.json:33 |
| kill_switch | true | bool | caps.json:28 |
| live_trading | false | bool | caps.json:29 |
| require_confirmation | true | bool | caps.json:30 |

## 3. Locked Protocol

- **Scored trades count from ZERO** as of seal time (2026-10-06T23:53:00Z).
- **Round-trip cost:** 700 bps per trade (0.07 ETH on 0.01 ETH notional at current prices).
- **Latency floor:** N=12 blocks (~24 seconds on Base) per discovery-to-entry cycle.
- **Exit logic:** ATR-based trailing stop (strategy.py:130-149, executor.py:721-724) + blowoff TP at +50% ROE (executor.py:706-707). The `dip_buy_pct`, `sell_high_pct`, and `trailing_stop_pct` parameters are loaded into `load_strategy_params()` but are **not consumed** by any live code path (see AC2 consumer grep below).
- **Pass criterion:** >=55% positive AND average >= +3.0% net return over 100 counted paper trades.
  - *55% bar authorized by McKenzie on 2026-10-06 ("Yes i did").*
  - *Pre-fix n=9 (56%) recorded as historical context, excluded from the 100-trade count.*
- **Safety:** kill_switch=true and live_trading=false are frozen for the entire 100-trade run. No real transactions are broadcast.
| **Locked Protocol (continued):**
|- **Max hold:** 24 bars per trade (scorer-side, seal2_scorer.py:221 — `exit_bar = min(entry_bar + 24, n_bars - 1)`). Clamped to available data.
|- **Validation harness:** research_harness/strategies/simple_ema_v1.py (the WO4 harness engine), NOT simple_ema.py. The executor's `_check_entry_timing()` (executor.py:590-591) returns True immediately for simple_ema_v1 — the EMA crossover IS the entry gate.

## 4. Effective Exit Logic (paper-trade path)

The sealed evaluation scores trades through the **research_harness/strategies/simple_ema_v1.py** engine:

- **Entry:** Fast EMA(9) crosses above Slow EMA(21) on hourly candles.
- **Exit:** First bar (after entry) where `close <= entry_price * (1 - stop_loss_pct)` triggers stop_loss; `close >= entry_price * (1 + take_profit_pct)` triggers take_profit.
  - `stop_loss_pct = 0.02` → exit at 2% adverse move
  - `take_profit_pct = 0.05` → exit at 5% favorable move

**Note:** The live executor (`executor.py:665-733`, `_check_exit`) uses a different exit path — ATR-based stops (`strategy.py:130-149`) + hardcoded blowoff at +50% ROE. The paper-trade harness uses the fixed % exits from `simple_ema_v1.py:81-88`. These are **two different engines** scoring the same strategy parameters. The seal evaluates the harness path (no look-ahead, bar-next resolution) as the canonical scorer.

## 5. Invalidation Rule

Any change to the frozen parameters (section 2) or sealed code digests (section 1) invalidates SEAL2. The 100-trade count resets to zero. A new seal must be generated and re-authorized by McKenzie.

## 6. Scored-Trade Counter

**Location:** `/home/kenzodadon/base-trend-bot/state/seal2_trade_counter.json`

```json
{"count": 0, "last_updated": "2026-10-06T23:53:00Z", "seal_id": "SEAL2-simple-ema-v1"}
```

**AC3: Counter value at seal time = 0**.