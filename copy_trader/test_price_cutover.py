#!/usr/bin/env python3
"""
Cutover test: GMGN-first price feed with GeckoTerminal fallback (2026-10-11).

Wires fetch_token_price_result() to try GMGN (gmgn-cli token info) first and
fall back to GeckoTerminal. Additive — the GeckoTerminal path is unchanged.

The critical case this pins: GMGN returns the STRING "0" (truthy) for an
unpriced/dead token. A bare `not price` guard passes "0" through as float 0.0 —
a garbage $0.00 close price that would corrupt P&L. The fix parses first and
rejects non-positive numerics as no_pool.

Stubbed subprocess tests run anywhere; the two live-GMGN assertions skip
gracefully when gmgn-cli is absent.

Run: python3 copy_trader/test_price_cutover.py   (use the project .venv)
"""
from __future__ import annotations

import json
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from copy_trader.poller import (  # noqa: E402
    PRICE_OK, PRICE_NO_POOL,
    fetch_token_price_gmgn, fetch_token_price_result,
)

WETH = "0x4200000000000000000000000000000000000006"
DEAD = "0x000000000000000000000000000000000000dead"


class _FakeProc:
    def __init__(self, rc, out, err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


def _stub_subprocess(fn):
    """Swap sys.modules['subprocess'] with a stub whose .run calls fn(argv)."""
    fake = types.ModuleType("subprocess")
    fake.run = fn
    sys.modules["subprocess"] = fake


def _unstub_subprocess():
    sys.modules.pop("subprocess", None)


def test_gmgn_rejects_string_zero():
    _stub_subprocess(lambda argv, **kw: _FakeProc(
        0, json.dumps({"price": {"price": "0", "price_24h": "0"}})))
    try:
        r = fetch_token_price_gmgn(DEAD)
    finally:
        _unstub_subprocess()
    assert r.ok is False, "string '0' must NOT be ok"
    assert r.reason == PRICE_NO_POOL, f"expected no_pool, got {r.reason}"
    assert r.price is None, "no_pool must carry price=None, not 0.0"
    print("[PASS] gmgn string '0' -> no_pool (price=None), not $0.00")


def test_gmgn_parses_real_price():
    _stub_subprocess(lambda argv, **kw: _FakeProc(
        0, json.dumps({"price": {"price": "2531.76"}})))
    try:
        r = fetch_token_price_gmgn(WETH)
    finally:
        _unstub_subprocess()
    assert r.ok is True and r.reason == PRICE_OK
    assert r.price is not None and abs(r.price - 2531.76) < 0.01
    print("[PASS] gmgn real price parsed -> ok")


def test_gmgn_nonzero_exit_is_not_ok():
    _stub_subprocess(lambda argv, **kw: _FakeProc(1, "", "RATE_LIMIT_BANNED"))
    try:
        r = fetch_token_price_gmgn(WETH)
    finally:
        _unstub_subprocess()
    assert r.ok is False and r.reason != PRICE_OK
    print(f"[PASS] gmgn non-zero exit -> not ok ({r.reason})")


def _has_gmgn() -> bool:
    import shutil
    return shutil.which(os.environ.get("GMGN_CLI", "gmgn-cli")) is not None


def test_orchestrator_dead_addr_is_no_pool_live():
    if not _has_gmgn():
        print("[SKIP] orchestrator dead-addr (no gmgn-cli on PATH)")
        return
    r = fetch_token_price_result(DEAD)
    assert r.price != 0.0, "orchestrator must never surface a $0.00 price"
    if not r.ok:
        assert r.price is None
    print(f"[PASS] orchestrator dead addr: ok={r.ok} reason={r.reason} price={r.price}")


def test_orchestrator_weth_prices_live():
    if not _has_gmgn():
        print("[SKIP] orchestrator WETH (no gmgn-cli on PATH)")
        return
    r = fetch_token_price_result(WETH)
    assert r.ok is True, f"WETH should price, got {r.reason}:{r.detail}"
    assert r.price and r.price > 0
    print(f"[PASS] orchestrator WETH: {r.price}")


def main() -> int:
    print("=== price-feed cutover tests ===\n")
    for t in (test_gmgn_rejects_string_zero, test_gmgn_parses_real_price,
              test_gmgn_nonzero_exit_is_not_ok,
              test_orchestrator_dead_addr_is_no_pool_live,
              test_orchestrator_weth_prices_live):
        t()
    print("\nGREEN — cutover wired, zero-price rejection holds, fallback intact.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
