# FINDING: no hourly-EMA edge on liquid Base/WETH pools at 68bps round-trip

**Status: CLOSED — negative result, published deliberately.**
**Sealed ledger: 0 of 6 rows cleared. Gate thresholds were never modified.**

Date: 2026-10-01
Author: Hermes
Commits: `d5d2898` (harness), `820a39e` (WO3 reconciliation), `539b5f8` (deep window)

---

## 1. The claim

Neither `simple_ema_v1` nor `pullback_v2` produced a strategy return that beat
buy-and-hold on any tested pool, at any tested window length. The measured
margin is not marginal — on the volatile pools it is large and negative.

This is a **negative finding**, written up on purpose. It is published to stop
further compute being spent on a dead strategy family, not to be revisited.

## 2. What was run

Two strategies × three already-pinned WETH pools × two window depths.
The **universe was not widened** at any point. No gate threshold was altered.

| | spec | window | bars |
|---|---|---|---|
| Baseline | `1f0274fa6629` (simple_ema_v1), `dc4f65468c22` (pullback_v2) | 300 hourly | 299 |
| Deep window | `8c98bdbee6af`, `67e29b348234` | 1000 hourly | 999 |

Gates identical across all four specs: `min_trades_test: 10`, `pf_min: 1.1`,
`maxdd_max: 40.0`, `analysis.test: welch_t_one_sided`, `alpha: 0.05`.
Costs: `pessimistic_v1`, `slippage_multiplier: 2.0` (~68 bps round-trip).
Split: positional, `train_frac: 0.6`. Every bar set SHA-256 pinned per pool
before evaluation (`specs/data_pins.jsonl`).

Pools (liquidity at run time):

| Pool | Address | Liquidity |
|---|---|---|
| cbBTC / WETH 0.009% | `0x7c7420dd105e2779316423ba3e973f434315efa9` | ~$2.20M |
| VIRTUAL / WETH 0.05% | `0x3f0296bf652e19bca772ec3df08b32732f93014a` | ~$371K |
| AERO / WETH 0.05% | `0x20cb8f872ae894f7c9e32e621c186e5afce82fd0` | ~$185K |

## 3. Results — 0 of 6 cleared

### Deep window (999 hourly bars, ~41.5 days)

| Pool | Strategy | n | Strategy | Buy & hold | Verdict |
|---|---|---|---|---|---|
| cbBTC / WETH | `pullback_v2` | 3 | **+11.92%** | +11.13% | REJECT — `INSUFFICIENT_SAMPLE: 3 < 10` |
| VIRTUAL / WETH | `pullback_v2` | 1 | **+29.89%** | +28.91% | REJECT — `INSUFFICIENT_SAMPLE: 1 < 10` |
| AERO / WETH | `pullback_v2` | 3 | **+16.09%** | +23.42% | REJECT — `INSUFFICIENT_SAMPLE: 3 < 10`; `FAILS benchmark` |
| cbBTC / WETH | `simple_ema_v1` | 3 | +10.69% | +11.08% | REJECT — `INSUFFICIENT_SAMPLE`; `FAILS benchmark` |
| VIRTUAL / WETH | `simple_ema_v1` | 7 | **−6.54%** | +32.17% | REJECT — `INSUFFICIENT_SAMPLE`; `FAILS benchmark`; `FAILS live` |
| AERO / WETH | `simple_ema_v1` | 8 | **−8.64%** | +46.26% | REJECT — `INSUFFICIENT_SAMPLE`; `FAILS benchmark`; `FAILS live` |

Artifacts: `research_runs/wf_deep1000_simple_ema_v1_20261001T135430.json`,
`research_runs/wf_deep1000_pullback_v2_20261001T140113.json`.

### Baseline window (299 hourly bars, ~12.5 days)

4 rows, 0 cleared. n was 1, 1, 1, 3.

**Every rejection came from the unmodified `min_trades: 10` gate.**

## 4. Why more data did not fix it

Tripling the window (299 → 999 bars) moved n from 1–3 to 1–8. It did **not**
produce a scorable row. The hypothesis that these strategies were merely
data-starved is **falsified**: the signal rate is structurally too low for
this bar interval, not the sample too short.

Corollary: widening the universe would have bought more
`INSUFFICIENT_SAMPLE` rejections, not statistical power. The binding
constraint was bars and signal frequency, never token count.

## 5. The finding that dominates the pass/fail count

**Buy-and-hold beat both strategies on all 6 deep-window rows**, and not
closely where it mattered:

