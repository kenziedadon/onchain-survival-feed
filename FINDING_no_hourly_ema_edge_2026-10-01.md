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

`pullback_v2` reports `positive: 3/3, mean net +19.30%`. **This is not edge.**
The train-window trade counts were 2, 2, 1 — the strategy is flat most of the
time. With 1–3 test trades, holding cash through a rally produces a large
positive mean return while carrying none of the risk that produced it. Being
selective and being flat are indistinguishable at n=1.

The harness behaved correctly and refused to report a p-value:

```
t-test: NOT REPORTED (below min_trades — a p-value from a tiny sample is noise)
```

Any future reader must not cite `+19.30%` as a result.

## 7. Caveats on the record

1. **`pullback_v2` train counts are degenerate** (2, 2, 1 against a ~600-bar
   train window). The strategy almost never fires on hourly bars. This
   undermines confidence in all of its numbers, favourable or not, and is
   unexplained.
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