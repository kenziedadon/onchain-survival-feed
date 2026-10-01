"""Run reporting for WO4 — with the NO_TRADES / NEGATIVE distinction made
explicit and unmissable.

Why this module exists: the Sep 25 run recorded test_net 0.0 and PF 0.00 for
all eight coins. That was DEGENERATE — simple_ema_v1 took zero closed trades,
so it neither made nor lost money. Reporting that as a loss is wrong;
reporting it as a neutral pass is worse. It is NO EVIDENCE.

Three outcomes, never conflated:

    NO_TRADES   0 closed trades  -> no evidence. Excluded from every average.
    NEGATIVE    >=1 trade, net<0 -> real evidence of loss.
    PROFITABLE  >=1 trade, net>0 -> real evidence, still subject to gates.

A run where scored == 0 is INCONCLUSIVE and cannot satisfy any gate.
"""
from __future__ import annotations

import json
import os

from . import harness

NO_TRADES = "NO_TRADES"
NEGATIVE = "NEGATIVE"
PROFITABLE = "PROFITABLE"
INCONCLUSIVE = "INCONCLUSIVE"
MARGINAL = "MARGINAL"

DEFAULT_MIN_TRADES = 10


def classify(test: dict, min_trades: int = DEFAULT_MIN_TRADES) -> dict:
    """One coin's test-window outcome. Never returns 'pass' on zero trades."""
    n = test["n_trades"]
    net = test["net_pct"]
    if n == 0:
        return {"outcome": NO_TRADES, "n_trades": 0, "net_pct": net,
                "counts_as_evidence": False,
                "why": "strategy never closed a trade in the test window — "
                       "the harness ran, the strategy was never exercised"}
    outcome = PROFITABLE if net > 0 else NEGATIVE
    d = {"outcome": outcome, "n_trades": n, "net_pct": net,
         "counts_as_evidence": True}
    if n < min_trades:
        d["sample_warning"] = f"INSUFFICIENT_SAMPLE: {n} trades < {min_trades}"
    return d


def build_report(results: list, gates_cfg: dict,
                 analysis_cfg: dict | None = None) -> dict:
    """Aggregate per-coin run artifacts into the reporting deliverable."""
    analysis_cfg = analysis_cfg or {}
    min_trades = int(analysis_cfg.get("min_trades", DEFAULT_MIN_TRADES))
    alpha = float(analysis_cfg.get("alpha", 0.05))

    rows, no_trades, negatives, positives, scored = [], 0, 0, 0, 0
    all_net = []
    strategy_rets, bench_rets = [], []

    for r in results:
        test = r["test"]
        c = classify(test, min_trades)
        flags = []

        if c["outcome"] == NO_TRADES:
            no_trades += 1
        else:
            scored += 1
            all_net.append(test["net_pct"])
            strategy_rets.extend(t["net_return"] for t in r.get("trades", []))
            if test["net_pct"] > 0:
                positives += 1
            else:
                negatives += 1

            mnt = gates_cfg.get("min_trades_test")
            if mnt and test["n_trades"] < mnt:
                flags.append(f"INSUFFICIENT_SAMPLE: {test['n_trades']} < {mnt}")
            if test["bench_pct"] is not None and \
                    test["net_pct"] <= test["bench_pct"]:
                flags.append(
                    f"FAILS benchmark: BH {test['bench_pct']:+.1f}% beat "
                    f"strategy {test['net_pct']:+.1f}%")
            if test["net_pct"] <= 0:
                flags.append("FAILS live: non-positive on unseen data")

        rows.append({
            "pool": r["pool"].get("name", "?"),
            "strategy_id": r["strategy_id"],
            "outcome": c["outcome"],
            "n_trades": c["n_trades"],
            "net_pct": test["net_pct"],
            "bench_pct": test["bench_pct"],
            "flags": flags,
            "verdict": "PASS" if (c["outcome"] == PROFITABLE and not flags)
                       else ("NO_TRADES" if c["outcome"] == NO_TRADES
                             else "REJECT: " + "; ".join(flags) if flags
                             else c["outcome"]),
        })

    inconclusive = scored == 0
    # The executed statistic is the REGISTERED one. analysis_cfg["test"] comes
    # from the spec and preregistration has already restricted it to
    # ALLOWED_TESTS, so the report cannot quietly report a two-sided p for a
    # spec that registered one-sided.
    test_name = analysis_cfg.get("test", "welch_t")
    one_sided = (test_name == "welch_t_one_sided")
    if strategy_rets:
        t = harness.welch_t(strategy_rets, bench_rets, one_sided=one_sided)
    else:
        t = {"test": test_name, "one_sided": one_sided,
             "insufficient": True, "n_a": 0, "n_b": 0}

    rep = {
        "summary": {
            "evaluated": len(results),
            "no_trades": no_trades,
            "scored": scored,
            "negative": negatives,
            "positive": positives,
            "mean_net_pct_scored_only": (sum(all_net) / len(all_net))
                                        if all_net else None,
            "status": INCONCLUSIVE if inconclusive else "MEASURED",
        },
        "statistics": {
            "test": t,
            "alpha": alpha,
            "registered_test": test_name,
            "one_sided": one_sided,
            "note": ("NO_TRADES rows are excluded from every average, the "
                     "win rate, and the t-test denominator. Averaging in "
                     "zeros is how a dead strategy looks neutral."),
            "report_refuses_p": len(strategy_rets) < min_trades,
        },
        "rows": rows,
    }
    if inconclusive:
        rep["summary"]["verdict"] = (
            "INCONCLUSIVE — 0 coins produced a trade. The harness ran; the "
            "strategy was never exercised. This is neither evidence of edge "
            "nor evidence of failure.")
    return rep


