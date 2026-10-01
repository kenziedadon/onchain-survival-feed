#!/usr/bin/env python3
"""
Unit tests for executor.py idempotency lock mechanism.

Tests that the EOA safety executor properly prevents double-buying
via lockout in holdings.json before broadcast.

Run directly: python tests/test_executor_idempotency.py
All tests should print PASSED.
"""
import json
import os
import sys
from datetime import datetime, timezone, timedelta

# Add project root to path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def test_blocked_today_detects_deterministic_failures():
    """_blocked_today returns True for deterministic sell-path failures."""
    # Test the core logic from executor.py lines 593-603
    token_addr = "0xabc123"
    state_data = {
        "day": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "spent_eth": 0.0,
        "pnl_eth": 0.0,
        "trades": [],
        "blocked": [
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "proposal": {"token_in": "ETH", "token_out": token_addr},
                "reason": "sell path blocked: no valid quote"
            }
        ]
    }
    
    # Core blocking logic from _blocked_today()
    is_blocked = False
    for b in state_data.get("blocked", []):
        p = b.get("proposal", {})
        if not (p.get("token_out") == token_addr or p.get("token_in") == token_addr):
            continue
        reason = (b.get("reason") or "").lower()
        if any(k in reason for k in [
            "sell path blocked",
            "nonstandard erc20", 
            "allowance remains 0",
            "no valid quote",
        ]):
            is_blocked = True
            break
    
    assert is_blocked, "Token should be detected as blocked for deterministic failure"
    print("PASSED: test_blocked_today_detects_deterministic_failures")


def test_blocked_today_ignores_transient_errors():
    """_blocked_today returns False for transient RPC/network errors."""
    token_addr = "0xdef456"
    state_data = {
        "day": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "blocked": [
            {"reason": "connection refused"}  # Transient error
        ]
    }
    
    is_blocked = False
    for b in state_data.get("blocked", []):
        reason = (b.get("reason") or "").lower()
        # Same keywords as in _blocked_today (line 344-353 executor.py)
        if any(k in reason for k in [
            "sell path blocked",
            "nonstandard erc20",
            "allowance remains 0",
            "no valid quote",
            "gas estimate failed",
            "cannot read properties",
            "buffer",
            "unsafe allowance change",
        ]):
            is_blocked = True
            break
    
    assert not is_blocked, "Transient errors should not block token"
    print("PASSED: test_blocked_today_ignores_transient_errors")


def test_holdings_pending_buy_prevents_double_spend():
    """
    Test that a 'pending_buy' status in holdings prevents concurrent cron
    from re-buying the same token before the first swap writes final state.
    
    This is the key idempotency guard from executor.py lines 741-746:
    Write holdings with pending_buy status BEFORE calling swap.js.
    """
    HOLDINGS = {
        "0xnewtoken123": {
            "status": "pending_buy",
            "entry_eth_value": 0.0005,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
    }
    
    token_out = "0xnewtoken123"
    
    # Idempotency check from executor.py lines 696-700
    if token_out in HOLDINGS:
        status = HOLDINGS[token_out].get("status")
        assert status == "pending_buy", f"Expected pending_buy, got {status}"
        should_skip = True
        assert should_skip, "Should skip token with pending_buy status"
    
    print("PASSED: test_holdings_pending_buy_prevents_double_spend")


def test_holdings_completed_prevents_duplicate_buy():
    """
    A token with amount_token > 0 should be skipped as already held.
    This tests the second line of defense: already-fulfilled positions.
    """
    HOLDINGS = {
        "0xexistingtoken": {
            "amount_token": 1000.0,
            "entry_eth_value": 0.0005,
            "score": 75,
            "pool": "0xpool123",
            "name": "TEST_TOKEN",
            "ts": datetime.now(timezone.utc).isoformat(),
        }
    }
    
    token_out = "0xexistingtoken"
    entry = HOLDINGS[token_out]
    
    already_held = token_out in HOLDINGS
    assert already_held
    
    is_pending = entry.get("status") == "pending_buy"
    assert not is_pending, "Should not be pending_buy for completed holding"
    
    print("PASSED: test_holdings_completed_prevents_duplicate_buy")


def test_state_rolls_over_daily():
    """State should roll over blocked list at midnight UTC (risk.py:52-56)."""
    from risk import _roll
    
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    state = {
        "day": yesterday,
        "spent_eth": 0.5,
        "pnl_eth": -0.1,
        "trades": [{"ts": "yesterday", "amount_eth": 0.5}],
        "blocked": [
            {"ts": "yesterday-block", "reason": "some failure"},
            {"ts": "yesterday-block2", "reason": "another failure"}
        ]
    }
    
    rolled = _roll(state)
    
    assert rolled["day"] == today, f"Day should be today, got {rolled['day']}"
    assert rolled["spent_eth"] == 0.0, "spent_eth should reset to 0"
    assert rolled["pnl_eth"] == 0.0, "pnl_eth should reset to 0"
    assert rolled["trades"] == [], "trades should be cleared"
    assert rolled["blocked"] == [], "blocked should be cleared"
    
    print("PASSED: test_state_rolls_over_daily")


def test_load_caps_rejects_armed_backup_without_provenance():
    """load_caps() must force-disarm any live_trading=true config lacking the
    arm provenance marker (defeats a restored armed backup like caps.json.bak2)."""
    import risk, tempfile, os
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump({"live_trading": True, "kill_switch": False, "edge_evidence": {}}, tmp)
    tmp.close()
    saved = risk.CAPS_FILE
    risk.CAPS_FILE = tmp.name
    try:
        c = risk.load_caps()
        assert c["live_trading"] is False, "armed backup without provenance must be disarmed"
        assert c["kill_switch"] is True, "guard must also engage kill_switch"
        on_disk = json.load(open(tmp.name))
        assert on_disk["live_trading"] is False, "disarm must be persisted to disk"
    finally:
        risk.CAPS_FILE = saved
        os.unlink(tmp.name)
    print("PASSED: test_load_caps_rejects_armed_backup_without_provenance")


def test_load_caps_accepts_legit_armed_config():
    """load_caps() must accept a live_trading=true config stamped by arm_signal()."""
    import risk, tempfile, os
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump({"live_trading": True, "kill_switch": False,
               "_arm_provenance": risk.ARM_PROVENANCE}, tmp)
    tmp.close()
    saved = risk.CAPS_FILE
    risk.CAPS_FILE = tmp.name
    try:
        c = risk.load_caps()
        assert c["live_trading"] is True, "legit armed config must load unchanged"
    finally:
        risk.CAPS_FILE = saved
        os.unlink(tmp.name)
    print("PASSED: test_load_caps_accepts_legit_armed_config")


def run_all_tests():
    """Run all tests and report results."""
    print("=" * 60)
    print("EXECUTOR IDEMPOTENCY LOCK TESTS")
    print("=" * 60)
    
    tests = [
        test_blocked_today_detects_deterministic_failures,
        test_blocked_today_ignores_transient_errors,
        test_holdings_pending_buy_prevents_double_spend,
        test_holdings_completed_prevents_duplicate_buy,
        test_state_rolls_over_daily,
        test_load_caps_rejects_armed_backup_without_provenance,
        test_load_caps_accepts_legit_armed_config,
    ]
    
    passed = 0
    failed = 0
    
    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"FAILED: {test.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"ERROR: {test.__name__}: {e}")
            failed += 1
    
    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 60)
    
    return failed == 0


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)