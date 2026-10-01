#!/usr/bin/env python3
"""POST-HOC REGIME DIAGNOSTIC — NOT EVIDENCE.

McKenzie authorized this on 2026-10-01 as option (a) of an explicit three-way
choice, on the understanding that it CANNOT re-adjudicate the sealed ledger.

READ THIS BEFORE CITING ANY NUMBER FROM THIS SCRIPT.

  * The sealed ledger stays 0/6. Nothing here changes it, and nothing here can.
  * The regime buckets, the exposure framing and the same-exposure benchmark
    were all chosen AFTER seeing which rows won. That is a post-hoc split.
  * Consequently EVERY output here is descriptive, not confirmatory. There is
    no p-value in this file and none may be computed from one.
  * If a regime-conditional edge appears here, it is a HYPOTHESIS for a fresh
    preregistration on fresh data. It is not a result, and the 0/6 stands.

Why it exists at all: `pullback_v2` reported +19.30% mean net with test-split
trade counts of 3, 1, 3 while the train split showed 2, 2, 1. The first guess
was that the strategy was flat ~99% of the time and merely caught a rally.
THAT WAS WRONG, and this script disproved it: exposure is 50-88%, not ~1%,
because the 8-10% stop holds positions for hundreds of bars. The low trade
COUNT reflects very long holding periods, not inactivity. The corrected
reading is that the return is exposure, and the same-exposure random control
below does at least as well. See FINDING section 6.1.

Note also that this script does NOT replay the pinned bars -- it re-fetches
live OHLCV and calls run_segment directly, bypassing
prereg.require_pinned_data. That is deliberate (a post-hoc diagnostic must not
be blocked by the refusal gate), but it means its figures can differ slightly
from the sealed artifacts, whose bars were pinned hours earlier and have since
been superseded by a newly closed hourly bar. Compare same-split net_pct
before quoting either number.

What this measures, per pool and split:
  1. entries / exits / signal-candidate counts, and WHY entries were rejected
  2. exposure: fraction of bars in-market, avg holding period
  3. strategy return vs buy-and-hold vs "buy-and-hold over the flat windows only"
     (i.e. what the asset did while the strategy was sitting in cash)
  4. regime buckets: trend up/down/range, volatility tercile
  5. same-exposure control: random entries with the same cash fraction and
     same trade count, averaged over many draws

Submits nothing, reads no key, touches no caps.json, writes one JSON artifact.
"""
from __future__ import annotations

import json
import math
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from research_harness import costs as costs_mod, harness, no_lookahead, prereg, strategies
from run_walkforward import _get, fetch_ohlcv
from run_deep_window import PINNED_POOLS, build_deep_spec, resolve_pool_names

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "research_runs")

NAMES: dict = {}


def pool_meta(addr: str) -> dict:
    label, liq = NAMES.get(addr.lower(), ("", 0.0))
    return {"name": label, "address": addr,
            "fee_bps": costs_mod.pool_fee_bps(label),
            "liquidity_usd": liq, "notional_usd": 100.0}


# ---------------------------------------------------------------------------
# 1. Signal-candidate accounting — which conjunct is actually binding?
# ---------------------------------------------------------------------------

