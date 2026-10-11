#!/usr/bin/env python3
"""
Tests for the four-source price chain: consensus, circuit breaker,
disagreement journal, timeout budgets, fail-closed.

Stubbed-fetcher tests are deterministic and run anywhere. The live smoke test
skips gracefully when gmgn-cli is absent.

Run: python3 copy_trader/test_price_chain.py   (use the project .venv)
"""
from __future__ import annotations

import json
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from copy_trader import price_chain as pc  # noqa: E402
from copy_trader.price_chain import (  # noqa: E402
    PriceResult, _Breaker, fetch_price_consensus, PRICE_OK, PRICE_NO_POOL,
    PRICE_NETWORK, PRICE_HTTP_5XX,
)

TOKEN = "0x4200000000000000000000000000000000000006"  # WETH


def _install_fetchers(mapping: dict):
    """Replace price_chain._FETCHERS with fixed results keyed by source name."""
    real = dict(pc._FETCHERS)
    pc._FETCHERS = {name: (lambda tok, _r=r: _r) for name, r in mapping.items()}
    return real


def _restore_fetchers(real):
    pc._FETCHERS = real


# --- consensus --------------------------------------------------------------
def test_consensus_median_resists_outlier():
    real = _install_fetchers({
        "gmgn": PriceResult(price=100.0, reason=PRICE_OK),
        "geckoterminal": PriceResult(price=101.0, reason=PRICE_OK),
        "dexscreener": PriceResult(price=9999.0, reason=PRICE_OK),  # bad outlier
        "kyberswap": PriceResult(reason=PRICE_NO_POOL),
    })
    try:
        res, diag = fetch_price_consensus(TOKEN, breakers={})
    finally:
        _restore_fetchers(real)
    # median of [100,101,9999] = 101 — the outlier does not drag it.
    assert res.ok and res.price is not None and abs(res.price - 101.0) < 1e-9, res
    assert set(diag["used"]) == {"gmgn", "geckoterminal", "dexscreener"}
    print(f"[PASS] consensus median resists outlier -> {res.price} (used {diag['used']})")


def test_fail_closed_when_all_fail():
    real = _install_fetchers({
        "gmgn": PriceResult(reason=PRICE_NETWORK, detail="x"),
        "geckoterminal": PriceResult(reason=PRICE_NO_POOL),
        "dexscreener": PriceResult(reason=PRICE_NETWORK, detail="x"),
        "kyberswap": PriceResult(reason=PRICE_NO_POOL),
    })
    try:
        res, diag = fetch_price_consensus(TOKEN, breakers={})
    finally:
        _restore_fetchers(real)
    assert not res.ok, "must fail closed when no source returns a price"
    assert res.price is None, "fail-closed must carry price=None, never 0.0"
    assert res.reason == PRICE_NO_POOL, f"prefer genuine no_pool, got {res.reason}"
    assert diag["used"] == []
    print(f"[PASS] fail-closed when all fail -> reason={res.reason} price={res.price}")


def test_zero_price_is_not_a_quote():
    """A source returning price=0.0 must be treated as no-price, not ok."""
    real = _install_fetchers({
        "gmgn": PriceResult(price=0.0, reason=PRICE_OK),  # the GMGN string-"0" bug
        "geckoterminal": PriceResult(price=50.0, reason=PRICE_OK),
        "dexscreener": PriceResult(reason=PRICE_NO_POOL),
        "kyberswap": PriceResult(reason=PRICE_NO_POOL),
    })
    try:
        res, diag = fetch_price_consensus(TOKEN, breakers={})
    finally:
        _restore_fetchers(real)
    # price=0.0 is not .ok (PriceResult.ok requires price>0), so it is excluded.
    assert "gmgn" not in diag["used"], "0.0 price must not count as a usable quote"
    assert res.ok and abs(res.price - 50.0) < 1e-9, res
    print("[PASS] price=0.0 excluded from consensus -> 50.0 from gecko only")


# --- circuit breaker --------------------------------------------------------
def test_breaker_trips_after_threshold():
    br = _Breaker()
    t = 1000.0
    for _ in range(pc.BREAKER_FAIL_THRESHOLD):
        br.record_failure(t)
    assert br.state == "open", f"expected open after {pc.BREAKER_FAIL_THRESHOLD} fails"
    assert not br.allow(t), "open breaker must refuse the source"
    print(f"[PASS] breaker trips open after {pc.BREAKER_FAIL_THRESHOLD} consecutive fails")


def test_breaker_half_open_after_cooldown():
    br = _Breaker()
    t = 1000.0
    for _ in range(pc.BREAKER_FAIL_THRESHOLD):
        br.record_failure(t)
    assert br.state == "open"
    # Before cooldown: refused.
    assert not br.allow(t + pc.BREAKER_COOLDOWN_S - 1)
    # After cooldown: one half-open probe allowed.
    assert br.allow(t + pc.BREAKER_COOLDOWN_S + 1)
    assert br.state == "half_open"
    # A success closes it again.
    br.record_success()
    assert br.state == "closed" and br.allow(t + 1)
    print("[PASS] breaker half-opens after cooldown, closes on success")


