"""Report tests — WO4 deliverable #5 (the NO_TRADES distinction).

The property: a strategy that never traded can never be reported as a pass,
a loss, or a neutral result. The Sep 25 degenerate run is the incident this
exists to prevent.
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from research_harness import report as R


def fake_run(name, n_trades, net, bench=None, trades=None):
    return {
        "pool": {"name": name, "fee_bps": 30.0, "liquidity_usd": 1_000_000,
                 "notional_usd": 100.0},
        "strategy_id": "simple_ema_v1",
        "test": {"net_pct": net, "n_trades": n_trades, "bench_pct": bench},
        "trades": trades if trades is not None else [
            {"net_return": net / max(n_trades, 1)} for _ in range(n_trades)],
    }


def test_zero_trades_is_not_negative():
    c = R.classify({"n_trades": 0, "net_pct": 0.0})
    assert c["outcome"] == R.NO_TRADES
    assert c["counts_as_evidence"] is False
    assert "never closed a trade" in c["why"]


def test_zero_trades_never_passes_a_gate():
    rep = R.build_report([fake_run("DEAD/POOL", 0, 0.0)],
                         {"min_trades_test": 10})
    row = rep["rows"][0]
    assert row["outcome"] == R.NO_TRADES
    assert row["verdict"] == "NO_TRADES"
    assert "PASS" not in row["verdict"]


def test_real_loss_is_negative_not_no_trades():
    c = R.classify({"n_trades": 5, "net_pct": -12.0})
    assert c["outcome"] == R.NEGATIVE
    assert c["counts_as_evidence"] is True


def test_real_gain_is_profitable():
    c = R.classify({"n_trades": 5, "net_pct": 12.0})
    assert c["outcome"] == R.PROFITABLE


def test_all_no_trades_run_is_inconclusive():
    rep = R.build_report([fake_run("A", 0, 0.0), fake_run("B", 0, 0.0)],
                         {"min_trades_test": 10})
    s = rep["summary"]
    assert s["status"] == R.INCONCLUSIVE
    assert s["scored"] == 0
    assert s["no_trades"] == 2
    assert "INCONCLUSIVE" in s["verdict"]


def test_no_trades_excluded_from_the_mean():
    """Averaging in zeros is how a dead strategy looks neutral."""
    rep = R.build_report([fake_run("LIVE", 12, 10.0),
                          fake_run("DEAD", 0, 0.0)],
                         {"min_trades_test": 10})
    assert rep["summary"]["no_trades"] == 1
    assert rep["summary"]["scored"] == 1
    assert rep["summary"]["mean_net_pct_scored_only"] == 10.0


def test_small_sample_is_flagged_not_scored_as_evidence():
    c = R.classify({"n_trades": 3, "net_pct": 5.0}, min_trades=10)
    assert "INSUFFICIENT_SAMPLE" in c["sample_warning"]


def test_small_sample_run_fails_the_min_trades_gate():
    rep = R.build_report([fake_run("THIN", 3, 5.0)],
                         {"min_trades_test": 10})
    assert any("INSUFFICIENT_SAMPLE" in f for f in rep["rows"][0]["flags"])
    assert "PASS" not in rep["rows"][0]["verdict"]


def test_benchmark_loss_fails():
    rep = R.build_report([fake_run("LAGGARD", 20, 5.0, bench=40.0)],
                         {"min_trades_test": 10})
    assert any("FAILS benchmark" in f for f in rep["rows"][0]["flags"])


def test_benchmark_win_positive_large_sample_passes():
    rep = R.build_report([fake_run("WINNER", 20, 40.0, bench=5.0)],
                         {"min_trades_test": 10})
    assert rep["rows"][0]["verdict"] == "PASS"


def test_p_value_refused_below_min_trades():
    rep = R.build_report([fake_run("THIN", 4, 5.0)],
                         {"min_trades_test": 10},
                         {"min_trades": 10, "alpha": 0.05})
    assert rep["statistics"]["report_refuses_p"] is True
    assert "NOT REPORTED" in R.render_text(rep)


def test_render_leads_with_coverage_line():
    rep = R.build_report([fake_run("A", 12, 5.0, bench=1.0),
                          fake_run("B", 0, 0.0)],
                         {"min_trades_test": 10})
    txt = R.render_text(rep)
    assert "evaluated=2" in txt and "no_trades=1" in txt and "scored=1" in txt


def test_write_report_refuses_missing_guard_stamp(tmp_path):
    """A run that bypassed the guard must not produce a reviewable artifact."""
    rep = R.build_report([fake_run("A", 12, 5.0)], {"min_trades_test": 10})
    with pytest.raises(RuntimeError) as exc:
        R.write_report(rep, str(tmp_path / "r.json"))
    assert "no_lookahead_guard" in str(exc.value)
    assert not os.path.exists(tmp_path / "r.json")


def test_write_report_accepts_stamped_report(tmp_path):
    rep = R.build_report([fake_run("A", 12, 5.0)], {"min_trades_test": 10})
    rep["guard"] = {"no_lookahead_guard": "enforced"}
    p = R.write_report(rep, str(tmp_path / "r.json"))
    assert os.path.exists(p)


def test_statistics_note_explains_exclusion():
    rep = R.build_report([fake_run("A", 0, 0.0)], {"min_trades_test": 10})
    assert "excluded from every average" in rep["statistics"]["note"]
