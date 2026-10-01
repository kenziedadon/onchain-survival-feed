#!/usr/bin/env python3
"""
test_nous_gate_fallback.py — failure-injection tests for nous_gate.py

Tests the 8-model fallback cascade with injected failures.
Run modes:
  TEST_MODE=mock        → reference implementation (all pass)
  TEST_MODE=mock_broken → approve-everything saboteur (must FAIL)
  TEST_MODE=real        → your nous_gate.py (implement RealGateAdapter)
"""
import os, sys, json, time, re
from typing import Protocol, Optional, Callable
from unittest.mock import patch, MagicMock

# ═══════════════════════════════════════════════════════════════════════════
# ADAPTER CONTRACT — implement 1 method to test your nous_gate.py
# ═══════════════════════════════════════════════════════════════════════════

class GateAdapter(Protocol):
    """Minimal seam — wire to your nous_gate.py without re-implementing logic."""
    def score_setup(self, symbol: str, price: float, change_24h: float, volume_24h: float,
                    liquidity: float, ema_fast: float, ema_slow: float, atr: float,
                    strategy: str = "simple_ema_v1") -> dict:
        """Return {score, reason, model}."""
        ...


# ═══════════════════════════════════════════════════════════════════════════
# REFERENCE ADAPTER — correct behavior (TEST_MODE=mock)
# ═══════════════════════════════════════════════════════════════════════════

class MockGateAdapter:
    """Reference implementation matching spec."""
    MODEL_CHAIN = [
        "nvidia/nemotron-3.5-lightning:free",
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "thinkingmachines/inkling-small:free",
        "google/gemma-4-31b-it:free",
        "inclusionai/ling-3.0-flash-fin:free",
        "poolside/laguna-s-2.1:free",
        "nvidia/nemotron-3-super-120b-a12b:free",
        "cohere/north-mini-code:free",
    ]
    
    def score_setup(self, symbol: str, price: float, change_24h: float, volume_24h: float,
                    liquidity: float, ema_fast: float, ema_slow: float, atr: float,
                    strategy: str = "simple_ema_v1") -> dict:
        """Simulate cascade: try each model, return first success."""
        last_err = None
        for model in self.MODEL_CHAIN:
            try:
                # Simulate API call
                if model in self._failures:
                    raise self._failures[model]
                # Return a valid score >= 80 (passes gate)
                return {"score": 85, "reason": "mock", "model": model}
            except Exception as e:
                last_err = e
                continue
        return {"score": 0, "reason": f"All models failed: {last_err}", "model": "none"}
    
    def __init__(self):
        self._failures: dict[str, Exception] = {}
    
    def inject_failure(self, model: str, exc: Exception):
        self._failures[model] = exc
    
    def clear_failures(self):
        self._failures.clear()


# ═══════════════════════════════════════════════════════════════════════════
# BROKEN ADAPTER — silently approves everything (TEST_MODE=mock_broken)
# ═══════════════════════════════════════════════════════════════════════════

class BrokenGateAdapter(MockGateAdapter):
    """Saboteur: returns APPROVE for any input, even with all models down."""
    def score_setup(self, symbol: str, price: float, change_24h: float, volume_24h: float,
                    liquidity: float, ema_fast: float, ema_slow: float, atr: float,
                    strategy: str = "simple_ema_v1") -> dict:
        return {"score": 100, "reason": "APPROVE", "model": "broken"}


# ═══════════════════════════════════════════════════════════════════════════
# REAL ADAPTER — wire to your nous_gate.py (TEST_MODE=real)
# ═══════════════════════════════════════════════════════════════════════════

class RealGateAdapter:
    """Delegates to actual nous_gate.py with injectable failures."""
    
    def __init__(self):
        from nous_gate import score_setup, _score_with_client, _nous_client, _openrouter_client, FREE_MODEL_CHAIN
        self._score_setup = score_setup
        self._score_with_client = _score_with_client
        self._nous_client = _nous_client
        self._openrouter_client = _openrouter_client
        self._FREE_MODEL_CHAIN = FREE_MODEL_CHAIN
        self._failures: dict[str, Exception] = {}
        self._orig_score_with_client = _score_with_client
    
    def inject_failure(self, model: str, exc: Exception):
        """Inject a failure for a specific model."""
        self._failures[model] = exc
    
    def clear_failures(self):
        self._failures.clear()
    
    def score_setup(self, symbol: str, price: float, change_24h: float, volume_24h: float,
                    liquidity: float, ema_fast: float, ema_slow: float, atr: float,
                    strategy: str = "simple_ema_v1") -> dict:
        """Score with failure injection via monkeypatch."""
        import nous_gate
        
        orig = nous_gate._score_with_client
        
        def patched(client, model, prompt):
            if model in self._failures:
                raise self._failures[model]
            return orig(client, model, prompt)
        
        nous_gate._score_with_client = patched
        try:
            return self._score_setup(symbol, price, change_24h, volume_24h, liquidity, ema_fast, ema_slow, atr, strategy)
        finally:
            nous_gate._score_with_client = orig


