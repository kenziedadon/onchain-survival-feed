#!/usr/bin/env python3
"""Arm check for base-trend-bot: evaluates strict edge gate and methodology gate."""

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

HERE = Path(__file__).parent
bt_path = HERE / "backtest_results.json"
c_path = HERE / "caps.json"

GRID_MIN_POS_FRAC = 0.55  # 55% minimum positive rate
GRID_MIN_AVG_PCT = 3.0    # 3.0% minimum average return


def _load_backtest_results():
    """Load backtest_results.json, return list of results."""
    try:
        with open(bt_path) as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        return []
    except Exception:
        return []


def _aggregate_stats():
    """Aggregate n, prof, avg across ALL backtest_results.json entries."""
    bt = _load_backtest_results()
    n = len(bt)
    if n == 0:
        return 0, 0, 0
    prof = sum(1 for r in bt if r.get("net_return_pct", 0) > 0)
    avg = sum(r.get("net_return_pct", 0) for r in bt) / n
    return n, prof, avg


def _strict_edge_ok():
    """Check strict edge gate using aggregated backtest results."""
    n, prof, avg = _aggregate_stats()
    pos_frac = prof / n if n else 0
    reasons = []
    if pos_frac < GRID_MIN_POS_FRAC:
        reasons.append(f"only {pos_frac*100:.0f}% positive (<{int(GRID_MIN_POS_FRAC*100)}%)")
    if avg < GRID_MIN_AVG_PCT:
        reasons.append(f"avg {avg:+.1f}% (<{GRID_MIN_AVG_PCT:.1f}%)")
    if reasons:
        detail = "strict bar not met: " + "; ".join(reasons)
        return False, detail
    return True, f"strict pass: {pos_frac*100:.0f}% positive, avg {avg:+.1f}% (n={n})"


def _early_edge_ok():
    path = os.path.join(HERE, "early_edge_results.json")
    if not os.path.exists(path):
        return False, "no early_edge_results.json (no backtestable new pools yet)"
    try:
        results = json.load(open(path))
    except Exception as e:
        return False, f"early results unreadable: {e}"
    if not results:
        return False, "no early results"
    n = len(results)
    prof = sum(1 for r in results if r.get("net", 0) > 0)
    avg = sum(r.get("net", 0) for r in results) / n
    if prof / n >= 0.5 and avg >= 0.0:
        return True, f"early strict pass: {prof}/{n} positive, avg {avg:+.1f}%"
    return False, f"early not met: {prof}/{n} positive, avg {avg:+.1f}%"


def main():
    c = {}
    if c_path.exists():
        with open(c_path) as f:
            c = json.load(f)

    g_ok, g_why = _strict_edge_ok()
    e_ok, e_why = _early_edge_ok()

    # Load methodology eval if available
    me_path = HERE / "methodology_eval.json"
    me = {}
    if me_path.exists():
        try:
            with open(me_path) as f:
                me = json.load(f)
        except Exception as e:
            pass

    print(f"  grid (strict)   : {g_ok} — {g_why}")
    print(f"  early (strict)  : {e_ok} — {e_why}")

    # methodology gate: >50% coins positive and avg > 0
    bt = _load_backtest_results()
    prof_total = sum(1 for r in bt if r.get("net_return_pct", 0) > 0)
    bt_avg = sum(r.get("net_return_pct", 0) for r in bt) / len(bt) if bt else 0
    meth_pass = prof_total / len(bt) >= 0.50 and bt_avg > 0 if bt else False
    print(f"  methodology     : {meth_pass} — simple_ema_v1 basket: {prof_total}/{len(bt) if bt else 0} positive, avg {bt_avg:+.1f}%")

    print(f"  strict edge streak: consecutive runs tracked separately")

    # Update edge_evidence — write to edge_evidence.json (atomic), NOT embedded in caps.json.
    # This keeps caps.json posture-only so routine timestamp refreshes don't churn it and wake
    # the watchdog.  Readers fall back to edge_evidence.json via _load_edge_evidence().
    ee = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "pass": g_ok,
        "grid_pass": g_ok,
        "detail": g_why,
        "early_edge_pass": e_ok,
        "methodology_pass": meth_pass,
    }
    _save_edge_evidence(ee)

    print(f"  edge_evidence pass: {g_ok}")
    print(f"  edge_evidence status: {'PASSED' if g_ok else 'PENDING_HUMAN_ARM'}")

    return g_ok, e_ok

def _save_edge_evidence(ev):
    """Write edge_evidence.json atomically (tmp + rename)."""
    edge_path = HERE / "edge_evidence.json"
    tmp_path = edge_path.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        json.dump(ev, f, indent=2)
        f.write("\n")
    tmp_path.replace(edge_path)

if __name__ == "__main__":
    main()