def signal_audit(candles, strat_mod, params) -> dict:
    """Replay the entry predicate bar by bar and count each rejection reason.

    `generate_signals` returns [] for every miss, which hides WHY. Counting
    the conjuncts independently is the only way to tell "the filter is too
    tight" apart from "the market genuinely never qualified".
    """
    p = dict(strat_mod.DEFAULTS)
    p.update(params or {})
    min_hist = strat_mod.min_history_bars(p)
    n = len(candles)
    has_sma = "fast" in p and "slow" in p
    has_rsi = "rsi_lo" in p and "rsi_hi" in p
    has_roc = "roc_lo" in p and "roc_hi" in p
    has_roc24 = "roc_24_min" in p

    counts = {"bars_eligible": 0, "signals_emitted": 0}
    fails: dict = {}
    if has_sma:
        fails["px_vs_slow_sma"] = 0
        fails["px_vs_fast_sma"] = 0
    if has_rsi:
        fails["rsi"] = 0
    if has_roc:
        fails["roc_fast"] = 0
    if has_roc24:
        fails["roc_24h"] = 0
    # Conjunct pass counts: how many bars cleared each individual condition.
    pass_counts = {k: 0 for k in fails}
    for i in range(min_hist, n):
        # VisibleWindow wraps CANDLES (not closes) and derives .closes itself;
        # it physically truncates to bar i, so this cannot see the future.
        w = no_lookahead.VisibleWindow(candles, bar=i)
        c = w.closes
        px = c[-1]
        sf = sum(c[-int(p["fast"]):]) / int(p["fast"])
        ss = sum(c[-int(p["slow"]):]) / int(p["slow"])
        r = strat_mod._rsi(c)
        roc_f = c[-1] / c[-int(p["fast"])] - 1.0 if len(c) > int(p["fast"]) else 0.0
        roc24 = c[-1] / c[-24] - 1.0 if len(c) > 24 else 0.0

        # Only the conjuncts THIS strategy actually applies. simple_ema_v1 has
        # no rsi_lo/roc_lo, so blindly testing every key would either KeyError or
        # report conditions the strategy never used -- an audit of a predicate
        # the strategy does not evaluate is not an audit of the strategy.
        conds = {}
        if has_sma:
            conds["px_vs_slow_sma"] = px > ss
            conds["px_vs_fast_sma"] = px < sf
        if has_rsi:
            conds["rsi"] = p["rsi_lo"] < r < p["rsi_hi"]
        if has_roc:
            conds["roc_fast"] = p["roc_lo"] < roc_f < p["roc_hi"]
        if has_roc24:
            conds["roc_24h"] = roc24 > p["roc_24_min"]

        counts["bars_eligible"] += 1
        for k, ok in conds.items():
            if ok:
                pass_counts[k] += 1
            else:
                fails[k] += 1
        if all(conds.values()):
            counts["signals_emitted"] += 1

    counts["conjunct_pass_rate_pct"] = round(
        100.0 * counts["signals_emitted"] / max(counts["bars_eligible"], 1), 4)
    counts["per_condition_pass_rate_pct"] = {
        k: round(100.0 * pass_counts[k] / max(counts["bars_eligible"], 1), 2)
        for k in pass_counts}
    counts["per_condition_fail_count"] = fails
    return counts


# ---------------------------------------------------------------------------
# 2. Exposure
# ---------------------------------------------------------------------------

def exposure(candles, trades) -> dict:
    """Fraction of bars actually in-market, and mean holding period."""
    n = len(candles)
    if not trades:
        return {"bars_in_market": 0, "exposure_pct": 0.0,
                "avg_hold_bars": None, "n_trades": 0}
    bars = 0
    holds = []
    for t in trades:
        hold = max(1, t.exit_bar - t.entry_bar)
        holds.append(hold)
        bars += min(hold, n - t.entry_bar)
    return {"bars_in_market": bars,
            "exposure_pct": round(100.0 * bars / n, 3),
            "avg_hold_bars": round(sum(holds) / len(holds), 2),
            "n_trades": len(trades)}


# ---------------------------------------------------------------------------
# 3. Benchmark decomposition — what did the asset do while we sat in cash?
# ---------------------------------------------------------------------------

def bench_decomposition(candles, trades) -> dict:
    """Full-window B&H, plus B&H restricted to the bars the strategy was FLAT.

    The second number answers a question that the full-window figure hides:
    what did the asset do during the stretches this strategy sat out? If most
    of the move happened there, the strategy avoided it rather than earned it.

    Note the corrected reading (FINDING section 6.1): for `pullback_v2` the
    flat windows are a MINORITY of bars (exposure 50-88%), so this is not the
    "cash caught the rally" story. It is a diagnostic of where the asset's
    return came from, nothing more.
    """
    n = len(candles)
    if n < 2:
        return {"bh_full_pct": None, "bh_flat_windows_pct": None,
                "flat_bars": 0}
    bh_full = (candles[-1]["c"] / candles[0]["c"] - 1.0) * 100.0

    in_market = [False] * n
    for t in trades:
        for b in range(t.entry_bar, min(t.exit_bar, n)):
            in_market[b] = True

    # Compound the asset across each contiguous flat stretch.
    cash, started, prev = 1.0, False, None
    flat_bars = 0
    for b in range(n):
        if not in_market[b]:
            flat_bars += 1
            if not started:
                prev = candles[b]["c"]
                started = True
            else:
                cash *= candles[b]["c"] / prev
                prev = candles[b]["c"]
        else:
            started, prev = False, None
    return {"bh_full_pct": round(bh_full, 4),
            "bh_flat_windows_pct": round((cash - 1.0) * 100.0, 4),
            "flat_bars": flat_bars,
            "flat_fraction_pct": round(100.0 * flat_bars / n, 3)}