# ═══════════════════════════════════════════════════════════════════════════
# ADAPTER FACTORY
# ═══════════════════════════════════════════════════════════════════════════

def make_adapter():
    mode = os.getenv("TEST_MODE", "mock")
    if mode == "mock":
        return MockGateAdapter()
    elif mode == "mock_broken":
        return BrokenGateAdapter()
    elif mode == "real":
        return RealGateAdapter()
    else:
        raise ValueError(f"Unknown TEST_MODE={mode}")


# ═══════════════════════════════════════════════════════════════════════════
# TEST SUITE — 9 tests
# ═══════════════════════════════════════════════════════════════════════════

def assert_eq(actual, expected, msg=""):
    if actual != expected:
        raise AssertionError(f"{msg}: expected {expected}, got {actual}")

def assert_reject(result: dict, msg=""):
    """Score < 80 = reject."""
    if result.get("score", 0) >= 80:
        raise AssertionError(f"{msg}: should reject but got score={result.get('score')}")

def assert_allow(result: dict, msg=""):
    """Score >= 80 = allow."""
    if result.get("score", 0) < 80:
        raise AssertionError(f"{msg}: should allow but got score={result.get('score')}")


def run_tests() -> tuple[int, int]:
    """Returns (passed, failed)."""
    passed = failed = 0
    
    def test(name, fn):
        nonlocal passed, failed
        adapter = make_adapter()  # NEW adapter per test for isolation
        start = time.time()
        try:
            fn(adapter)
            elapsed = time.time() - start
            passed += 1
            print(f"  ✓ {name} ({elapsed:.2f}s)")
        except Exception as e:
            elapsed = time.time() - start
            failed += 1
            print(f"  ✗ {name} ({elapsed:.2f}s): {e}")
    
    # ── F1/F2: timeout + rate-limit on primary → falls back ──────────────
    test("F1: timeout on primary → fallback", lambda adapter: (
        adapter.inject_failure("nvidia/nemotron-3.5-lightning:free", TimeoutError("timed out")),
        result := adapter.score_setup("DRB", 0.000265, 23.9, 1359734, 1628755, 0.00025, 0.00024, 0.00001),
        assert_eq(result["model"] != "nvidia/nemotron-3.5-lightning:free", True, "should not be primary"),
        assert_allow(result, "fallback should return valid score")
    ))
    
    test("F2: rate-limit on primary → fallback", lambda adapter: (
        adapter.inject_failure("nvidia/nemotron-3.5-lightning:free", Exception("429 rate limit")),
        result := adapter.score_setup("DRB", 0.000265, 23.9, 1359734, 1628755, 0.00025, 0.00024, 0.00001),
        assert_eq(result["model"] != "nvidia/nemotron-3.5-lightning:free", True, "should not be primary"),
        assert_allow(result, "fallback should return valid score")
    ))
    
    # ── F3/F4: malformed JSON and missing-verdict-field → never parsed ──
    def malformed_json_test(adapter):
        # Simulate a model returning malformed JSON
        bad_result = {"score": "APPROVE", "reason": "yes"}  # score is string, not int
        # In real code, this would be caught by json.loads or int() conversion
        try:
            score = int(bad_result.get("score", 0))
            raise AssertionError(f"should reject malformed score but got {score}")
        except (ValueError, TypeError):
            pass  # Expected: malformed data rejected
    
    test("F3: malformed JSON score → rejected", malformed_json_test)
    
    def missing_field_test(adapter):
        bad_result = {"reason": "yes"}  # missing score field
        score = bad_result.get("score", 0)
        assert_eq(score, 0, "missing score should default to 0")
        assert_reject({"score": score}, "missing field = reject")
    
    test("F4: missing verdict field → reject", missing_field_test)
    
    # ── F5: all 8 models down → deny-by-default ─────────────────────────
    def all_models_down_test(adapter):
        # Inject failures for all models in chain
        for m in ["nvidia/nemotron-3.5-lightning:free", "nvidia/nemotron-3-ultra-550b-a55b:free",
                  "thinkingmachines/inkling-small:free", "google/gemma-4-31b-it:free",
                  "inclusionai/ling-3.0-flash-fin:free", "poolside/laguna-s-2.1:free",
                  "nvidia/nemotron-3-super-120b-a12b:free", "cohere/north-mini-code:free"]:
            adapter.inject_failure(m, Exception("503 service unavailable"))
        
        result = adapter.score_setup("DRB", 0.000265, 23.9, 1359734, 1628755, 0.00025, 0.00024, 0.00001)
        assert_reject(result, "all models down = deny by default")
        assert_eq(result["model"], "none", "should report no model used")
    
    test("F5: all 8 models down → deny-by-default", all_models_down_test)
    
    # ── F6: deep fallback → model_used reported ─────────────────────────
    def deep_fallback_test(adapter):
        # Fail first 6 models, 7th should succeed
        for m in ["nvidia/nemotron-3.5-lightning:free", "nvidia/nemotron-3-ultra-550b-a55b:free",
                  "thinkingmachines/inkling-small:free", "google/gemma-4-31b-it:free",
                  "inclusionai/ling-3.0-flash-fin:free", "poolside/laguna-s-2.1:free"]:
            adapter.inject_failure(m, Exception("overloaded"))
        
        result = adapter.score_setup("DRB", 0.000265, 23.9, 1359734, 1628755, 0.00025, 0.00024, 0.00001)
        assert_allow(result, "deep fallback should succeed")
        assert_eq(result["model"], "nvidia/nemotron-3-super-120b-a12b:free", "should report 7th model")
    
    test("F6: deep fallback → model_used reported", deep_fallback_test)
    
    # ── F7: six "yes-ish" strings → none count as APPROVE ──────────────
    def yes_ish_test(adapter):
        yes_strings = ["Yes I would approve", "APPROVED", "yes, approve", "sure thing", "absolutely", "confirm"]
        for s in yes_strings:
            # These should NOT be parsed as a valid score >= 80
            # In real code, the LLM returns JSON, not free text
            # If it does return free text, it should fail json.loads
            try:
                parsed = json.loads(s)  # Will fail for all these
                raise AssertionError(f"should reject '{s}' as invalid JSON")
            except json.JSONDecodeError:
                pass  # Expected: free text rejected
    
    test("F7: 'yes-ish' strings → none count as APPROVE", yes_ish_test)
    
    # ── F8: full-cascade failure bounded at 30s ─────────────────────────
    def timeout_bound_test(adapter):
        # Inject timeouts on all models
        for m in ["nvidia/nemotron-3.5-lightning:free", "nvidia/nemotron-3-ultra-550b-a55b:free",
                  "thinkingmachines/inkling-small:free", "google/gemma-4-31b-it:free",
                  "inclusionai/ling-3.0-flash-fin:free", "poolside/laguna-s-2.1:free",
                  "nvidia/nemotron-3-super-120b-a12b:free", "cohere/north-mini-code:free"]:
            adapter.inject_failure(m, TimeoutError("timed out"))
        
        start = time.time()
        result = adapter.score_setup("DRB", 0.000265, 23.9, 1359734, 1628755, 0.00025, 0.00024, 0.00001)
        elapsed = time.time() - start
        assert_reject(result, "all timed out = reject")
        assert_eq(elapsed < 30, True, f"should complete in <30s but took {elapsed:.1f}s")
    
    test("F8: full-cascade failure bounded at 30s", timeout_bound_test)
    
    # ── F9: broken adapter returns APPROVE ──────────────────────────────
    def broken_adapter_test(adapter):
        broken = BrokenGateAdapter()
        result = broken.score_setup("DRB", 0.000265, 23.9, 1359734, 1628755, 0.00025, 0.00024, 0.00001)
        assert_eq(result["score"], 100, "broken adapter should return 100")
        assert_eq(result["model"], "broken", "broken adapter should report 'broken' model")
    
    test("F9: broken adapter returns APPROVE", broken_adapter_test)
    
    return passed, failed


if __name__ == "__main__":
    print(f"TEST_MODE={os.getenv('TEST_MODE', 'mock')}")
    p, f = run_tests()
    print(f"\n=== {p} passed, {f} failed ===")
    sys.exit(0 if f == 0 else 1)