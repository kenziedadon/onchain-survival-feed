"""Walk-forward engine for WO4.

Owns everything a strategy must not: split logic, fill timing, sizing, costs,
accounting. A strategy only says "enter at bar i, exit at bar j".

Fill convention: a signal decided at bar i's close fills at bar i+1's OPEN.
A strategy cannot pick its own fill price, which closes the most common
backtest cheat (filling at the signal bar's close).

Split: positional, train_frac from the spec. The test window is never used
for any decision.
"""
from __future__ import annotations

import json
import os
import statistics
import time
from typing import NamedTuple

from . import costs as costs_mod
from . import prereg
from . import strategies
from .no_lookahead import (LookaheadViolation, proof_of_no_lookahead,
                           run_guarded)


class Trade(NamedTuple):
    entry_bar: int
    exit_bar: int
    entry_px: float
    exit_px: float
    gross_return: float
    cost_bps: float
    net_return: float
    reason: str


class SegmentResult(NamedTuple):
    net_pct: float
    trades: list
    bench_pct: float
    n_trades: int


def split_candles(candles, train_frac: float):
    """Positional split. The test window is untouched by every decision."""
    if not 0.1 <= train_frac <= 0.9:
        raise ValueError(f"train_frac must be in [0.1,0.9], got {train_frac}")
    k = int(len(candles) * train_frac)
    if k < 2 or len(candles) - k < 2:
        raise ValueError(
            f"split too small: {len(candles)} bars -> train {k}, test "
            f"{len(candles) - k}")
    return candles[:k], candles[k:]


def _bench_pct(candles, first: int, last: int):
    """Buy-and-hold over exactly the bars the strategy was in the market."""
    if first is None or last is None or first >= last:
        return None
    if first >= len(candles) or last >= len(candles):
        return None
    return (candles[last]["c"] / candles[first]["c"] - 1.0) * 100.0


def run_segment(candles, strat_mod, params: dict, fee_bps: float,
                liquidity_usd: float, notional_usd: float,
                scenario: str = "pessimistic") -> SegmentResult:
    """Run one segment under the causality guard.

    Returns a SegmentResult with a possibly EMPTY trade list. An empty list
    means NO_TRADES — it is NOT a zero return and NOT a loss. report.py is
    responsible for keeping that distinction visible.
    """
    candles = list(candles)
    min_hist = strat_mod.min_history_bars(params)

    try:
        signals_by_bar, n = run_guarded(
            strat_mod.generate_signals, candles, params, min_history=min_hist)
    except LookaheadViolation:
        # Abort the whole run; deliberately no partial result.
        raise

    cm = costs_mod.CostModel(fee_bps, liquidity_usd, notional_usd, scenario)
    rt_bps = cm.roundtrip_bps()
    cost_frac = rt_bps / 10000.0

    cash = 1.0
    trades = []
    entry_bar = entry_px = None
    first_entry = last_exit = None

    for bar in range(n):
        for sig in signals_by_bar.get(bar, []):
            if entry_bar is not None:
                continue                       # one position at a time
            fill_bar = sig.entry_bar + 1
            if fill_bar >= n:
                continue
            entry_bar, entry_px = fill_bar, candles[fill_bar]["o"]
            if first_entry is None:
                first_entry = fill_bar

        if entry_bar is not None:
            # Resolve the exit only once its bar is fully visible.
            seg = candles[entry_bar:bar + 1]
            if len(seg) >= 2:
                rel, _why = strat_mod.exit_rule(entry_px, seg, params)
                abs_exit = entry_bar + rel
                fill_exit = abs_exit + 1
                if fill_exit <= bar:
                    px = candles[fill_exit]["o"]
                    gross = px / entry_px - 1.0
                    net = gross - cost_frac
                    cash *= (1.0 + net)
                    trades.append(Trade(entry_bar, fill_exit, entry_px, px,
                                        gross * 100, rt_bps, net * 100, "closed"))
                    last_exit = fill_exit
                    entry_bar = entry_px = None

    # Force-close anything still open at the segment end.
    if entry_bar is not None and n - 1 > entry_bar:
        px = candles[n - 1]["c"]
        gross = px / entry_px - 1.0
        net = gross - cost_frac
        cash *= (1.0 + net)
        trades.append(Trade(entry_bar, n - 1, entry_px, px, gross * 100,
                            rt_bps, net * 100, "window_end"))
        last_exit = n - 1

    return SegmentResult(
        net_pct=(cash - 1.0) * 100.0,
        trades=trades,
        bench_pct=_bench_pct(candles, first_entry, last_exit),
        n_trades=len(trades),
    )


