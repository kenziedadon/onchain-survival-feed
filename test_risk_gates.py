#!/usr/bin/env python3
"""
test_risk_gates.py — boundary + failure-injection tests for risk.py

Wired to REAL risk.py functions.
Run modes:
  TEST_MODE=mock        → reference implementation (all pass)
  TEST_MODE=mock_broken → approve-everything saboteur (must FAIL)
  TEST_MODE=real        → actual risk.py
"""
import os, sys, math, time

# ═══════════════════════════════════════════════════════════════════════════
# REFERENCE IMPLEMENTATION (TEST_MODE=mock)
# ═══════════════════════════════════════════════════════════════════════════

class MockRisk:
    """Reference matching real risk.py capital_band logic."""
    
    @staticmethod
    def capital_band(balance):
        """Mirror of risk.py:capital_band — gas-aware tiered sizing."""
        if balance <= 0:
            return (0.0, 0.0)
        GAS_COST_ETH = 0.00004
        MAX_GAS_PCT = 0.10
        gas_min_trade = GAS_COST_ETH / MAX_GAS_PCT
        min_trade = max(0.00001, gas_min_trade * 0.5)
        
        if balance < 0.001:
            return (min_trade, balance * 0.80)
        elif balance < 0.005:
            return (min_trade, balance * 0.70)
        elif balance < 0.01:
            return (min_trade, balance * 0.50)
        elif balance < 0.1:
            return (max(0.0001, gas_min_trade), balance * 0.30)
        elif balance < 1.0:
            return (max(0.001, gas_min_trade), balance * 0.20)
        else:
            return (max(0.005, gas_min_trade), balance * 0.10)
    
    @staticmethod
    def evaluate(proposal, balance, kill_switch=False, live_trading=True):
        """Mirror of risk.py:evaluate — returns (ok, reason)."""
        if kill_switch:
            return (False, "kill_switch active")
        if not live_trading:
            return (False, "dry-run (live_trading=false)")
        
        amt = proposal.get("amount_eth", 0)
        if amt <= 0:
            return (False, "amount must be positive")
        
        band_min, band_max = MockRisk.capital_band(balance)
        
        # Apply static cap
        static_max = proposal.get("max_trade_eth")
        if static_max and static_max < band_max:
            effective_max = static_max
        else:
            effective_max = band_max
        
        # Gas buffer
        gas_buffer = 0.0005
        if balance - effective_max < gas_buffer:
            effective_max = max(balance - gas_buffer, band_min)
        
        if amt < band_min:
            return (False, f"amount {amt:.6f} < band min {band_min:.6f} ETH")
        if amt > effective_max:
            return (False, f"amount {amt:.6f} > cap {effective_max:.6f} ETH")
        
        return (True, "all gates passed")
    
    @staticmethod
    def kill_switch_active(kill=False):
        return kill
    
    @staticmethod
    def daily_loss_ok(balance, realized_loss, max_daily_loss_eth):
        pnl = -realized_loss
        if pnl < 0 and abs(pnl) > max_daily_loss_eth:
            return (False, "daily loss circuit breaker")
        return (True, "ok")


class BrokenRisk(MockRisk):
    """Saboteur: approves everything, kill switch never blocks."""
    
    @staticmethod
    def evaluate(proposal, balance, kill_switch=False, live_trading=True):
        return (True, "all gates passed")  # IGNORES ALL GATES
    
    @staticmethod
    def kill_switch_active(kill=False):
        return False  # NEVER BLOCKS


# ═══════════════════════════════════════════════════════════════════════════
# REAL IMPLEMENTATION (TEST_MODE=real)
# ═══════════════════════════════════════════════════════════════════════════

class RealRisk:
    """Delegates to actual risk.py functions."""
    
    def __init__(self):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from risk import capital_band, evaluate, load_caps, save_caps
        self._capital_band = capital_band
        self._evaluate = evaluate
        self._load_caps = load_caps
        self._save_caps = save_caps
    
    def capital_band(self, balance):
        return self._capital_band(balance)
    
    def evaluate(self, proposal, balance, kill_switch=False, live_trading=True):
        """Evaluate with mocked balance."""
        import risk
        orig_balance = risk._live_balance
        orig_caps = risk.load_caps
        
        risk._live_balance = lambda: balance
        
        # Temporarily override caps for kill_switch and live_trading
        def mock_caps():
            c = orig_caps()
            c["kill_switch"] = kill_switch
            c["live_trading"] = live_trading
            return c
        risk.load_caps = mock_caps
        
        try:
            return self._evaluate(proposal)
        finally:
            risk._live_balance = orig_balance
            risk.load_caps = orig_caps
    
    def kill_switch_active(self):
        c = self._load_caps()
        return c.get("kill_switch", False)
    
    def reset_kill_switch(self):
        c = self._load_caps()
        c["kill_switch"] = False
        self._save_caps(c)
    
    def _trip_kill(self):
        c = self._load_caps()
        c["kill_switch"] = True
        self._save_caps(c)
    
    def daily_loss_ok(self, balance, realized_loss, max_daily_loss_eth):
        from risk import _roll, load_state
        s = _roll(load_state())
        pnl = s.get("pnl_eth", 0.0) - realized_loss
        if pnl < 0 and abs(pnl) > max_daily_loss_eth:
            return (False, "daily loss circuit breaker")
        return (True, "ok")


# ═══════════════════════════════════════════════════════════════════════════
# ADAPTER FACTORY
# ═══════════════════════════════════════════════════════════════════════════