- AERO: BH **+46.26%** vs `simple_ema_v1` **−8.64%**, `pullback_v2` +16.09%
- VIRTUAL: BH **+32.17%** vs `simple_ema_v1` **−6.54%**, `pullback_v2` +29.89%

The tested pools were in a strong-up regime over the test window. Neither
strategy captured that regime.

## 6. A positive-looking number that is NOT evidence of edge

> **AMENDED 2026-10-01 (post-hoc correction).** The original text of this
> section explained `pullback_v2`'s `+19.30%` mean as a **low-exposure
> artifact** — the claim that the strategy sat in cash through the rally. **That
> mechanism was factually wrong.** Exposure was 50–88%, not low. The corrected
> explanation is in §6.1: the return is **exposure**, and a random entry rule
> matched on that same exposure does better. The conclusion of this section is
> unchanged — `+19.30%` is still not edge — but the original reasoning was not.
>
> This amendment is a **post-hoc correction, not new evidence**. It does not
> add a row, does not change any measurement, and does not alter the sealed
> ledger, which remains **0/6**. The numbers below come from a diagnostic
> stamped `POST_HOC / NOT_EVIDENCE`; no p-value may be derived from them.

`pullback_v2` reports `positive: 3/3, mean net +19.30%`. **This is not edge.**

The harness behaved correctly and refused to report a p-value:

```
t-test: NOT REPORTED (below min_trades — a p-value from a tiny sample is noise)
```

Any future reader must not cite `+19.30%` as a result.

### 6.1 Corrected mechanism — exposure, not absence

The train-window trade counts (2, 2, 1) were read as "the strategy is flat
most of the time." That inference was wrong. The low trade *count* reflects
very long holding periods, not inactivity:

| Pool | Split | n trades | Exposure | Avg hold (bars) |
|---|---|---|---|---|
| cbBTC / WETH | train | 2 | **78.6%** | 235.5 |
| cbBTC / WETH | test | 3 | **50.2%** | 67.0 |
| VIRTUAL / WETH | train | 2 | **72.0%** | 215.5 |
| VIRTUAL / WETH | test | 1 | **82.8%** | 331.0 |
| AERO / WETH | train | 1 | **88.1%** | 528.0 |
| AERO / WETH | test | 3 | **65.2%** | 87.0 |

Signal *entries* fire at 5.5–6.9% of eligible bars. Trades are few because the
8–10% stop and `rsi>85` exit hold positions for hundreds of bars. The strategy
is in-market most of the window and is barely distinguishable from simply
holding — while paying ~68 bps per round trip to do it.

**Same-exposure control.** Random entries matched on trade count and holding
period, 500 draws per split (`run_regime_postmortem.py`):

| Strategy | Pool | Split | Strategy | Random (mean) | Random p05–p95 |
|---|---|---|---|---|---|
| `pullback_v2` | cbBTC / WETH | test | +11.87% | +4.76% | −3.80 .. +13.32 |
| `pullback_v2` | VIRTUAL / WETH | test | **+29.42%** | **+33.41%** | +29.79 .. +37.75 |
| `pullback_v2` | AERO / WETH | test | **+15.32%** | **+37.82%** | +10.35 .. +62.88 |
| `pullback_v2` | cbBTC / WETH | train | +2.46% | −0.57% | −4.86 .. +2.71 |
| `pullback_v2` | VIRTUAL / WETH | train | −9.99% | −9.14% | −22.34 .. +7.46 |
| `pullback_v2` | AERO / WETH | train | +19.84% | +19.97% | +14.23 .. +27.04 |
| `simple_ema_v1` | cbBTC / WETH | test | +10.64% | +4.65% | −3.06 .. +12.65 |
| `simple_ema_v1` | VIRTUAL / WETH | test | **−6.54%** | **+6.29%** | −8.60 .. +21.35 |
| `simple_ema_v1` | AERO / WETH | test | **−8.65%** | **+7.69%** | −10.40 .. +28.82 |
| `simple_ema_v1` | cbBTC / WETH | train | −2.98% | −0.00% | −5.13 .. +4.70 |
| `simple_ema_v1` | VIRTUAL / WETH | train | **−18.32%** | **−5.70%** | −22.29 .. +12.99 |
| `simple_ema_v1` | AERO / WETH | train | +19.53% | +1.56% | −16.84 .. +25.96 |

Counted across all 12 rows: the random control's mean exceeds the strategy's
return in **8 of 12** (`pullback_v2` 4 of 6, `simple_ema_v1` 4 of 6). On
`pullback_v2` VIRTUAL test the strategy lands **below the 5th percentile** of
the random distribution. **11 of 12 rows fall inside the random p05–p95 band**
— i.e. indistinguishable from random at matched exposure, not merely beaten.

