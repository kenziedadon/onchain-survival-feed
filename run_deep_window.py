#!/usr/bin/env python3
"""WO-followup (a): DEEPEN THE DATA WINDOW on the already-pinned 3 pools.

Read-only research. Submits nothing, touches no key, edits no caps.json.

Why this exists: every preregistered walkforward to date fetched 300 hourly
bars (~12.5 days) and produced n=1..3 trades per pool. The sealed gate needs
min_trades=10 for the t-test. The binding constraint is BARS, not universe
size — widening the token list buys more insufficient-sample rejections, not
statistical power.

This deepens the window to 1000 hourly bars (~41.5 days, the full depth
GeckoTerminal serves for these pools) on the SAME 3 pools, under a NEW
spec_hash. Gates, min_trades, pf_min, alpha, costs and params are copied
from the previously registered spec verbatim — nothing is relaxed. If the
deeper window still cannot clear n=10, that is the honest finding and the
answer is option (c), not a lower threshold.

Usage:
    python3 run_deep_window.py --strategy simple_ema_v1 --register --pin-only
    python3 run_deep_window.py --strategy simple_ema_v1
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from research_harness import harness, prereg, report, strategies
from run_walkforward import _get, fetch_ohlcv

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "research_runs")

#: Exactly the pools already pinned under spec 1f0274fa6629 — the universe is
#: NOT widened. Same three pools, deeper window. Names are resolved once at
#: module load from the live pool list because costs.pool_fee_bps() refuses to
#: guess a fee from a bare address, and guessing a fee would silently corrupt
#: every net-return figure in the report.
PINNED_POOLS = [
    "0x7c7420dd105e2779316423ba3e973f434315efa9",
    "0x3f0296bf652e19bca772ec3df08b32732f93014a",
    "0x20cb8f872ae894f7c9e32e621c186e5afce82fd0",
]


def resolve_pool_names(addresses: list) -> dict:
    """address -> (pool label, liquidity_usd), via the live pool list.

    Fee tiers differ per pool (0.009% vs 0.05%) and the cost model reads them
    off the label, so the labels are data, not decoration.
    """
    want = {a.lower() for a in addresses}
    out: dict = {}
    for page in range(1, 6):
        if len(out) == len(want):
            break
        # GeckoTerminal rate-limits hard (429), and _get's retry budget is
        # finite. A rate-limited page must not abort the whole resolution --
        # the pinned pools are large and sit on page 1, so a later 429 is not
        # evidence they are absent. Skip and keep going; the caller refuses
        # only if something is still unresolved at the end.
        try:
            data = json.loads(_get(
                f"https://api.geckoterminal.com/api/v2/networks/base/pools?page={page}"
            )).get("data", [])
        except Exception as ex:
            print(f"  [pool-list] page {page} skipped: {ex}")
            time.sleep(4.0)
            continue
        for p in data:
            # GeckoTerminal returns EIP-55 checksummed ids; the pinned addresses
            # are lowercase. Compare case-insensitively or every lookup misses
            # and the run refuses for a reason that has nothing to do with data.
            addr = p.get("id", "").split("_")[-1].lower()
            if addr in want:
                a = p.get("attributes", {})
                out[addr] = (a.get("name") or "", float(a.get("reserve_in_usd") or 0))
        time.sleep(2.0)
    return out


def build_deep_spec(strategy_id: str, limit: int) -> dict:
    """Same experiment as build_spec() in run_walkforward, deeper window.

    Every gate is copied from the module's own DEFAULTS-driven spec so the
    ONLY difference from spec 1f0274fa6629 / dc4f65468c22 is data.limit.
    """
    mod = strategies.get(strategy_id)
    return {
        "strategy_id": strategy_id,
        "strategy_version": mod.STRATEGY_VERSION,
        "params": dict(mod.DEFAULTS),
        "data": {"source": "geckoterminal", "interval": "hour",
                 "limit": limit,
                 "universe": "base_weth_pools_largest_first",
                 "window_note": "deepened 300->1000 hourly bars on the 3 "
                                "already-pinned pools; universe unchanged"},
        "split": {"train_frac": 0.6, "scheme": "positional"},
        "costs": {"model": "pessimistic", "slippage_multiplier": 2.0,
                  "constants_version": "pessimistic_v1"},
        "gates": {"min_trades_test": 10, "pf_min": 1.1, "maxdd_max": 40.0},
        "analysis": {"test": "welch_t_one_sided", "alpha": 0.05,
                     "min_trades": 10},
    }


_NAMES: dict = {}


def pool_meta(addr: str) -> dict:
    """Pool metadata for the cost model. Requires a resolved label.

    costs.pool_fee_bps() raises rather than guessing a fee, which is the right
    default — an invented fee would quietly change every net-return number in
    the report. So we resolve real labels once and fail loudly if one is
    missing, instead of inventing "0.05%" and reporting fiction.
    """
    from research_harness import costs
    label, liq = _NAMES.get(addr.lower(), ("", 0.0))
    return {"name": label, "address": addr,
            "fee_bps": costs.pool_fee_bps(label),
            "liquidity_usd": liq, "notional_usd": 100.0}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="simple_ema_v1")
    ap.add_argument("--limit-candles", type=int, default=1000)
    ap.add_argument("--register", action="store_true")
    ap.add_argument("--pin-only", action="store_true")
    ap.add_argument("--who", default="hermes")
    args = ap.parse_args()

    spec = build_deep_spec(args.strategy, args.limit_candles)
    h = prereg.spec_hash(spec)

    if args.register:
        e = prereg.register(spec, who=args.who,
                            note=f"deep-window (a): {args.limit_candles} bars, "
                                 "3 pinned pools, gates unchanged")
        print(f"registered {e['short']} status={e['status']}")
        print(f"spec_hash {h}\n")
    else:
        print("MODE: preregistered deep-window run (refusal gate enforced)")
        print(f"spec_hash {h}\n")

    print(f"window: {args.limit_candles} hourly bars, universe = "
          f"{len(PINNED_POOLS)} already-pinned pools (NOT widened)\n")

    global _NAMES
    _NAMES = resolve_pool_names(PINNED_POOLS)
    for a in PINNED_POOLS:
        nm, liq = _NAMES.get(a.lower(), ("", 0.0))
        print(f"  pool {a[:12]} {nm or 'UNRESOLVED'}  liq=${liq:,.0f}")
    missing = [a for a in PINNED_POOLS if a.lower() not in _NAMES]
    if missing:
        print(f"\nREFUSING: unresolved pool labels {missing}. The cost model "
              f"reads fee tiers off the label; guessing one would corrupt every "
              f"net-return figure. Re-run when the pool list serves them.")
        return
    print()

    if args.register:
        pinned = 0
        for addr in PINNED_POOLS:
            try:
                cs = fetch_ohlcv(addr, args.limit_candles)
            except Exception as ex:
                print(f"  [pin-skip] {addr[:12]} fetch failed: {ex}")
                continue
            if len(cs) < 120:
                print(f"  [pin-skip] {addr[:12]} only {len(cs)} candles")
                continue
            time.sleep(1.2)
            try:
                pe = prereg.pin_data(spec, h, cs, addr, who=args.who)
            except prereg.PreregError as ex:
                print(f"  [pin-refused] {addr[:12]} {ex}")
                continue
            w = pe["window"]
            print(f"  [pin] {addr[:12]} {pe['status']:15} bars={w['bars']} "
                  f"[{w['start_ts']}..{w['end_ts']}]")
            pinned += 1
        print(f"\npinned {pinned}/{len(PINNED_POOLS)} pool(s)")
        if args.pin_only:
            print("--pin-only: exiting without evaluation.")
            return

    results = []
    for addr in PINNED_POOLS:
        try:
            candles = fetch_ohlcv(addr, args.limit_candles)
        except Exception as ex:
            print(f"  [skip] {addr[:12]} fetch failed: {ex}")
            continue
        if len(candles) < 120:
            print(f"  [skip] {addr[:12]} only {len(candles)} candles")
            continue
        time.sleep(1.2)
        try:
            out = harness.run(candles, spec, pool_meta(addr),
                              infrastructure_validation=False)
        except prereg.PreregError as ex:
            print(f"  [REFUSED] {addr[:12]} {ex}")
            break
        results.append(out)
        t = out["test"]
        bh = f"{t['bench_pct']:+7.1f}%" if t["bench_pct"] is not None else "    n/a"
        print(f"  {addr[:12]} bars={len(candles)} test={t['net_pct']:+7.2f}% "
              f"BH={bh} trades={t['n_trades']} (train {out['train']['n_trades']})")

    if not results:
        print("\nNo pools evaluated — nothing to report.")
        return

    rep = report.build_report(results, spec["gates"], spec["analysis"])
    rep["guard"] = results[0]["guard"]
    rep["provenance"] = results[0]["provenance"]
    rep["cost_constants"] = results[0]["cost_constants"]
    print()
    print(report.render_text(rep))

    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR,
                        f"wf_deep{args.limit_candles}_{args.strategy}_"
                        f"{time.strftime('%Y%m%dT%H%M%S')}.json")
    report.write_report(rep, path)
    print(f"\nwrote {path}")


if __name__ == "__main__":
    main()