def welch_t(a: list, b: list, one_sided: bool = False) -> dict:
    """Welch's two-sample t-test, equal-variance NOT assumed.

    Implemented here rather than pulled from scipy so the harness runs on a
    stock interpreter. Returns effect size and CI as well as p — a p-value
    from a small sample is the statistic that misleads most often.

    `one_sided` tests a>b and halves the tail area. WO4's hypothesis is
    directional ("strategy beats benchmark"), so the registered spec asks for
    the one-sided form; a two-sided p can never be silently substituted
    because report.py passes the spec's own analysis.test through, and
    preregistration refuses any test name outside ALLOWED_TESTS.
    """
    name = "welch_t_one_sided" if one_sided else "welch_t"
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return {"test": name, "one_sided": one_sided,
                "insufficient": True, "n_a": na, "n_b": nb}
    ma, mb = statistics.fmean(a), statistics.fmean(b)
    va, vb = statistics.variance(a), statistics.variance(b)
    se2 = va / na + vb / nb
    if se2 <= 0:
        return {"test": name, "one_sided": one_sided,
                "insufficient": True, "n_a": na, "n_b": nb}
    t = (ma - mb) / (se2 ** 0.5)
    df = (se2 ** 2) / ((va / na) ** 2 / (na - 1) + (vb / nb) ** 2 / (nb - 1))
    # p from the normal approximation to t. Adequate for n>=10, and the report
    # refuses to compute a p at all below min_trades_test.
    from math import erfc, sqrt
    if one_sided:
        # P(T > t). A negative t in the one-sided test means the observed
        # effect points the wrong way; that is p=1.0, not p near 0.5, and it
        # must never read as significance.
        p = 0.5 * erfc(t / sqrt(2)) if t > 0 else 1.0
        tail = "one-sided: P(strategy > benchmark)"
    else:
        p = erfc(abs(t) / sqrt(2))
        tail = "two-sided: P(strategy != benchmark)"
    se = sqrt(se2)
    return {"test": name, "one_sided": one_sided,
            "n_a": na, "n_b": nb, "t": t, "df": df,
            "p_approx": p, "mean_a": ma, "mean_b": mb,
            "diff": ma - mb, "ci95": [ma - mb - 1.96 * se, ma - mb + 1.96 * se],
            "tail": tail,
            "note": "normal approximation to t; see df"}


def run(candles, spec: dict, pool: dict,
        infrastructure_validation: bool = False) -> dict:
    """Full walk-forward run. Produces the artifact dict; does not write."""
    strat_mod = strategies.get(spec["strategy_id"])

    # Provenance and refusal gate, BEFORE any data is touched.
    if infrastructure_validation:
        spec_h = prereg.spec_hash(spec)
        prov = {"spec_hash": spec_h, "spec_short": prereg.short(spec_h),
                "strategy_hash": prereg.strategy_hash(spec["strategy_id"]),
                "registered_before_run": False,
                "infrastructure_validation": True,
                "preregistered": False,
                "NOTE": "harness validation only; not strategy evidence"}
    else:
        spec_h = prereg.require_registered(spec)
        prov = prereg.provenance(spec, spec_h, False)

    # Data refusal gate. Runs AFTER the spec gate and BEFORE any computation:
    # a spec that is registered but unpinned, or whose upstream window has
    # rolled since the pin, must not produce an artifact at all. Skipped for
    # infrastructure_validation, which is by definition not evidence.
    if infrastructure_validation:
        prov["data_pin"] = {
            "pinned": False,
            "why": "infrastructure validation run — not citable, data not pinned",
            "data_hash_observed": prereg.data_hash(candles,
                                                   pool.get("address", "")),
            "window": prereg.data_window(candles),
        }
    else:
        pin = prereg.require_pinned_data(
            spec_h, candles, pool.get("address", ""))
        prov["data_pin"] = {
            "pinned": True,
            "data_hash": pin["data_hash"],
            "data_hash_short": prereg.short(pin["data_hash"]),
            "window": pin["window"],
            "pool": pin["pool"],
            "pinned_at": pin["pinned_at"],
        }

    train_frac = float(spec["split"]["train_frac"])
    tr, te = split_candles(candles, train_frac)
    scenario = spec["costs"].get("model", "pessimistic")

    def seg(cs):
        return run_segment(cs, strat_mod, spec["params"], pool["fee_bps"],
                           pool["liquidity_usd"], pool["notional_usd"], scenario)

    r_tr, r_te = seg(tr), seg(te)

    out = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "strategy_id": spec["strategy_id"],
        "strategy_version": strat_mod.STRATEGY_VERSION,
        "pool": pool,
        "spec": spec,
        "provenance": prov,
        "cost_constants": costs_mod.constants_document(),
        "train": {"net_pct": r_tr.net_pct, "n_trades": r_tr.n_trades,
                  "bench_pct": r_tr.bench_pct},
        "test": {"net_pct": r_te.net_pct, "n_trades": r_te.n_trades,
                 "bench_pct": r_te.bench_pct},
        "trades": [t._asdict() for t in r_te.trades],
    }
    out["guard"] = proof_of_no_lookahead({}, len(candles))
    return out
