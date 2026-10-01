#!/usr/bin/env python3
"""WO4 harness runner — network-touching walk-forward over live pools.

PAPER ONLY. Nothing here submits a transaction, reads a signing key, or
touches caps.json. It fetches public OHLCV and writes JSON.

Usage:
    python3 run_walkforward.py --infrastructure-validation
    python3 run_walkforward.py --strategy simple_ema_v1 --register --who hermes
    python3 run_walkforward.py --strategy simple_ema_v1

The first form runs WITHOUT preregistration and stamps the artifact
infrastructure_validation=true, so it can never be cited as strategy evidence.
The second registers the spec first, then runs under the refusal gate.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from research_harness import costs, harness, prereg, report, strategies

_UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "research_runs")


def _get(url, timeout=25, retries=3):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=_UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode()
        except Exception:
            if i < retries - 1:
                time.sleep(1.5)
                continue
            raise


def fetch_ohlcv(pool_addr, limit=300, drop_incomplete=True):
    """Fetch hourly OHLCV, newest last.

    `drop_incomplete` removes the trailing bar when it is still forming.
    GeckoTerminal returns the CURRENT hour as a live, partially-filled candle
    whose close/volume keep moving until the hour closes. Two fetches minutes
    apart therefore return different values for the same timestamp.

    That is fatal for data pinning: a pin taken mid-hour can never be
    reproduced, because the bar it contains is still moving. The run refuses
    with DATA PIN MISMATCH an hour later for a reason that has nothing to do
    with the experiment changing. Only closed bars are pinnable, and an
    in-progress candle is not data anyway — the strategy would be acting on a
    close that is not the close.
    """
    url = (f"https://api.geckoterminal.com/api/v2/networks/base/pools/"
           f"{pool_addr}/ohlcv/hour?aggregate=1&limit={limit}")
    rows = json.loads(_get(url)).get("data", {}).get("attributes", {}) \
                                  .get("ohlcv_list", [])
    out = sorted(({"ts": r[0], "o": float(r[1]), "h": float(r[2]),
                   "l": float(r[3]), "c": float(r[4]), "v": float(r[5])}
                  for r in rows), key=lambda x: x["ts"])
    if drop_incomplete and out:
        current_hour = int(time.time()) // 3600 * 3600
        out = [c for c in out if c["ts"] < current_hour]
    return out


def base_weth_pools(limit=8):
    """WETH-quoted Base pools, largest first. Deliberately not 'trending' —
    selecting by current performance is itself a lookahead in the universe
    choice."""
    url = "https://api.geckoterminal.com/api/v2/networks/base/pools?page=1"
    data = json.loads(_get(url)).get("data", [])
    out = []
    for p in data:
        a = p.get("attributes", {})
        nm = a.get("name", "")
        if "WETH" not in nm.upper():
            continue
        if any(s in nm.upper() for s in ("USDC", "USDT", "DAI")):
            continue
        liq = float(a.get("reserve_in_usd") or 0)
        if liq < 100_000:
            continue
        out.append({"name": nm, "address": p["id"].split("_")[-1],
                    "liquidity_usd": liq,
                    "fee_bps": costs.pool_fee_bps(nm)})
    out.sort(key=lambda x: -x["liquidity_usd"])
    return out[:limit]


def build_spec(strategy_id):
    mod = strategies.get(strategy_id)
    return {
        "strategy_id": strategy_id,
        "strategy_version": mod.STRATEGY_VERSION,
        "params": dict(mod.DEFAULTS),
        "data": {"source": "geckoterminal", "interval": "hour", "limit": 300,
                 "universe": "base_weth_pools_largest_first"},
        "split": {"train_frac": 0.6, "scheme": "positional"},
        "costs": {"model": "pessimistic", "slippage_multiplier": 2.0,
                  "constants_version": "pessimistic_v1"},
        "gates": {"min_trades_test": 10, "pf_min": 1.1, "maxdd_max": 40.0},
        "analysis": {"test": "welch_t_one_sided", "alpha": 0.05, "min_trades": 10},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="simple_ema_v1")
    ap.add_argument("--pools", type=int, default=6)
    ap.add_argument("--register", action="store_true")
    ap.add_argument("--who", default="hermes")
    ap.add_argument("--infrastructure-validation", action="store_true")
    ap.add_argument("--limit-candles", type=int, default=300)
    ap.add_argument("--pin-only", action="store_true",
                    help="register the spec AND pin the fetched bars, then "
                         "exit without evaluating. This is the only window in "
                         "which a preregistered run is permitted to execute.")
    args = ap.parse_args()

    spec = build_spec(args.strategy)
    infra = args.infrastructure_validation

    if infra:
        print("MODE: infrastructure validation (NOT strategy evidence)")
        print("  preregistration is not enforced for this run by design.\n")
    elif args.register:
        e = prereg.register(spec, who=args.who,
                            note="registered before first preregistered run")
        print(f"registered spec {e['short']} status={e['status']}\n")
    else:
        print("MODE: preregistered run (refusal gate enforced)\n")

    pools = base_weth_pools(args.pools)
    print(f"universe: {len(pools)} WETH pools\n")

    # --- pin phase -------------------------------------------------------
    # Registration and data pinning happen in the SAME invocation, before any
    # evaluation, so the window a spec is judged on is fixed before anyone can
    # see a result. Every later preregistered run must reproduce these exact
    # bars or the data gate refuses it.
    if args.register and not infra:
        spec_h = prereg.spec_hash(spec)
        pinned = 0
        for p in pools:
            try:
                cs = fetch_ohlcv(p["address"], args.limit_candles)
            except Exception as ex:
                print(f"  [pin-skip] {p['name'][:22]:22} fetch failed: {ex}")
                continue
            if len(cs) < 120:
                print(f"  [pin-skip] {p['name'][:22]:22} only {len(cs)} candles")
                continue
            time.sleep(1.2)
            e = prereg.pin_data(spec, spec_h, cs, p["address"], who=args.who)
            w = e["window"]
            print(f"  [pin] {p['name'][:22]:22} {e['status']:15} "
                  f"data={e['data_hash'][:12]} bars={w['bars']} "
                  f"[{w['start_ts']}..{w['end_ts']}]")
            pinned += 1
        print(f"\npinned {pinned} pool(s) to spec {spec_h[:12]}")
        if args.pin_only:
            print("--pin-only: exiting without evaluation.")
            return

    results = []

    for p in pools:
        try:
            candles = fetch_ohlcv(p["address"], args.limit_candles)
        except Exception as ex:
            print(f"  [skip] {p['name'][:22]:22} fetch failed: {ex}")
            continue
        if len(candles) < 120:
            print(f"  [skip] {p['name'][:22]:22} only {len(candles)} candles")
            continue
        time.sleep(1.2)
        pool = {"name": p["name"], "address": p["address"],
                "fee_bps": p["fee_bps"], "liquidity_usd": p["liquidity_usd"],
                "notional_usd": 100.0}
        try:
            out = harness.run(candles, spec, pool,
                              infrastructure_validation=infra)
        except prereg.PreregError as ex:
            # A refusal is a hard stop for the whole run, not a per-pool skip.
            # Continuing would let a later pool produce an artifact after the
            # gate already declared this evaluation illegitimate.
            print(f"  [REFUSED] {p['name'][:22]:22} {ex}")
            break
        results.append(out)
        t = out["test"]
        bh = f"{t['bench_pct']:+7.1f}%" if t["bench_pct"] is not None else "    n/a"
        print(f"  {p['name'][:22]:22} test={t['net_pct']:+7.2f}% BH={bh} "
              f"trades={t['n_trades']} (train {out['train']['n_trades']})")

    if not results:
        print("\nNo pools evaluated — nothing to report.")
        return

    gates_cfg = spec["gates"]
    rep = report.build_report(results, gates_cfg, spec["analysis"])
    rep["guard"] = results[0]["guard"]
    rep["provenance"] = results[0]["provenance"]
    rep["cost_constants"] = results[0]["cost_constants"]

    print()
    print(report.render_text(rep))

    os.makedirs(OUT_DIR, exist_ok=True)
    tag = "infrastructure_validation" if infra else f"{args.strategy}"
    path = os.path.join(OUT_DIR,
                        f"wf_{tag}_{time.strftime('%Y%m%dT%H%M%S')}.json")
    report.write_report(rep, path)
    print(f"\nwrote {path}")


if __name__ == "__main__":
    main()
