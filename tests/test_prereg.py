"""Preregistration tests — WO4 deliverable #3.

The property under test: a run that was not registered before it started
cannot happen, and changing the experiment requires a new registration.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from research_harness import prereg


@pytest.fixture(autouse=True)
def isolated_specs(tmp_path, monkeypatch):
    """Redirect SPECS_DIR/REGISTRY so tests never touch the real ledger."""
    d = tmp_path / "specs"
    d.mkdir()
    monkeypatch.setattr(prereg, "SPECS_DIR", str(d))
    monkeypatch.setattr(prereg, "REGISTRY", str(d / "registry.jsonl"))
    monkeypatch.setattr(prereg, "DATA_PINS", str(d / "data_pins.jsonl"))
    yield str(d)


BASE = {
    "strategy_id": "simple_ema_v1",
    "strategy_version": "1.0.0",
    "params": {"fast": 9, "slow": 21, "stop_loss_pct": 0.025,
               "take_profit_pct": 0.05},
    "data": {"source": "geckoterminal", "interval": "hour", "limit": 300},
    "split": {"train_frac": 0.6, "scheme": "positional"},
    "costs": {"model": "pessimistic_v1", "slippage_multiplier": 2.0},
    "gates": {"min_trades_test": 10, "pf_min": 1.1, "maxdd_max": 40.0},
    "analysis": {"test": "welch_t", "alpha": 0.05},
}


def test_hash_is_deterministic_across_key_order():
    a = {k: BASE[k] for k in reversed(list(BASE))}
    assert prereg.spec_hash(a) == prereg.spec_hash(BASE)
    assert len(prereg.spec_hash(BASE)) == 64


def test_hash_changes_when_any_param_changes():
    b = json.loads(json.dumps(BASE))
    b["params"]["fast"] = 10
    assert prereg.spec_hash(b) != prereg.spec_hash(BASE)


def test_unregistered_spec_is_refused():
    """THE refusal gate. Nothing runs without registration."""
    with pytest.raises(prereg.PreregError) as exc:
        prereg.require_registered(BASE)
    assert "NOT registered" in str(exc.value)


def test_registered_spec_passes_the_gate():
    prereg.register(BASE, who="test")
    h = prereg.require_registered(BASE)
    assert h == prereg.spec_hash(BASE)


def test_tampered_spec_is_refused_after_registration():
    """Register, then quietly change a param. The run must not proceed —
    this is exactly the 'tuned until it looked good' failure."""
    prereg.register(BASE, who="test")
    tampered = json.loads(json.dumps(BASE))
    tampered["params"]["take_profit_pct"] = 0.50   # much more attractive
    with pytest.raises(prereg.PreregError):
        prereg.require_registered(tampered)


def test_changing_costs_requires_new_registration():
    prereg.register(BASE, who="test")
    tweaked = json.loads(json.dumps(BASE))
    tweaked["costs"]["slippage_multiplier"] = 0.5   # make costs kinder
    with pytest.raises(prereg.PreregError):
        prereg.require_registered(tweaked)


def test_ledger_records_who_and_when():
    e = prereg.register(BASE, who="hermes", note="initial")
    led = prereg.ledger()
    assert len(led) == 1
    assert led[0]["who"] == "hermes"
    assert led[0]["spec_hash"] == e["spec_hash"]
    assert led[0]["strategy_hash"], "strategy code hash must be recorded too"


def test_strategy_hash_is_recorded_and_differs_from_spec_hash():
    e = prereg.register(BASE, who="test")
    assert e["strategy_hash"] != e["spec_hash"]
    assert len(e["strategy_hash"]) == 64


def test_spec_roundtrips_through_disk():
    prereg.register(BASE, who="test")
    h = prereg.spec_hash(BASE)
    loaded = prereg.load_spec(h)
    assert loaded["params"] == BASE["params"]
    assert prereg.spec_hash(loaded) == h


def test_load_by_short_hash():
    prereg.register(BASE, who="test")
    short = prereg.short(prereg.spec_hash(BASE))
    assert prereg.spec_hash(prereg.load_spec(short)) == prereg.spec_hash(BASE)


def test_missing_required_key_rejected():
    for k in list(BASE):
        bad = {kk: v for kk, v in BASE.items() if kk != k}
        with pytest.raises(prereg.PreregError):
            prereg.validate_spec(bad)


def test_absurd_train_frac_rejected():
    bad = json.loads(json.dumps(BASE))
    bad["split"]["train_frac"] = 0.99
    with pytest.raises(prereg.PreregError):
        prereg.validate_spec(bad)


def test_unsupported_statistical_test_rejected():
    bad = json.loads(json.dumps(BASE))
    bad["analysis"]["test"] = "chi_squared"
    with pytest.raises(prereg.PreregError):
        prereg.validate_spec(bad)


def test_provenance_marks_infrastructure_validation_unregistered():
    """The very first harness-validation run must be labelled so it can never
    be cited as strategy evidence."""
    p = prereg.provenance(BASE, prereg.spec_hash(BASE), True)
    assert p["preregistered"] is False
    assert p["infrastructure_validation"] is True


def test_provenance_for_real_run_is_preregistered():
    p = prereg.provenance(BASE, prereg.spec_hash(BASE), False)
    assert p["preregistered"] is True
    assert p["registered_before_run"] is True


def test_unknown_strategy_hash_raises():
    bad = json.loads(json.dumps(BASE))
    bad["strategy_id"] = "does_not_exist_v9"
    with pytest.raises(prereg.PreregError):
        prereg.register(bad, who="test")


# ---------------------------------------------------------------------------
# Finding 1 — the registered statistic must be the executed statistic.
# WO4 specifies a ONE-SIDED test. Two-sided is conservative but it is a
# different hypothesis, and a spec that does not describe the statistic it
# runs has a broken audit trail.
# ---------------------------------------------------------------------------

def test_one_sided_test_is_accepted():
    spec = json.loads(json.dumps(BASE))
    spec["analysis"]["test"] = "welch_t_one_sided"
    prereg.validate_spec(spec)          # must not raise


def test_one_sided_and_two_sided_are_distinct_registrations():
    """Changing the test changes spec_hash -> a new experiment, not an edit."""
    two = json.loads(json.dumps(BASE))
    one = json.loads(json.dumps(BASE))
    one["analysis"]["test"] = "welch_t_one_sided"
    assert prereg.spec_hash(two) != prereg.spec_hash(one)


def test_missing_statistical_test_is_rejected_not_defaulted():
    """No implicit default. A spec that omits `test` must be refused rather
    than silently executed under a statistic nobody registered."""
    spec = json.loads(json.dumps(BASE))
    del spec["analysis"]["test"]
    with pytest.raises(prereg.PreregError):
        prereg.validate_spec(spec)


# ---------------------------------------------------------------------------
# Finding 3 — data provenance. The spec pinned WHERE to fetch, never WHAT.
# Same registered spec + a rolled upstream window = a different experiment
# wearing an identical spec_hash. Pin the bars themselves.
# ---------------------------------------------------------------------------

def _bars(n=200, seed=0.0):
    return [{"ts": 1787223600 + i * 3600, "o": 2000.0 + i + seed,
             "h": 2005.0 + i + seed, "l": 1995.0 + i + seed,
             "c": 2001.0 + i + seed, "v": 1000.0 + i} for i in range(n)]


POOL = "0xabc123"


def test_data_hash_is_deterministic_and_content_sensitive():
    a, b = _bars(), _bars()
    assert prereg.data_hash(a, POOL) == prereg.data_hash(b, POOL)
    drifted = _bars()
    drifted[-1]["c"] += 0.01          # one basis point on the last bar
    assert prereg.data_hash(drifted, POOL) != prereg.data_hash(a, POOL)


def test_data_hash_is_scoped_to_pool():
    """Identical bars attributed to a different pool must not satisfy a pin."""
    a = _bars()
    assert prereg.data_hash(a, "0xpool_a") != prereg.data_hash(a, "0xpool_b")


def test_data_window_reports_bar_bounds():
    w = prereg.data_window(_bars(50))
    assert w["bars"] == 50
    assert w["end_ts"] > w["start_ts"]


def test_unpinned_spec_is_refused():
    """The gate: registered but never pinned -> refuse, do not evaluate."""
    spec_h = prereg.spec_hash(BASE)
    with pytest.raises(prereg.PreregError):
        prereg.require_pinned_data(spec_h, _bars(), POOL)


def test_rolled_window_is_refused_even_though_spec_is_registered():
    """This is the exact cheat the fix exists to stop: spec_hash identical,
    upstream window moved, run would otherwise look fully preregistered."""
    prereg.register(BASE, who="test")
    spec_h = prereg.spec_hash(BASE)
    original = _bars()
    prereg.pin_data(BASE, spec_h, original, POOL, who="test")
    # Same spec, still registered — but the upstream window has rolled.
    rolled = _bars(seed=1.0)
    assert prereg.spec_hash(BASE) == spec_h          # spec did not change
    with pytest.raises(prereg.PreregError):
        prereg.require_pinned_data(spec_h, rolled, POOL)


def test_pinned_identical_data_is_accepted():
    prereg.register(BASE, who="test")
    spec_h = prereg.spec_hash(BASE)
    bars = _bars()
    prereg.pin_data(BASE, spec_h, bars, POOL, who="test")
    pin = prereg.require_pinned_data(spec_h, _bars(), POOL)
    assert pin["data_hash"] == prereg.data_hash(bars, POOL)


def test_repinning_identical_data_is_a_noop():
    prereg.register(BASE, who="test")
    spec_h = prereg.spec_hash(BASE)
    e1 = prereg.pin_data(BASE, spec_h, _bars(), POOL, who="test")
    e2 = prereg.pin_data(BASE, spec_h, _bars(), POOL, who="test")
    assert e1["status"] == "pinned"
    assert e2["status"] == "already_pinned"


def test_repinning_different_window_raises():
    """Overwriting a pin with a new window is a new experiment, not an edit."""
    prereg.register(BASE, who="test")
    spec_h = prereg.spec_hash(BASE)
    prereg.pin_data(BASE, spec_h, _bars(), POOL, who="test")
    with pytest.raises(prereg.PreregError):
        prereg.pin_data(BASE, spec_h, _bars(seed=2.0), POOL, who="test")


def test_pin_is_scoped_to_its_pool():
    prereg.register(BASE, who="test")
    spec_h = prereg.spec_hash(BASE)
    bars = _bars()
    prereg.pin_data(BASE, spec_h, bars, POOL, who="test")
    with pytest.raises(prereg.PreregError):
        prereg.require_pinned_data(spec_h, bars, "0xdifferent")


def test_data_pin_ledger_is_append_only():
    prereg.register(BASE, who="test")
    spec_h = prereg.spec_hash(BASE)
    prereg.pin_data(BASE, spec_h, _bars(), POOL, who="test")
    prereg.pin_data(BASE, spec_h, _bars(), POOL, who="test")
    pins = prereg._read_jsonl(prereg.DATA_PINS)
    assert len(pins) == 2      # both attempts recorded, none overwritten


def test_multi_pool_pin_is_per_pool_not_last_wins():
    """Regression: the pin ledger is keyed (spec, pool). Keying it on spec
    alone made pool B's bars look like a re-pin of pool A's and raised a false
    conflict on the second pool of every multi-pool run."""
    prereg.register(BASE, who="test")
    spec_h = prereg.spec_hash(BASE)
    a, b = _bars(seed=0.0), _bars(seed=5.0)
    prereg.pin_data(BASE, spec_h, a, "0xpool_a", who="test")
    e = prereg.pin_data(BASE, spec_h, b, "0xpool_b", who="test")
    assert e["status"] == "pinned"
    # each pool validates against ITS OWN pin
    assert prereg.require_pinned_data(spec_h, a, "0xpool_a")["data_hash"] \
        == prereg.data_hash(a, "0xpool_a")
    assert prereg.require_pinned_data(spec_h, b, "0xpool_b")["data_hash"] \
        == prereg.data_hash(b, "0xpool_b")
    # ...and pool A's bars do NOT satisfy pool B's pin
    with pytest.raises(prereg.PreregError):
        prereg.require_pinned_data(spec_h, a, "0xpool_b")


def test_unpinned_pool_in_a_pinned_spec_is_refused():
    """Pinning two of three pools must not license the third."""
    prereg.register(BASE, who="test")
    spec_h = prereg.spec_hash(BASE)
    prereg.pin_data(BASE, spec_h, _bars(), "0xpool_a", who="test")
    prereg.pin_data(BASE, spec_h, _bars(seed=5.0), "0xpool_b", who="test")
    with pytest.raises(prereg.PreregError):
        prereg.require_pinned_data(spec_h, _bars(), "0xpool_c")


def test_pool_lookup_is_case_insensitive():
    prereg.register(BASE, who="test")
    spec_h = prereg.spec_hash(BASE)
    prereg.pin_data(BASE, spec_h, _bars(), "0xPoolA", who="test")
    pin = prereg.require_pinned_data(spec_h, _bars(), "0xpoola")
    assert pin["pool"] == "0xpoola"
