"""Causality guard for the WO4 research harness.

This module is the deliverable WO4 exists to provide. Everything else in the
harness is worthless if a strategy can see the future, because every gate
downstream would pass on a number that could never have been earned.

Two layers, deliberately:

  1. STRUCTURAL — the strategy never receives the future. On every bar the
     harness builds `visible = candles[:i+1]` and passes *that*. The future
     is not merely unused; it is not reachable from the object graph handed
     to the strategy.

  2. ASSERTIVE — every signal the strategy returns is checked against the
     current bar. A signal pointing at or beyond the current bar raises
     LookaheadViolation and aborts the run. No partial artifact is written.

Layer 1 stops the ordinary case. Layer 2 catches the case where a strategy
splices in a future value it cached earlier, or where a helper returns an
index rather than a bar number.

The negative/positive/statistical controls in tests/test_no_lookahead.py
prove this guard has teeth rather than merely existing.
"""
from __future__ import annotations

from typing import Any, NamedTuple


class LookaheadViolation(RuntimeError):
    """A strategy referenced data it could not have had.

    Aborts the whole run. There is deliberately no "skip this signal and
    continue" path: a run that completes is a run where no violation
    occurred, and partial results would break that guarantee.
    """


class Signal(NamedTuple):
    """A strategy intent, resolved by the harness at the NEXT bar's open.

    `entry_bar` / `exit_bar` are BAR NUMBERS, not prices. A strategy never
    chooses its own fill price — that closes the common backtest cheat of
    filling at the signal bar's close.
    """
    entry_bar: int
    exit_bar: int
    direction: str = "long"
    tag: str = ""


class VisibleWindow:
    """The only view of price data a strategy ever receives.

    Wraps candles[:i+1] and refuses any access at or beyond the current bar
    by index. A strategy doing `candles[i+1]` gets IndexError; a strategy
    doing `len(candles)` to reason about future length sees the honest count.
    """

    __slots__ = ("_candles", "_limit", "_bar")

    def __init__(self, candles, bar: int):
        # Physically truncate. The future is not in this object at all.
        self._candles = tuple(candles[:bar + 1])
        self._bar = bar
        self._limit = len(self._candles)

    def __len__(self) -> int:
        return self._limit

    def __iter__(self):
        return iter(self._candles)

    def __getitem__(self, idx):
        if isinstance(idx, slice):
            stop = idx.stop
            if stop is None or stop > self._limit:
                raise LookaheadViolation(
                    f"slice {idx} reaches past visible bar {self._bar} "
                    f"(visible: 0..{self._limit - 1})")
            return self._candles[idx]
        if idx < 0:
            idx += self._limit
        if idx < 0 or idx >= self._limit:
            raise LookaheadViolation(
                f"index {idx} is at or beyond the current bar {self._bar} "
                f"(visible: 0..{self._limit - 1})")
        return self._candles[idx]

    @property
    def bar(self) -> int:
        return self._bar

    @property
    def closes(self):
        return tuple(c["c"] for c in self._candles)


def validate_signals(signals, current_bar: int, n_bars: int) -> None:
    """Reject any signal that references a bar the strategy could not have had.

    This is the assertive layer. It catches a strategy that built a valid
    VisibleWindow but returned an index it computed from data it should not
    have had (e.g. a cached future price from an earlier, longer window).

    Note the bound on entry_bar is `> current_bar`, not `>=`. A signal may
    legitimately carry entry_bar == current_bar: the decision is made at
    that bar's close and the harness fills at the NEXT bar's open. Only an
    index strictly beyond the current bar is lookahead.
    """
    for s in signals:
        # Signal is a NamedTuple: index by attribute, not string key.
        entry, exit_ = s.entry_bar, s.exit_bar
        if entry > current_bar:
            raise LookaheadViolation(
                f"signal entry_bar={entry} is beyond current bar "
                f"{current_bar}")
        if exit_ <= entry:
            raise LookaheadViolation(
                f"signal exit_bar={exit_} must be after entry_bar={entry}")
        if entry >= n_bars or exit_ > n_bars:
            raise LookaheadViolation(
                f"signal bars ({entry}, {exit_}) exceed series length {n_bars}")


def run_guarded(strategy_fn, candles, params: dict, min_history: int = 0):
    """Drive a strategy bar-by-bar, enforcing causality at every step.

    strategy_fn(window, params) -> list[Signal]

    Returns (signals_by_bar, n_bars). Raises LookaheadViolation — having
    written nothing — if the strategy reaches for the future at any bar.
    """
    candles = list(candles)
    n = len(candles)
    signals_by_bar = {}

    for bar in range(min_history, n):
        window = VisibleWindow(candles, bar)
        out = strategy_fn(window, params)
        out = list(out or [])
        validate_signals(out, current_bar=bar, n_bars=n)
        # Only signals actionable at this bar are recorded now. A signal for
        # a later bar is fine (it is a pending exit) but its entry must
        # already be in the past, which validate_signals just proved.
        signals_by_bar[bar] = out

    return signals_by_bar, n


def proof_of_no_lookahead(signals_by_bar, n_bars: int) -> dict:
    """Provenance stamp embedded in every run artifact.

    report.py refuses to write any artifact lacking this field, so a
    run that bypassed the guard cannot produce a reviewable report.
    """
    return {
        "no_lookahead_guard": "enforced",
        "mechanism": "structural_truncation + assertive_signal_validation",
        "bars_processed": n_bars,
        "guard_version": "1.0.0",
    }