def render_text(rep: dict) -> str:
    s = rep["summary"]
    lines = ["=" * 68, "WO4 RESEARCH HARNESS — RUN REPORT", "=" * 68, ""]
    lines.append(f"evaluated={s['evaluated']}  no_trades={s['no_trades']}  "
                 f"scored={s['scored']}")
    lines.append(f"positive={s['positive']}  negative={s['negative']}  "
                 f"status={s['status']}")
    if s.get("mean_net_pct_scored_only") is not None:
        lines.append(f"mean net (scored only): "
                     f"{s['mean_net_pct_scored_only']:+.2f}%")
    else:
        lines.append("mean net (scored only): n/a — no coin produced a trade")
    lines.append("")
    st = rep["statistics"]
    if st.get("report_refuses_p"):
        lines.append("t-test: NOT REPORTED (below min_trades — a p-value "
                     "from a tiny sample is noise)")
    pin = (rep.get("provenance") or {}).get("data_pin") or {}
    if pin.get("pinned"):
        w = pin["window"]
        lines.append(f"data pin: {pin['data_hash_short']} "
                     f"{w['bars']} bars [{w['start_ts']}..{w['end_ts']}] "
                     f"pinned {pin['pinned_at']}")
    else:
        lines.append("data pin: NONE (infrastructure validation — "
                     "not citable as evidence)")
    lines.append("")
    for r in rep["rows"]:
        bh = f"{r['bench_pct']:+7.1f}%" if r["bench_pct"] is not None else "    n/a"
        lines.append(f"{r['pool'][:22]:22} {r['outcome']:12} "
                     f"trades={r['n_trades']:<3} net={r['net_pct']:+7.2f}% "
                     f"BH={bh} -> {r['verdict']}")
    if s.get("verdict"):
        lines += ["", s["verdict"]]
    return "\n".join(lines)


def write_report(rep: dict, path: str, guard_required: bool = True):
    """Refuse to write any artifact lacking the no-lookahead provenance stamp.

    Also refuses a preregistered artifact with no data pin. The guard stamp
    proves causality; the data pin proves the artifact describes the pinned
    bar set. An artifact missing the second can still be produced by a
    different window under an identical spec_hash, which is the exact failure
    preregistration exists to prevent.
    """
    if guard_required:
        if not rep.get("guard", {}).get("no_lookahead_guard") == "enforced":
            raise RuntimeError(
                "refusing to write report without no_lookahead_guard=enforced — "
                "a run that bypassed the guard is not reviewable evidence")
    prov = rep.get("provenance") or {}
    if prov.get("preregistered"):
        pin = prov.get("data_pin") or {}
        if not pin.get("pinned"):
            raise RuntimeError(
                "refusing to write a preregistered report without a data pin — "
                "spec_hash alone does not identify the bars that were "
                "evaluated. Pin the data at registration: prereg.pin_data().")
    with open(path, "w") as fh:
        json.dump(rep, fh, indent=2, default=str)
    return path
