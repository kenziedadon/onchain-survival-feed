#!/usr/bin/env python3
"""
Red-first tests for the price-fetch failure paths.

These tests exercise the ACTUAL exception path by injecting at the HTTP layer
(`requests.get`), not by patching `fetch_token_price`. Patching the wrapper
certifies nothing about the function's internals.

Expected behavior on current (unfixed) poller.py:
  - test_throttled_close_keeps_position_open : FAILS (position is erased)
  - test_throttled_max_hold_keeps_position_open: FAILS (position is erased)
  - test_throttled_entry_returns_no_position   : FAILS (returns None, not a
                                                  retryable result)
  - test_price_result_distinguishes_failure    : FAILS (no PriceResult type)

Run: python -m copy_trader.test_price_failure
"""
from __future__ import annotations

import sys
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import requests

from copy_trader.poller import (
    CopyTraderPoller,
    SwapEvent,
    PaperPosition,
    PollerState,
    open_paper_position,
)

WALLET = "0xd569e570b94265e5ebc7df1990f99ee68d3f60a1"
TOKEN = "0xb20000000000000000000070f6c1a66d7c1e4d01"


class _Resp:
    """Minimal stand-in for requests.Response that raises on raise_for_status."""

    def __init__(self, status_code: int):
        self.status_code = status_code

    def raise_for_status(self):
        raise requests.HTTPError(f"{self.status_code} Server Error")


def _http_429(*args, **kwargs):
    """Simulate a GeckoTerminal throttle: HTTP 429."""
    return _Resp(429)


def _price_body(price: str):
    """A well-formed GeckoTerminal token response."""
    class _OK:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": {
                    "attributes": {
                        "price_usd": price,
                        "symbol": "SPIKE",
                    }
                }
            }
    return _OK()