def test_open_breaker_source_is_skipped_in_chain():
    real = _install_fetchers({
        "gmgn": PriceResult(price=100.0, reason=PRICE_OK),
        "geckoterminal": PriceResult(price=100.5, reason=PRICE_OK),
        "dexscreener": PriceResult(price=101.0, reason=PRICE_OK),
        "kyberswap": PriceResult(price=101.5, reason=PRICE_OK),
    })
    try:
        # Pre-trip the gmgn breaker open.
        gmgn_br = _Breaker()
        now = 1000.0
        for _ in range(pc.BREAKER_FAIL_THRESHOLD):
            gmgn_br.record_failure(now)
        breakers = {"gmgn": gmgn_br}
        # now_fn frozen so cooldown never elapses during the call.
        res, diag = fetch_price_consensus(TOKEN, breakers=breakers, now_fn=lambda: now)
    finally:
        _restore_fetchers(real)
    assert "gmgn" in diag["skipped_breaker"], "open breaker source must be skipped"
    assert res.ok and res.price is not None
    print(f"[PASS] open-breaker source skipped -> median {res.price} from remaining 3")


# --- disagreement journal ---------------------------------------------------
def test_disagreement_journal_records_wide_spread():
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "_test_disagree.jsonl")
    real_journal = pc.DISAGREEMENT_JOURNAL
    real_thresh = pc.DISAGREEMENT_THRESHOLD
    pc.DISAGREEMENT_JOURNAL = tmp
    pc.DISAGREEMENT_THRESHOLD = 0.02
    if os.path.exists(tmp):
        os.remove(tmp)
    try:
        real = _install_fetchers({
            "gmgn": PriceResult(price=100.0, reason=PRICE_OK),
            "geckoterminal": PriceResult(price=110.0, reason=PRICE_OK),  # 10% spread
            "dexscreener": PriceResult(reason=PRICE_NO_POOL),
            "kyberswap": PriceResult(reason=PRICE_NO_POOL),
        })
        try:
            fetch_price_consensus(TOKEN, breakers={})
        finally:
            _restore_fetchers(real)
        assert os.path.exists(tmp), "wide disagreement must be journaled"
        with open(tmp) as f:
            rec = json.loads(f.readline())
        assert rec["spread_pct"] > 2.0, rec
        assert "gmgn" in rec["quotes"] and "geckoterminal" in rec["quotes"]
        print(f"[PASS] disagreement journaled: spread {rec['spread_pct']}%")
    finally:
        pc.DISAGREEMENT_JOURNAL = real_journal
        pc.DISAGREEMENT_THRESHOLD = real_thresh
        if os.path.exists(tmp):
            os.remove(tmp)


def test_no_journal_when_sources_agree():
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "_test_agree.jsonl")
    real_journal = pc.DISAGREEMENT_JOURNAL
    pc.DISAGREEMENT_JOURNAL = tmp
    if os.path.exists(tmp):
        os.remove(tmp)
    try:
        real = _install_fetchers({
            "gmgn": PriceResult(price=100.0, reason=PRICE_OK),
            "geckoterminal": PriceResult(price=100.1, reason=PRICE_OK),  # 0.1% — agree
            "dexscreener": PriceResult(reason=PRICE_NO_POOL),
            "kyberswap": PriceResult(reason=PRICE_NO_POOL),
        })
        try:
            fetch_price_consensus(TOKEN, breakers={})
        finally:
            _restore_fetchers(real)
        assert not os.path.exists(tmp), "agreement must NOT be journaled"
        print("[PASS] no journal entry when sources agree")
    finally:
        pc.DISAGREEMENT_JOURNAL = real_journal
        if os.path.exists(tmp):
            os.remove(tmp)


# --- timeout budget ---------------------------------------------------------
def test_budget_exhaustion_stops_chain():
    """When the total wall-clock budget is blown before later sources run,
    those sources are skipped and flagged."""
    # A fake clock that stays at 0 for the chain-start and the first (gmgn)
    # budget check, then jumps past the budget so later sources are skipped.
    calls = {"n": 0}
    def fake_now():
        calls["n"] += 1
        return 0.0 if calls["n"] <= 2 else pc.CHAIN_TOTAL_BUDGET_S + 1
    real = _install_fetchers({
        "gmgn": PriceResult(price=100.0, reason=PRICE_OK),
        "geckoterminal": PriceResult(price=100.0, reason=PRICE_OK),
        "dexscreener": PriceResult(price=100.0, reason=PRICE_OK),
        "kyberswap": PriceResult(price=100.0, reason=PRICE_OK),
    })
    try:
        res, diag = fetch_price_consensus(TOKEN, breakers={}, now_fn=fake_now)
    finally:
        _restore_fetchers(real)
    assert diag["budget_exhausted"] is True, "budget flag must be set"
    assert diag["used"] == ["gmgn"], f"only gmgn should have run, got {diag['used']}"
    assert res.ok and abs(res.price - 100.0) < 1e-9
    print("[PASS] budget exhaustion stops the chain after first source")


# --- live smoke -------------------------------------------------------------
def test_live_smoke():
    import shutil
    if shutil.which(os.environ.get("GMGN_CLI", "gmgn-cli")) is None:
        print("[SKIP] live smoke (no gmgn-cli on PATH)")
        return
    res, diag = fetch_price_consensus(TOKEN, breakers={})
    assert res.price != 0.0, "live chain must never surface a $0.00 price"
    print(f"[PASS] live WETH: ok={res.ok} price={res.price} "
          f"used={diag['used']} reasons="
          f"{ {k: v.get('reason') for k, v in diag['sources'].items()} }")


def main() -> int:
    print("=== four-source price chain tests ===\n")
    for t in (test_consensus_median_resists_outlier,
              test_fail_closed_when_all_fail,
              test_zero_price_is_not_a_quote,
              test_breaker_trips_after_threshold,
              test_breaker_half_open_after_cooldown,
              test_open_breaker_source_is_skipped_in_chain,
              test_disagreement_journal_records_wide_spread,
              test_no_journal_when_sources_agree,
              test_budget_exhaustion_stops_chain,
              test_live_smoke):
        t()
    print("\nGREEN — consensus, breaker, journal, budgets, fail-closed all hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