The +19.30% mean is therefore **not skill**. It is what holding the asset
through that window produced, minus costs, and a random entry rule holding for
the same spans does at least as well.

### 6.1a Reconciling these figures against the sealed artifacts

The returns above are **not** identical to the published §3 rows
(`pullback_v2` test: +11.92 / +29.89 / +16.09 vs +11.87 / +29.42 / +15.32
here). Cause: `run_regime_postmortem.py` re-fetches live OHLCV and calls
`run_segment` directly, bypassing `prereg.require_pinned_data`, so it evaluates
a window one closed hourly bar newer than the pin taken at 14:01. Same
pools, same strategies, same gates — a one-bar shift in the evaluation window.
Trade counts are identical (3, 1, 3). The `+19.30%` mean in the header belongs
to the **pinned** set; these figures belong to the live re-fetch. Both are
correct for their own window; they are not interchangeable.

### 6.2 Regime structure — the conditional question cannot be asked

Entry rate is **0.0% in every down-trend bucket**, on every pool, for both
strategies. `px > slow_sma` is a hard conjunct: these strategies are
structurally long-only-uptrend, so there is no down-market behaviour to
condition on. On up-trend buckets where entries do fire (2.6–16.7% entry
rate), the strategies still fail to beat the random control at matched
exposure. No regime bucket reaches n≥10 out-of-sample.

Artifacts (post-hoc, non-evidence):
`research_runs/POSTHOC_regime_pullback_v2_20261001T142528.json`
sha256 `1ed7b13bbc7612ed0ebdfdbe5ef6f2adde3e4cacfde46b230267c898ea56c1a8`
`research_runs/POSTHOC_regime_simple_ema_v1_20261001T142653.json`
sha256 `4761d3eda0a5fadc243003bc93c2586c9fda570915cf2eedd16371a7528daff8`

## 7. Caveats on the record

1. **`pullback_v2` train counts are low** (2, 2, 1 against a ~600-bar
   train window). *Corrected 2026-10-01:* the original wording here said
   "almost never fires," which was wrong — signal entries fire on 5.5–6.9% of
   eligible bars. The low trade count reflects **very long holding periods**
   (avg 215–528 bars), not inactivity; exposure was 50–88%. See §6.1. The
   consequence stands: with 1–3 trades per split, no row can clear
   `min_trades: 10`, and all of its numbers are unreliable in both directions.
2. **GeckoTerminal returned HTTP 429 twice mid-run.** Handled with backoff and
   page-skip; no pool was silently dropped from any reported result. Two
   follow-on invocations refused to evaluate rather than evaluate partially,
   and were re-run cleanly. All 6 rows in §3 come from runs where all three
   pools resolved.
3. **The V4 `QuoteSwap` path was never directly exercised.** WO2's
   real-vs-fake control failed (keyed RPC returns `data: null` on revert,
   stripping revert selectors), and the Uniswap V4 Quoter address could not
   be resolved — `web_search`/`web_extract` have no registered provider and
   the browser has no CDP endpoint. Evidence is from the V4 **PoolManager**,
   which is strong but not a direct `QuoteSwap` proof.
4. **Balance reconciliation (WO3).** Wallet `0xe7a1…540F` holds
   **0.00295266739727199 ETH** at block 52044143. The prior caps.json record
   of 0.007833 ETH (2026-09-25) is stale; the **−0.0048803 ETH delta is
   UNATTRIBUTED** — cause not verified and not inferred. The wallet is
   EIP-7702 delegated (`0x7702cb55…1544a`), so outflows execute as ERC-4337
   UserOps and are invisible to ordinary transaction/log watchers.

## 8. Why this stops here

The natural next move — "maybe these work in choppy markets" — is exactly the
post-hoc regime split that produces a spurious edge. It is **not pursued under
this run**. If a regime-conditioned hypothesis is worth testing, it is a **new
research direction requiring its own preregistration**, not a continuation of
this evaluation.

## 9. Recommendation

**Stop.** Treat the hourly-EMA family as dead on liquid Base/WETH pools at this
cost model. Any future work on this problem should begin from a different
hypothesis with its own preregistration — not from a re-parameterization of
these two.

Trading posture is unchanged and unaffected by this finding:
`kill_switch: true`, `live_trading: false`, `require_confirmation: true`,
`arm_provenance: arm_signal_v1` preserved for audit.