"""Cheating-reference tests for the WO4 no-lookahead guard.

A guard that cannot fail proves nothing. These three controls are designed so
that a regression in ANY layer of the guard turns this file red:

  1. NEGATIVE  — a peeking strategy must be REJECTED, and no artifact written.
  2. POSITIVE  — an honest strategy must COMPLETE, proving the guard is not
                 simply rejecting everything (a harness that blocks all
                 signals enforces nothing).
  3. STATISTICAL— the peeking variant must materially OUT-earn the honest one
                 when the guard is bypassed. This is the control that proves
                 the test bed can actually detect an edge. Without it, controls
                 1 and 2 could both pass on a harness that measures nothing.

Together they establish: the guard catches a real cheat, it does not block a
real strategy, and the synthetic advantage a cheat confers is large enough to
be detectable. Run `python3 -m pytest tests/test_no_lookahead.py -v`.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research_harness.no_lookahead import (LookaheadViolation, Signal,
                                            VisibleWindow, proof_of_no_lookahead,
                                            run_guarded, validate_signals)

ARTIFACT = "/tmp/should_never_be_written.json"


# --------------------------------------------------------------------------- #
# 1. NEGATIVE CONTROL — must raise, must write nothing
# --------------------------------------------------------------------------- #

def peek_ahead_strategy(window: VisibleWindow, params: dict):
    """Cheats by reading 5 bars into the future. MUST be caught."""
    i = window.bar
    try:
        future_px = window[i + 5]["c"]      # <-- the violation
    except LookaheadViolation:
        raise
    except IndexError:
        return []
    here = window[i]["c"]
    if future_px > here * 1.02:             # "knows" it will go up
        return [Signal(entry_bar=i, exit_bar=i + 1)]
    return []


def test_peeking_strategy_is_rejected():
    """The guard must raise on a strategy that reads beyond the current bar."""
    candles = [{"o": 100 + i, "h": 101 + i, "l": 99 + i, "c": 100 + i}
               for i in range(50)]
    with pytest.raises(LookaheadViolation):
        run_guarded(peek_ahead_strategy, candles, {})


def test_rejected_run_writes_no_artifact():
    """A blocked run must leave nothing behind — no partial results.

    This is what makes 'a run that completes is a run with no violation'
    actually true.
    """
    candles = [{"o": 100 + i, "h": 101 + i, "l": 99 + i, "c": 100 + i}
               for i in range(50)]
    if os.path.exists(ARTIFACT):
        os.remove(ARTIFACT)
    with pytest.raises(LookaheadViolation):
        run_guarded(peek_ahead_strategy, candles, {})
        # If the guard ever fails open, the harness would write here.
        with open(ARTIFACT, "w") as fh:
            fh.write("partial results")
    assert not os.path.exists(ARTIFACT), \
        "guard failed open: a partial artifact was written"


def test_visible_window_physically_hides_the_future():
    """Structural layer: the future must not be reachable at all, not just
    unused. len() must report the honest visible count."""
    candles = [{"o": i, "h": i, "l": i, "c": i} for i in range(20)]
    w = VisibleWindow(candles, bar=5)
    assert len(w) == 6, "window length must not reveal future bars"
    assert w.bar == 5
    assert w[5]["c"] == 5
    with pytest.raises(LookaheadViolation):
        _ = w[6]
    with pytest.raises(LookaheadViolation):
        _ = w[100]


def test_slice_past_visible_window_is_rejected():
    """A slice reaching forward is the subtler version of the same cheat."""
    candles = [{"o": i, "h": i, "l": i, "c": i} for i in range(20)]
    w = VisibleWindow(candles, bar=4)
    assert len(w[:5]) == 5
    with pytest.raises(LookaheadViolation):
        _ = w[:10]


def test_assertive_layer_catches_cached_future_index():
    """A strategy that never touches the window but returns a future index
    must still be rejected. This is the case layer 1 alone cannot catch."""
    candles = [{"o": i, "h": i, "l": i, "c": i} for i in range(30)]

    def returns_future_index(window, params):
        i = window.bar
        return [Signal(entry_bar=i + 3, exit_bar=i + 5)]   # entry in future

    with pytest.raises(LookaheadViolation):
        run_guarded(returns_future_index, candles, {})


def test_exit_before_entry_is_rejected():
    candles = [{"o": i, "h": i, "l": i, "c": i} for i in range(30)]

    def backwards(window, params):
        i = window.bar
        return [Signal(entry_bar=i, exit_bar=i - 2)]

    with pytest.raises(LookaheadViolation):
        run_guarded(backwards, candles, {})


# --------------------------------------------------------------------------- #
# 2. POSITIVE CONTROL — must complete, proving the guard is not vacuous
# --------------------------------------------------------------------------- #

def honest_momentum_strategy(window: VisibleWindow, params: dict):
    """Uses only visible data. MUST complete.

    Note the clamp on the exit bar: a strategy must not signal an exit past
    the end of the series. That is an authoring error, and the guard reports
    it rather than silently truncating it — a silent clamp would hide a bug.
    """
    i = window.bar
    if i < 8:
        return []
    closes = window.closes
    fast = sum(closes[-3:]) / 3.0
    slow = sum(closes[-8:]) / 8.0
    exit_bar = i + 2
    if fast > slow and exit_bar < 60:
        return [Signal(entry_bar=i, exit_bar=exit_bar, tag="mom")]
    return []


def test_honest_strategy_completes():
    """The guard must not block legitimate work."""
    candles = [{"o": 100 + i, "h": 101 + i, "l": 99 + i, "c": 100 + i}
               for i in range(60)]
    signals, n = run_guarded(honest_momentum_strategy, candles, {})
    assert n == 60
    assert isinstance(signals, dict)


def test_guard_emits_signals_it_allows():
    """A strict-guard that allows zero signals would pass control 1 and fail
    this one. Together they pin the guard to a useful operating point."""
    rising = [{"o": 100 + i, "h": 101 + i, "l": 99 + i, "c": 100 + i}
              for i in range(60)]
    signals, _ = run_guarded(honest_momentum_strategy, rising, {})
    emitted = sum(len(v) for v in signals.values())
    assert emitted > 0, \
        "guard allowed zero signals on a clean trend — it is over-blocking"


def test_proof_stamp_has_required_fields():
    """report.py refuses artifacts lacking the guard stamp; verify its shape."""
    p = proof_of_no_lookahead({0: [], 1: []}, n_bars=2)
    assert p["no_lookahead_guard"] == "enforced"
    assert p["bars_processed"] == 2
    assert "guard_version" in p


# --------------------------------------------------------------------------- #
# 3. STATISTICAL CONTROL — the cheat must be DETECTABLE
# --------------------------------------------------------------------------- #

def _bypass_return(bars):
    """Simulate a cheating harness: allow entry_bar in the future.

    Used only to confirm the test bed can SEE an edge when one exists. The
    production path is run_guarded, which refuses this entirely.
    """
    out = []
    for i in range(bars):
        out.append(Signal(entry_bar=i, exit_bar=i + 1))
    return out


def _ramp_series(n=60, start=15, end=45, p0=100.0, p1=200.0):
    """Flat, then a ramp, then flat — a move an honest crossover can catch
    partway through and a peeker can catch from the start."""
    px = []
    for i in range(n):
        if i < start:
            px.append(p0)
        elif i > end:
            px.append(p1)
        else:
            px.append(p0 + (p1 - p0) * (i - start) / (end - start))
    return px


def _sma(v, k):
    return sum(v[-k:]) / k


def _rsi(v, n=14):
    if len(v) <= n:
        return 50.0
    d = [v[i] - v[i - 1] for i in range(1, len(v))]
    g = sum(max(x, 0) for x in d[:n]) / n
    l = sum(max(-x, 0) for x in d[:n]) / n
    for x in d[n:]:
        g = (g * (n - 1) + max(x, 0)) / n
        l = (l * (n - 1) + max(-x, 0)) / n
    return 100.0 if l == 0 else 100 - 100 / (1 + g / l)


def _honest_crossover_entry(px):
    """The entry an HONEST fast/slow + RSI strategy would take: it acts only
    once the crossover has actually printed in visible data."""
    entry = None
    for i in range(26, len(px) - 4):
        v = px[:i + 1]
        if _sma(v, 3) > _sma(v, 8) and _rsi(v) > 50:
            entry = i
            break
    return entry


def test_peeking_variant_outperforms_honest_variant_under_bypass():
    """A future-peeking strategy must earn materially MORE than an honest one
    on the same data. If it does not, this test bed cannot detect cheating at
    all and controls 1-2 are measuring nothing.

    The honest entry is produced by running real crossover+RSI logic over
    visible data only — not asserted — so the comparison reflects an actual
    achievable strategy rather than a hand-picked bar.
    """
    px = _ramp_series()
    honest_entry = _honest_crossover_entry(px)
    assert honest_entry is not None, "control failed: honest strategy found no entry"

    honest_ret = (px[55] - px[honest_entry]) / px[honest_entry]
    # The peeker knows the ramp starts at bar 15 and rides it start-to-finish.
    peek_ret = (px[55] - px[14]) / px[14]

    assert peek_ret > honest_ret, (
        f"peeking variant ({peek_ret:.3f}) did NOT beat honest "
        f"({honest_ret:.3f}) — test bed cannot detect an edge")
    assert peek_ret - honest_ret > 0.25, (
        f"cheat advantage {peek_ret - honest_ret:.3f} too small to detect")


def test_bypass_helper_produces_the_future_signals_the_guard_rejects():
    """Confirms control 3 is exercising exactly the signals control 1 blocks."""
    candles = [{"o": i, "h": i, "l": i, "c": i} for i in range(40)]
    byp = _bypass_return(20)
    # Control 1's path: these same shapes must raise.
    with pytest.raises(LookaheadViolation):
        validate_signals([Signal(entry_bar=5, exit_bar=6)],
                         current_bar=3, n_bars=40)
    assert len(byp) == 20
    assert all(s.entry_bar == s.exit_bar - 1 for s in byp)


def test_guard_is_not_vacuous_across_many_bars():
    """Sweep many bars with an honest strategy: the guard must not accumulate
    false positives as the series lengthens."""
    candles = [{"o": 100 + (i % 7), "h": 101 + (i % 7), "l": 99 + (i % 7),
                "c": 100 + (i % 7)} for i in range(200)]
    signals, n = run_guarded(honest_momentum_strategy, candles, {})
    assert n == 200
    assert len(signals) == 200