# ---------------------------------------------------------------------------
# 4. Regime buckets
# ---------------------------------------------------------------------------

def _sma(v, n):
    return sum(v[-n:]) / n if len(v) >= n else None


def regime_buckets(candles, strat_mod, params, trades) -> list:
    """Bucket eligible bars into trend x volatility, report entry frequency.

    Deliberately descriptive: these buckets were defined after seeing the
    results, so a bucket that looks good here is a hypothesis and nothing more.
    """
    p = dict(strat_mod.DEFAULTS)
    p.update(params or {})
    min_hist = strat_mod.min_history_bars(p)
    fast, slow = int(p["fast"]), int(p["slow"])

    buckets: dict = {}
    in_market = set()
    for t in trades:
        for b in range(t.entry_bar, min(t.exit_bar, len(candles))):
            in_market.add(b)

    for i in range(min_hist, len(candles)):
        c = [x["c"] for x in candles[: i + 1]]
        px = c[-1]
        sf, ss = _sma(c, fast), _sma(c, slow)
        if sf is None or ss is None:
            continue
        trend = "up" if px > ss else ("down" if px < ss else "flat")
        rets = [c[k] / c[k - 1] - 1.0 for k in range(max(1, i - 23), i + 1)]
        vol = (sum((x - sum(rets) / len(rets)) ** 2 for x in rets) / len(rets)) ** 0.5
        key = (trend, "hi_vol" if vol > 0.02 else "lo_vol")
        b = buckets.setdefault(key, {"bars": 0, "entries": 0, "in_market": 0})
        b["bars"] += 1
        if i in in_market:
            b["in_market"] += 1
        w = no_lookahead.VisibleWindow(candles, bar=i)
        if strat_mod.generate_signals(w, params):
            b["entries"] += 1

    out = []
    for (trend, vol), b in sorted(buckets.items()):
        out.append({"trend": trend, "vol": vol, **b,
                    "entry_rate_pct": round(100.0 * b["entries"] / max(b["bars"], 1), 4),
                    "in_market_pct": round(100.0 * b["in_market"] / max(b["bars"], 1), 3)})
    return out


# ---------------------------------------------------------------------------
# 5. Same-exposure control
# ---------------------------------------------------------------------------

