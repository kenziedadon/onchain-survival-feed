"""End-to-end harness tests: walk-forward mechanics, registry, split.

Uses synthetic candles only — no network, no live data. The network-touching
run is a separate script (run_walkforward.py), not a unit test.
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from research_harness import harness, prereg, strategies


def synthetic(n=200, seed=7):
    """Deterministic pseudo-random walk with trends — enough structure for a
    strategy to act, without any real market data."""
    px, out = 100.0, []
    s = seed
    for i in range(n):
        s = (1103515245 * s + 12345) % 2147483648
        px *= 1.0 + ((s / 2147483648.0) - 0.48) * 0.04
        hi, lo = px * 1.004, px * 0.996
        out.append({"o": px, "h": hi, "l": lo, "c": px, "v": 1000.0})
    return out


def spec(strategy_id="simple_ema_v1", train_frac=0.6):
    mod = strategies.get(strategy_id)
    params = dict(mod.DEFAULTS)
    return {
        "strategy_id": strategy_id,
        "strategy_version": mod.STRATEGY_VERSION,
        "params": params,
        "data": {"source": "synthetic", "interval": "hour", "limit": 200},
        "split": {"train_frac": train_frac, "scheme": "positional"},
        "costs": {"model": "pessimistic", "slippage_multiplier": 2.0},
        "gates": {"min_trades_test": 10, "pf_min": 1.1, "maxdd_max": 40.0},
        "analysis": {"test": "welch_t", "alpha": 0.05},
    }


POOL = {"name": "TEST / WETH 0.3%", "fee_bps": 30.0,
        "liquidity_usd": 1_000_000.0, "notional_usd": 100.0}


def test_registry_lists_both_strategies():
    av = strategies.available()
    assert "simple_ema_v1" in av and "pullback_v2" in av


def test_unknown_strategy_raises_not_defaults():
    with pytest.raises(strategies.UnknownStrategy):
        strategies.get("no_such_strategy_v9")


def test_split_is_positional_and_correct():
    c = synthetic(200)
    tr, te = harness.split_candles(c, 0.6)
    assert len(tr) == 120 and len(te) == 80
    assert tr[0] is c[0] and te[0] is c[120]


def test_absurd_split_raises():
    with pytest.raises(ValueError):
        harness.split_candles(synthetic(200), 0.99)


def test_welch_t_on_identical_samples_is_not_significant():
    a = [1.0, 2.0, 3.0, 4.0, 5.0]
    r = harness.welch_t(a, list(a))
    assert abs(r["t"]) < 1e-9
    assert r["p_approx"] > 0.99


def test_welch_t_detects_a_real_difference():
    a = [5.0, 6.0, 5.5, 6.5, 5.2, 6.1, 5.8, 6.3]
    b = [1.0, 1.5, 1.2, 1.8, 1.1, 1.4, 1.3, 1.6]
    r = harness.welch_t(a, b)
    assert r["p_approx"] < 0.001
    assert r["diff"] > 0


def test_welch_t_refuses_tiny_samples():
    r = harness.welch_t([1.0], [2.0, 3.0])
    assert r["insufficient"] is True


def test_unregistered_spec_is_refused_end_to_end(monkeypatch):
    d = os.path.join(os.path.dirname(ROOT), "specs_test_tmp")
    os.makedirs(d, exist_ok=True)
    monkeypatch.setattr(prereg, "SPECS_DIR", d)
    monkeypatch.setattr(prereg, "REGISTRY", os.path.join(d, "registry.jsonl"))
    monkeypatch.setattr(prereg, "DATA_PINS", os.path.join(d, "data_pins.jsonl"))
    with pytest.raises(prereg.PreregError):
        harness.run(synthetic(200), spec(), POOL)


def test_infrastructure_validation_run_needs_no_registration(monkeypatch):
    d = os.path.join(os.path.dirname(ROOT), "specs_test_tmp2")
    os.makedirs(d, exist_ok=True)
    monkeypatch.setattr(prereg, "SPECS_DIR", d)
    monkeypatch.setattr(prereg, "REGISTRY", os.path.join(d, "registry.jsonl"))
    monkeypatch.setattr(prereg, "DATA_PINS", os.path.join(d, "data_pins.jsonl"))
    out = harness.run(synthetic(200), spec(), POOL,
                      infrastructure_validation=True)
    assert out["provenance"]["preregistered"] is False
    assert out["provenance"]["infrastructure_validation"] is True


def test_registered_run_carries_full_provenance(monkeypatch):
    d = os.path.join(os.path.dirname(ROOT), "specs_test_tmp3")
    os.makedirs(d, exist_ok=True)
    monkeypatch.setattr(prereg, "SPECS_DIR", d)
    monkeypatch.setattr(prereg, "REGISTRY", os.path.join(d, "registry.jsonl"))
    monkeypatch.setattr(prereg, "DATA_PINS", os.path.join(d, "data_pins.jsonl"))
    s = spec()
    prereg.register(s, who="pytest")
    bars = synthetic(200)
    prereg.pin_data(s, prereg.spec_hash(s), bars, "", who="pytest")
    out = harness.run(bars, s, POOL)
    assert out["provenance"]["preregistered"] is True
    assert out["provenance"]["registered_before_run"] is True
    assert len(out["provenance"]["spec_hash"]) == 64
    assert len(out["provenance"]["strategy_hash"]) == 64
    assert out["provenance"]["data_pin"]["pinned"] is True


def test_artifact_always_carries_guard_stamp():
    out = harness.run(synthetic(200), spec(), POOL,
                      infrastructure_validation=True)
    assert out["guard"]["no_lookahead_guard"] == "enforced"
    assert "cost_constants" in out
    assert out["cost_constants"]["headline_scenario"] == "pessimistic"


def test_costs_reduce_return_versus_zero_cost():
    """Costs must actually bite, or the harness reports a fantasy."""
    mod = strategies.get("simple_ema_v1")
    c = synthetic(200)
    cheap = harness.run_segment(c, mod, mod.DEFAULTS, 30.0, 1_000_000, 100.0,
                                "nominal")
    dear = harness.run_segment(c, mod, mod.DEFAULTS, 30.0, 1_000_000, 100.0,
                               "pessimistic")
    assert dear.net_pct <= cheap.net_pct


def test_trades_carry_audit_trail():
    out = harness.run(synthetic(300), spec(), POOL,
                      infrastructure_validation=True)
    for t in out["trades"]:
        for k in ("entry_bar", "exit_bar", "entry_px", "exit_px",
                  "gross_return", "cost_bps", "net_return", "reason"):
            assert k in t, f"trade missing {k} — results must be auditable"
        assert t["exit_bar"] > t["entry_bar"]


def test_pullback_v2_legacy_runs_under_new_harness():
    """Reproducibility check: the legacy strategy must execute here, so the
    Aug 21 result can be regenerated and compared."""
    mod = strategies.get("pullback_v2")
    r = harness.run_segment(synthetic(300), mod, mod.DEFAULTS, 30.0,
                            1_000_000, 100.0, "pessimistic")
    assert r.n_trades >= 0
    assert r.bench_pct is None or isinstance(r.bench_pct, float)


def test_lookahead_in_a_strategy_aborts_the_whole_run():
    """The guard must propagate out of run_segment, not degrade to a partial."""
    class Peeker:
        STRATEGY_VERSION = "test"
        DEFAULTS = {}

        @staticmethod
        def min_history_bars(p):
            return 0

        @staticmethod
        def exit_rule(entry_px, bars, params):
            return len(bars) - 1, "window_end"

        @staticmethod
        def generate_signals(window, params):
            i = window.bar
            fwd = window[i + 3]["c"]          # the cheat
            return ([{"entry_bar": i, "exit_bar": i + 1}] if fwd > 0 else [])

    from research_harness.no_lookahead import LookaheadViolation
    with pytest.raises(LookaheadViolation):
        harness.run_segment(synthetic(100), Peeker, {}, 30.0, 1_000_000, 100.0)


# ---------------------------------------------------------------------------
# Finding 1 — the statistic the spec registers is the statistic that runs.
# ---------------------------------------------------------------------------

def test_one_sided_p_is_half_the_two_sided_p_for_a_positive_effect():
    """One-sided halves the tail area when the effect points the predicted
    way. If these ever come out equal, the flag is being ignored."""
    strat = [1.2, 1.5, 1.1, 1.9, 1.4, 1.6, 1.3, 1.8, 1.45, 1.55,
             1.25, 1.35]
    bench = [0.4, 0.6, 0.5, 0.7, 0.45, 0.65, 0.55, 0.75, 0.5, 0.6,
             0.42, 0.52]
    two = harness.welch_t(strat, bench, one_sided=False)
    one = harness.welch_t(strat, bench, one_sided=True)
    assert two["t"] == one["t"]
    assert one["p_approx"] == pytest.approx(two["p_approx"] / 2.0, rel=1e-9)
    assert one["test"] == "welch_t_one_sided"
    assert two["test"] == "welch_t"


def test_one_sided_refuses_to_call_a_negative_effect_significant():
    """Strategy UNDERPERFORMS the benchmark: t < 0. A one-sided p must be 1.0,
    never something that could be misread as evidence FOR the strategy."""
    strat = [0.4, 0.6, 0.5, 0.7, 0.45, 0.65, 0.55, 0.75, 0.5, 0.6,
             0.42, 0.52]
    bench = [1.2, 1.5, 1.1, 1.9, 1.4, 1.6, 1.3, 1.8, 1.45, 1.55,
             1.25, 1.35]
    r = harness.welch_t(strat, bench, one_sided=True)
    assert r["t"] < 0
    assert r["p_approx"] == 1.0
    # the two-sided form is much smaller here — this is precisely why the
    # statistic must be the one the spec registered, not chosen afterwards.
    assert harness.welch_t(strat, bench, one_sided=False)["p_approx"] < 1.0


def test_welch_t_default_remains_two_sided():
    """No silent behaviour change for existing callers."""
    a = [1.1, 1.4, 1.2, 1.6, 1.3, 1.5]
    assert harness.welch_t(a, [0.5, 0.7, 0.6, 0.8, 0.65, 0.75])["one_sided"] \
        is False


def test_report_uses_the_registered_test(monkeypatch):
    """report.build_report must execute what analysis.test says, not a
    hardcoded two-sided default."""
    from research_harness import report as report_mod

    def fake_run(n_trades, net, bench, trades):
        return {"pool": {"name": "TEST / WETH 0.3%"},
                "strategy_id": "simple_ema_v1",
                "test": {"net_pct": net, "n_trades": n_trades,
                         "bench_pct": bench},
                "trades": [{"net_return": r} for r in trades]}

    results = [
        fake_run(12, 3.0, 1.0, [0.5, 0.6, 0.7, 0.8, 0.9, 1.0,
                                1.1, 1.2, 1.3, 1.4, 1.5, 1.6]),
        fake_run(12, 2.5, 1.2, [0.4, 0.5, 0.6, 0.7, 0.8, 0.9,
                                1.0, 1.1, 1.2, 1.3, 1.4, 1.5]),
    ]
    gates = {"min_trades_test": 10, "pf_min": 1.1, "maxdd_max": 40.0}

    one = report_mod.build_report(
        results, gates, {"test": "welch_t_one_sided", "alpha": 0.05})
    two = report_mod.build_report(
        results, gates, {"test": "welch_t", "alpha": 0.05})

    assert one["statistics"]["one_sided"] is True
    assert two["statistics"]["one_sided"] is False
    assert one["statistics"]["test"]["test"] == "welch_t_one_sided"
    assert two["statistics"]["test"]["test"] == "welch_t"
    assert one["statistics"]["registered_test"] == "welch_t_one_sided"
    assert two["statistics"]["registered_test"] == "welch_t"
    # p-halving itself is asserted directly against welch_t in
    # test_one_sided_p_is_half_the_two_sided_p_for_a_positive_effect; here we
    # only assert the flag reaches the executed statistic.
    assert one["statistics"]["test"]["one_sided"] is True
    assert two["statistics"]["test"]["one_sided"] is False


def test_infrastructure_validation_records_observed_hash_without_pinning():
    out = harness.run(synthetic(200), spec(), POOL,
                      infrastructure_validation=True)
    pin = out["provenance"]["data_pin"]
    assert pin["pinned"] is False
    # Still recorded, so an infra run is comparable even though uncitable.
    assert len(pin["data_hash_observed"]) == 64
    assert pin["window"]["bars"] == 200


# ---------------------------------------------------------------------------
# Finding 3 — the data gate, end to end through harness.run().
# ---------------------------------------------------------------------------

def _iso(monkeypatch, tag):
    d = os.path.join(os.path.dirname(ROOT), f"specs_pin_{tag}")
    os.makedirs(d, exist_ok=True)
    monkeypatch.setattr(prereg, "SPECS_DIR", d)
    monkeypatch.setattr(prereg, "REGISTRY", os.path.join(d, "registry.jsonl"))
    monkeypatch.setattr(prereg, "DATA_PINS", os.path.join(d, "data_pins.jsonl"))
    return d


def test_registered_but_unpinned_run_is_refused(monkeypatch):
    """Registration alone is not enough. This is the whole finding."""
    _iso(monkeypatch, "unpinned")
    s = spec()
    prereg.register(s, who="pytest")
    with pytest.raises(prereg.PreregError):
        harness.run(synthetic(200), s, POOL)


def test_run_with_rolled_window_is_refused_despite_matching_spec(monkeypatch):
    """The precise cheat: same spec_hash, different bars, still looks
    registered. Must refuse."""
    _iso(monkeypatch, "rolled")
    s = spec()
    prereg.register(s, who="pytest")
    prereg.pin_data(s, prereg.spec_hash(s), synthetic(200), "", who="pytest")
    rolled = synthetic(200, seed=99)          # same length, different bars
    with pytest.raises(prereg.PreregError):
        harness.run(rolled, s, POOL)


def test_pinned_run_stamps_the_data_hash(monkeypatch):
    _iso(monkeypatch, "stamped")
    s = spec()
    prereg.register(s, who="pytest")
    bars = synthetic(200)
    prereg.pin_data(s, prereg.spec_hash(s), bars, "", who="pytest")
    out = harness.run(bars, s, POOL)
    pin = out["provenance"]["data_pin"]
    assert pin["pinned"] is True
    assert pin["data_hash"] == prereg.data_hash(bars, "")
    assert pin["window"]["bars"] == 200


def test_unpinned_report_is_not_writable(tmp_path):
    """write_report refuses a preregistered artifact with no data pin, the
    same way it already refuses a missing guard stamp."""
    from research_harness import report as report_mod
    rep = {
        "guard": {"no_lookahead_guard": "enforced"},
        "provenance": {"preregistered": True, "data_pin": {"pinned": False}},
    }
    with pytest.raises(RuntimeError):
        report_mod.write_report(rep, str(tmp_path / "nope.json"))


def test_infrastructure_report_still_writable(tmp_path):
    """Not-citable runs must not be blocked by the pin check — they are
    already labelled uncitable and are used to validate the harness itself."""
    from research_harness import report as report_mod
    rep = {
        "guard": {"no_lookahead_guard": "enforced"},
        "provenance": {"preregistered": False,
                       "data_pin": {"pinned": False}},
    }
    p = report_mod.write_report(rep, str(tmp_path / "infra.json"))
    assert os.path.exists(p)


def test_rolling_one_pool_does_not_disturb_another(monkeypatch):
    """End-to-end: two pools pinned, one rolls. The good pool must still
    validate and the rolled one must still be refused."""
    _iso(monkeypatch, "multi")
    s = spec()
    prereg.register(s, who="pytest")
    bars_a = synthetic(200, seed=7)
    bars_b = synthetic(200, seed=21)
    h = prereg.spec_hash(s)
    prereg.pin_data(s, h, bars_a, "0xpool_a", who="pytest")
    prereg.pin_data(s, h, bars_b, "0xpool_b", who="pytest")

    out_a = harness.run(bars_a, s, dict(POOL, address="0xpool_a"))
    assert out_a["provenance"]["data_pin"]["data_hash"] \
        == prereg.data_hash(bars_a, "0xpool_a")

    with pytest.raises(prereg.PreregError):
        harness.run(synthetic(200, seed=99), s, dict(POOL, address="0xpool_b"))