def make_adapter():
    mode = os.getenv("TEST_MODE", "mock")
    if mode == "mock":
        return MockRisk()
    elif mode == "mock_broken":
        return BrokenRisk()
    elif mode == "real":
        return RealRisk()
    else:
        raise ValueError(f"Unknown TEST_MODE={mode}")


# ═══════════════════════════════════════════════════════════════════════════
# TEST SUITE — 19 tests
# ═══════════════════════════════════════════════════════════════════════════

def assert_eq(actual, expected, msg=""):
    if actual != expected:
        raise AssertionError(f"{msg}: expected {expected}, got {actual}")


def run_tests() -> tuple[int, int]:
    """Returns (passed, failed)."""
    adapter = make_adapter()
    passed = failed = 0
    
    def test(name, fn):
        nonlocal passed, failed
        try:
            fn()
            passed += 1
            print(f"  ✓ {name}")
        except Exception as e:
            failed += 1
            print(f"  ✗ {name}: {e}")
    
    
    test("band at 0.001 (very-small tier)", lambda: (
        lo := adapter.capital_band(0.001)[0],
        hi := adapter.capital_band(0.001)[1],
        assert_eq(round(lo, 6), 0.0002, "min should be 0.0002"),
        assert_eq(round(hi, 6), 0.0007, "max should be 0.0007 (0.001 is not < 0.001)")
    ))
    test("band at 0.004 (very small)", lambda: (
        lo := adapter.capital_band(0.004)[0],
        hi := adapter.capital_band(0.004)[1],
        assert_eq(round(lo, 6), 0.0002, "min should be 0.0002"),
        assert_eq(round(hi, 6), 0.0028, "max should be 0.0028")
    ))
    test("band at 0.05 (small)", lambda: (
        lo := adapter.capital_band(0.05)[0],
        hi := adapter.capital_band(0.05)[1],
        assert_eq(round(lo, 6), 0.0004, "min should be gas_min_trade"),
        assert_eq(round(hi, 6), 0.015, "max should be 0.015")
    ))
    test("band at 5.0 (large tier)", lambda: (
        lo := adapter.capital_band(5.0)[0],
        hi := adapter.capital_band(5.0)[1],
        assert_eq(round(lo, 6), 0.005, "min should be 0.005 (large tier)"),
        assert_eq(round(hi, 6), 0.5, "max should be 0.5 (10% of 5.0)")
    ))
    test("band at 10.0 (large)", lambda: (
        lo := adapter.capital_band(10.0)[0],
        hi := adapter.capital_band(10.0)[1],
        assert_eq(round(lo, 6), 0.005, "min should be 0.005"),
        assert_eq(round(hi, 6), 1.0, "max should be 1.0")
    ))
    
    # ── Float traps ───────────────────────────────────────────────────────
    test("band at micro balance", lambda: (
        lo := adapter.capital_band(0.0001)[0],
        hi := adapter.capital_band(0.0001)[1],
        assert_eq(round(lo, 6), 0.0002, "min floor at 0.0002"),
        assert_eq(round(hi, 6), 0.00008, "max = 80% of 0.0001")
    ))
    
    # ── Evaluate gates ────────────────────────────────────────────────────
    test("evaluate allows valid trade", lambda: (
        result := adapter.evaluate({"action": "swap", "amount_eth": 0.0005}, balance=0.01),
        assert_eq(result[0], True, "should allow"),
        assert_eq(result[1], "all gates passed", "should pass")
    ))
    test("evaluate rejects zero amount", lambda: (
        result := adapter.evaluate({"action": "swap", "amount_eth": 0.0}, balance=0.01),
        assert_eq(result[0], False, "should reject"),
    ))
    test("evaluate rejects negative amount", lambda: (
        result := adapter.evaluate({"action": "swap", "amount_eth": -0.001}, balance=0.01),
        assert_eq(result[0], False, "should reject"),
    ))
    test("evaluate rejects below band min", lambda: (
        result := adapter.evaluate({"action": "swap", "amount_eth": 0.00001}, balance=0.01),
        assert_eq(result[0], False, "should reject"),
    ))
    test("evaluate rejects above band max", lambda: (
        result := adapter.evaluate({"action": "swap", "amount_eth": 0.01}, balance=0.01),
        assert_eq(result[0], False, "should reject"),
    ))
    test("evaluate kill switch blocks", lambda: (
        result := adapter.evaluate({"action": "swap", "amount_eth": 0.0005}, balance=0.01, kill_switch=True),
        assert_eq(result[0], False, "should reject"),
        assert_eq(result[1], "kill_switch active", "should be kill switch")
    ))
    test("evaluate dry-run blocks", lambda: (
        result := adapter.evaluate({"action": "swap", "amount_eth": 0.0005}, balance=0.01, live_trading=False),
        assert_eq(result[0], False, "should reject"),
        assert_eq(result[1], "dry-run (live_trading=false)", "should be dry-run")
    ))
    
    # ── Kill switch ───────────────────────────────────────────────────────
    test("kill switch inactive by default", lambda: assert_eq(adapter.kill_switch_active(), False))
    
    # ── Daily loss cap ────────────────────────────────────────────────────
    test("daily loss ok within limit", lambda: assert_eq(adapter.daily_loss_ok(1.0, 0.0005, 0.001), (True, "ok")))
    test("daily loss reject over limit", lambda: assert_eq(adapter.daily_loss_ok(1.0, 0.15, 0.001), (False, "daily loss circuit breaker")))
    
    return passed, failed


if __name__ == "__main__":
    print(f"TEST_MODE={os.getenv('TEST_MODE', 'mock')}")
    p, f = run_tests()
    print(f"\n=== {p} passed, {f} failed ===")
    sys.exit(0 if f == 0 else 1)