class _GmgnProc:
    """Stand-in for the subprocess result gmgn-cli returns."""
    def __init__(self, returncode, stdout, stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _gmgn_price(price: str):
    """Deterministic healthy GMGN token-info output (no live binary/network)."""
    import json as _json
    body = _json.dumps({"price": {"price": price, "price_24h": price}})
    return lambda argv, **kw: _GmgnProc(0, body)


def _gmgn_throttled(*args, **kwargs):
    """GMGN unavailable: non-zero exit (burst-ban / quota), the real failure mode."""
    return _GmgnProc(1, "", "RATE_LIMIT_BANNED")


def _make_position(hours_old: float = 2.0) -> PaperPosition:
    ts = int((datetime.now(timezone.utc) - timedelta(hours=hours_old)).timestamp())
    return PaperPosition(
        pick_id=f"{WALLET}:0xdead",
        wallet_address=WALLET,
        wallet_label="test",
        token=TOKEN,
        entry_price=0.0007,
        paper_notional_usd=5.0,
        quantity=5.0 / 0.0007,
        cost_bps=900.0,
        open_time=datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
        open_tx_hash="0xdead",
        source_amount_in=0.0005,
        source_token_in="ETH",
    )


def _make_buy_swap() -> SwapEvent:
    return SwapEvent(
        tx_hash="0xabc",
        wallet_address=WALLET,
        wallet_label="test",
        token_in="ETH",
        token_out=TOKEN,
        amount_in=0.0005,
        amount_out=1000.0,
        timestamp=int(time.time()),
        is_buy=True,
    )


def _make_sell_swap(older_than_open_by_hours: float = 0.0) -> SwapEvent:
    return SwapEvent(
        tx_hash="0xdef",
        wallet_address=WALLET,
        wallet_label="test",
        token_in=TOKEN,           # the token being SOLD
        token_out="ETH",
        amount_in=1000.0,
        amount_out=0.0005,
        timestamp=int(time.time()) + int(older_than_open_by_hours * 3600),
        is_buy=False,
    )


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------

def _make_poller(state: PollerState) -> CopyTraderPoller:
    """Build a poller without running __init__ (which reads config/state files)."""
    poller = CopyTraderPoller.__new__(CopyTraderPoller)
    poller.state = state
    poller.paper_notional = 5.0
    poller.poll_interval = 60
    poller.rotation_enabled = False
    poller.min_trades_before_cut = 20
    poller.max_hold_seconds = 3600
    poller.logger = __import__("logging").getLogger("test")
    # journal_path is normally set in __init__ (a str); the close path's
    # _append_journal reads it. Point it at a throwaway file so a real close
    # does not crash. Kept a str to match __init__'s signature.
    import tempfile
    poller.journal_path = os.path.join(
        tempfile.mkdtemp(prefix="poller-journal-"), "j.jsonl")
    return poller


def test_throttled_close_keeps_position_open(tmp: Path) -> bool:
    """A price failure on the mirrored-exit must NOT erase the position.

    Dual-source path: BOTH GMGN and GeckoTerminal are throttled, so no price is
    available and the position must survive to close next cycle. Injects at both
    real layers (subprocess + requests.get), not by patching the wrapper.
    """
    state = PollerState()
    pos = _make_position(hours_old=2.0)
    state.open_positions[pos.pick_id] = pos

    poller = _make_poller(state)

    sell = _make_sell_swap()

    with patch("copy_trader.poller.requests.get", side_effect=_http_429), \
         patch("subprocess.run", side_effect=_gmgn_throttled):
        poller.process_swaps([sell])

    still_open = pos.pick_id in state.open_positions
    print(f"  position still open after throttled close: {still_open}")
    return still_open


def test_throttled_max_hold_keeps_position_open(tmp: Path) -> bool:
    """A price failure during max-hold must NOT erase the position.

    Dual-source: both sources throttled -> no price -> position survives.
    """
    state = PollerState()
    pos = _make_position(hours_old=48.0)   # well past any max_hold
    state.open_positions[pos.pick_id] = pos

    poller = _make_poller(state)
    poller.max_hold_seconds = 3600          # 1h — position is overdue

    with patch("copy_trader.poller.requests.get", side_effect=_http_429), \
         patch("subprocess.run", side_effect=_gmgn_throttled):
        poller.check_max_hold()

    still_open = pos.pick_id in state.open_positions
    print(f"  position still open after throttled max_hold: {still_open}")
    return still_open


def test_price_result_distinguishes_failure() -> bool:
    """fetch_token_price_result must return a typed failure, not a price.

    Dual-source: both throttled -> typed non-ok result with price None.
    Injects at both real layers.
    """
    from copy_trader import poller as p

    has_type = hasattr(p, "PriceResult") or hasattr(p, "fetch_token_price_result")
    if not has_type:
        print("  no PriceResult / fetch_token_price_result in poller module")
        return False

    fn = getattr(p, "fetch_token_price_result", None) or p.fetch_token_price
    with patch("copy_trader.poller.requests.get", side_effect=_http_429), \
         patch("subprocess.run", side_effect=_gmgn_throttled):
        r = fn(TOKEN)
    reason = getattr(r, "reason", None) or (r.get("reason") if isinstance(r, dict) else None)
    price = getattr(r, "price", None) or (r.get("price") if isinstance(r, dict) else None)
    print(f"  typed result -> reason={reason!r} price={price!r}")
    return reason is not None and price is None


def test_successful_close_still_works(tmp: Path) -> bool:
    """Control: with a healthy price, the close path must still function.

    Guards against a fix that leaves positions open forever. GMGN (primary)
    returns a healthy price deterministically; no live binary/network.
    """
    state = PollerState()
    pos = _make_position(hours_old=2.0)
    state.open_positions[pos.pick_id] = pos

    poller = _make_poller(state)

    sell = _make_sell_swap()
    journal = tmp / "closes.jsonl"

    with patch("copy_trader.poller.requests.get", return_value=_price_body("0.0008")), \
         patch("subprocess.run", side_effect=_gmgn_price("0.0008")), \
         patch.object(poller, "_append_journal", lambda rec: None):
        poller.process_swaps([sell])

    closed = pos.pick_id not in state.open_positions
    print(f"  position closed on healthy price: {closed}")
    return closed


def main() -> int:
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="poller-pricefail-"))

    tests = [
        ("throttled_close_keeps_position_open", lambda: test_throttled_close_keeps_position_open(tmp)),
        ("throttled_max_hold_keeps_position_open", lambda: test_throttled_max_hold_keeps_position_open(tmp)),
        ("price_result_distinguishes_failure", lambda: test_price_result_distinguishes_failure()),
        ("successful_close_still_works", lambda: test_successful_close_still_works(tmp)),
    ]

    print("=" * 70)
    print("RED-FIRST: price-fetch failure paths")
    print("=" * 70)

    failures = []
    for name, fn in tests:
        print(f"\n[{name}]")
        try:
            ok = fn()
        except Exception as exc:
            ok = False
            print(f"  EXCEPTION: {type(exc).__name__}: {exc}")
        print(f"  => {'PASS' if ok else 'FAIL'}")
        if not ok:
            failures.append(name)

    print("\n" + "=" * 70)
    if failures:
        print(f"RED as expected — {len(failures)} failing test(s) against current code:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("GREEN — all tests pass. Fix is in place.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