def same_exposure_control(candles, trades, fee_bps, liquidity_usd,
                          notional_usd, scenario, draws=500, seed=20261001) -> dict:
    """Random entries matched on trade count and exposure.

    The question this answers: if a DIFFERENT strategy had been flat exactly as
    much, held for exactly as long, and entered at random, would it have made
    the same money? If yes, the strategy's return is an artifact of exposure,
    not of its entry rule.
    """
    n = len(candles)
    if not trades or n < 50:
        return {"draws": 0, "note": "no trades to match"}

    cm = costs_mod.CostModel(fee_bps, liquidity_usd, notional_usd, scenario)
    cost_frac = cm.roundtrip_bps() / 10000.0
    holds = [max(1, t.exit_bar - t.entry_bar) for t in trades]
    k = len(holds)
    rng = random.Random(seed)

    rets = []
    for _ in range(draws):
        cash = 1.0
        used, guard = set(), 0
        for h in holds:
            # Place each synthetic trade in a non-overlapping window, so the
            # control matches exposure rather than accidentally overlapping.
            placed = False
            for _try in range(40):
                start = rng.randint(1, max(1, n - h - 2))
                span = set(range(start, start + h + 1))
                if not (span & used):
                    used |= span
                    cash *= 1.0 + ((candles[start + h]["o"] / candles[start]["o"] - 1.0)
                                   - cost_frac)
                    placed = True
                    break
            if placed:
                guard += 1
        if guard:
            rets.append((cash - 1.0) * 100.0)

    if not rets:
        return {"draws": 0, "note": "control could not place trades"}
    rets.sort()
    return {"draws": len(rets), "matched_trade_count": k,
            "mean_pct": round(sum(rets) / len(rets), 4),
            "median_pct": round(rets[len(rets) // 2], 4),
            "p05_pct": round(rets[int(0.05 * len(rets))], 4),
            "p95_pct": round(rets[int(0.95 * len(rets))], 4)}


# ---------------------------------------------------------------------------

def main() -> None:
    strategy_id = sys.argv[1] if len(sys.argv) > 1 else "pullback_v2"
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 1000

    global NAMES
    NAMES = resolve_pool_names(PINNED_POOLS)
    missing = [a for a in PINNED_POOLS if a.lower() not in NAMES]
    if missing:
        print(f"REFUSING: unresolved pool labels {missing} — cannot price fees.")
        return

    spec = build_deep_spec(strategy_id, limit)
    strat_mod = strategies.get(strategy_id)
    print(f"POST-HOC DIAGNOSTIC (NOT EVIDENCE) — {strategy_id}, {limit} bars")
    print(f"spec under discussion: {prereg.spec_hash(spec)[:12]}\n")

    artifact = {
        "STAMP": "POST_HOC / NOT_EVIDENCE",
        "warning": ("Regime buckets, exposure framing and the same-exposure "
                    "control were all chosen AFTER seeing results. Nothing in "
                    "this file is confirmatory. The sealed ledger remains 0/6 "
                    "and cannot be changed by this run. No p-value may be "
                    "derived from these numbers."),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "strategy_id": strategy_id,
        "strategy_version": strat_mod.STRATEGY_VERSION,
        "window_bars": limit,
        "gates_unchanged": spec["gates"],
        "analysis_unchanged": spec["analysis"],
        "spec_hash_discussed": prereg.spec_hash(spec),
        "pools": [],
    }

    for addr in PINNED_POOLS:
        pm = pool_meta(addr)
        candles = fetch_ohlcv(addr, limit)
        if len(candles) < 120:
            print(f"  [skip] {pm['name'][:22]} only {len(candles)} bars")
            continue
        time.sleep(2.0)

        tr, te = harness.split_candles(candles,
                                       float(spec["split"]["train_frac"]))
        pool_entry = {"pool": pm["name"], "address": addr,
                      "fee_bps": pm["fee_bps"], "bars": len(candles),
                      "splits": {}}

        for label, seg in (("train", tr), ("test", te)):
            r = harness.run_segment(seg, strat_mod, spec["params"],
                                    pm["fee_bps"], pm["liquidity_usd"],
                                    pm["notional_usd"],
                                    spec["costs"]["model"])
            au = signal_audit(seg, strat_mod, spec["params"])
            ex = exposure(seg, r.trades)
            bd = bench_decomposition(seg, r.trades)
            sc = same_exposure_control(
                seg, r.trades, pm["fee_bps"], pm["liquidity_usd"],
                pm["notional_usd"], spec["costs"]["model"])
            rb = regime_buckets(seg, strat_mod, spec["params"], r.trades)
            pool_entry["splits"][label] = {
                "bars": len(seg),
                "strategy_net_pct": round(r.net_pct, 4),
                "n_trades": r.n_trades,
                "signal_audit": au,
                "exposure": ex,
                "benchmarks": bd,
                "same_exposure_control": sc,
                "regime_buckets": rb,
                "trade_detail": [t._asdict() for t in r.trades],
            }

            print(f"  {pm['name'][:20]:20} {label:5} n={r.n_trades} "
                  f"net={r.net_pct:+8.2f}%  exposure={ex['exposure_pct']:5.1f}% "
                  f"BH_full={bd['bh_full_pct']:+7.2f}% "
                  f"BH_flat={bd['bh_flat_windows_pct']:+7.2f}% "
                  f"rand_same_exposure={sc.get('mean_pct', float('nan')):+7.2f}%")
            print(f"      signal rate {au['conjunct_pass_rate_pct']}% of "
                  f"{au['bars_eligible']} eligible bars; per-condition pass: "
                  f"{au['per_condition_pass_rate_pct']}")

        artifact["pools"].append(pool_entry)

    artifact["verdict"] = {
        "sealed_ledger": "0/6 — UNCHANGED by this run",
        "is_evidence": False,
        "next_step": ("If a regime-conditional edge is visible here, it is a "
                      "hypothesis for a NEW preregistration on fresh data. It "
                      "does not reopen this evaluation."),
    }

    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(
        OUT_DIR,
        f"POSTHOC_regime_{strategy_id}_{time.strftime('%Y%m%dT%H%M%S')}.json")
    with open(path, "w") as fh:
        json.dump(artifact, fh, indent=2, sort_keys=True)
    print(f"\nSTAMP: POST_HOC / NOT_EVIDENCE — sealed ledger still 0/6")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
