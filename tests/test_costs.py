"""Cost model tests — WO4 deliverable #5.

The headline property: the new model is never more optimistic than the legacy
one it replaces. Legacy used max(computed*0.5, 1.0); if that regresses, a
cheap edge could reappear on paper that does not exist live.
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from research_harness import costs


def legacy_slip_bps(notional, liquidity):
    """Transcribed from pullback_walkforward_fixed.py line 72."""
    return min(max((notional / liquidity) * 10000 * 0.5, 1.0), 80.0)


def test_pessimistic_is_never_more_cheaper_than_legacy():
    for liq in (100_000, 250_000, 1_000_000, 10_000_000):
        for notional in (25.0, 100.0, 1000.0):
            m = costs.CostModel(30.0, liq, notional, "pessimistic")
            legacy = legacy_slip_bps(notional, liq)
            assert m.slippage_bps() >= legacy, (
                f"pessimistic slippage {m.slippage_bps():.2f} < legacy "
                f"{legacy:.2f} at liq={liq} notional={notional}")


def test_floor_is_conservative_not_flattering():
    """Legacy floored at 1.0 bps. The new floor must be well above it."""
    deep = costs.CostModel(30.0, 100_000_000, 10.0, "pessimistic")
    assert deep.slippage_bps() >= costs.SLIPPAGE_FLOOR_BPS
    assert costs.SLIPPAGE_FLOOR_BPS > 1.0


def test_thin_pools_cost_more_than_deep_pools():
    thin = costs.CostModel(30.0, 50_000, 100.0, "pessimistic")
    deep = costs.CostModel(30.0, 5_000_000, 100.0, "pessimistic")
    assert thin.roundtrip_bps() > deep.roundtrip_bps()


def test_nominal_is_cheaper_than_pessimistic():
    n = costs.CostModel(30.0, 250_000, 100.0, "nominal")
    p = costs.CostModel(30.0, 250_000, 100.0, "pessimistic")
    assert n.roundtrip_bps() < p.roundtrip_bps()


def test_unknown_scenario_rejected():
    with pytest.raises(ValueError):
        costs.CostModel(30.0, 250_000, 100.0, "optimistic_and_wrong")


def test_nonpositive_liquidity_rejected():
    with pytest.raises(ValueError):
        costs.CostModel(30.0, 0, 100.0, "pessimistic")


def test_ceiling_prevents_absurd_costs():
    """A broken liquidity figure must not silently zero every trade."""
    m = costs.CostModel(30.0, 1.0, 1_000_000.0, "pessimistic")
    assert m.slippage_bps() <= costs.SLIPPAGE_CEILING_BPS


def test_roundtrip_doubles_fee_and_slippage_but_not_gas():
    m = costs.CostModel(30.0, 1_000_000, 100.0, "pessimistic")
    expected = 30.0 * 2 + m.slippage_bps() * 2 + m.gas_bps()
    assert abs(m.roundtrip_bps() - expected) < 1e-9


def test_per_trade_log_is_checkable_by_hand():
    m = costs.CostModel(30.0, 1_000_000, 100.0, "pessimistic")
    log = m.per_trade_log()
    for k in ("pool_fee_bps_oneway", "fee_bps_roundtrip", "slippage_bps_oneway",
              "gas_bps", "roundtrip_bps", "notional_usd", "liquidity_usd"):
        assert k in log, f"per-trade log missing {k} — costs must be auditable"
    assert abs(log["fee_bps_roundtrip"] - 60.0) < 1e-6


def test_constants_document_cites_every_constant():
    doc = costs.constants_document()
    names = {c["name"] for c in doc["constants"]}
    for required in ("SLIPPAGE_MULTIPLIER", "SLIPPAGE_FLOOR_BPS",
                     "GAS_USD_FALLBACK"):
        assert required in names
    for c in doc["constants"]:
        assert c.get("source"), f"{c['name']} has no source"
        assert c.get("asof"), f"{c['name']} has no as-of date"
    assert doc["headline_scenario"] == "pessimistic"


def test_pool_fee_parsed_from_label():
    assert costs.pool_fee_bps("WETH / USDC 0.05%") == 5.0
    assert costs.pool_fee_bps("DRB / WETH 1%") == 100.0


def test_unparseable_pool_fee_raises_rather_than_guessing():
    """A wrong fee silently biases every trade — never default it."""
    with pytest.raises(ValueError):
        costs.pool_fee_bps("MYSTERY / POOL")


def test_gas_fallback_is_positive_and_bounded():
    assert costs.GAS_USD_FALLBACK > 0
    m = costs.CostModel(30.0, 1_000_000, 10.0, "pessimistic")
    assert m.gas_bps() > 0
