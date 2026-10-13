"""Tests for the exit-pending fix (WO6 item 2) — proven at the junctions, not
just the pieces. Run: python test_exit_pending.py

Junction proofs, all driven through the real poller cycle path where the
review demanded:

  (a) The sell-matching branch calls the park when its price fetch comes back
      empty (the exact junction Brian died at). Zero silent failures.
  (b) A max-hold price failure now logs + parks (the no-silent-path rule).
  (c) Calendar-time days, not call-count days: the counter is DERIVED from
      elapsed wall-clock since the FIRST failure (floor of elapsed days plus
      one), never bumped on each call, so a 60s poll cadence can never run
      days 2, 3, 4 ... in minutes.
  (d) Compound junction (Blocker-1): across two real run_once cycles, a
      matched sell whose price fetch was None is PARKED (sell already in
      seen hashes), and the NEXT cycle's retry pass re-fetches the price and
      closes through the real cycle path — the running daemon, not a direct
      method call.
  (e) Escalation is loud once at the 3-day threshold and is NOT re-logged on
      the same simulated day.
  (f) A successful max-hold close clears a stale pending record.
"""
import json
import logging
import os
import sys
import tempfile
import datetime

sys.path.insert(0, "/home/kenzodadon/base-trend-bot/copy_trader")

import poller  # noqa: E402  -> module-level fetch_token_price, patched below.

from poller import CopyTraderPoller, PaperPosition, SwapEvent  # noqa: E402

_POLLER_MODULE = sys.modules["poller"]
_ORIG_FETCH = _POLLER_MODULE.fetch_token_price
WALLET = "0x347ffc6db9acc54ac2019795173b9599e8b82bd9"
BASE_TS = 1791818829  # 2026-10-12 (simulated clock)


def _make_poller(tmpdir, now_ts=BASE_TS):
    config_path = os.path.join(tmpdir, "watched.yaml")
    with open(config_path, "w") as f:
        f.write(
            "paper_notional_usd: 25.0\n"
            "wallets:\n"
            f"  - address: \"{WALLET}\"\n"
        )
    state_path = os.path.join(tmpdir, "state.json")
    journal_path = os.path.join(tmpdir, "closes.jsonl")
    p = CopyTraderPoller(config_path, state_path, journal_path)
    p.now_fn = lambda: datetime.datetime.fromtimestamp(now_ts, datetime.timezone.utc)
    return p, state_path, journal_path


def _patch_price(poller, fake=None):
    """Point the MODULE-level fetch_token_price the close path calls.

    process_swaps/check_max_hold/read the module global (not the instance),
    so instance-level patching never reaches them. Pass fake=None to fail
    closed. Returns the previous binding (restore after the test).
    """
    _POLLER_MODULE.fetch_token_price = fake if fake is not None else (lambda token: None)  # noqa: E501
    return _ORIG_FETCH


def _restore_price(saved):
    _POLLER_MODULE.fetch_token_price = saved


def _seed_position(poller, pick_id):
    poller.state.open_positions[pick_id] = PaperPosition(
        pick_id=pick_id,
        wallet_address=WALLET,
        wallet_label="pe___lu",
        token="0xb200000000000000000000aaf5c1ea536c778701",
        entry_price=4.1853603325e-06,
        paper_notional_usd=25.0,
        quantity=5973201.352789379,
        cost_bps=900.0,
        open_time="2026-10-12T15:26:27+00:00",
        open_tx_hash="0x7a19f4c753a99e56485eb3e666ae3e76877c1590339a142468b12ca7b7346a3d",
        source_amount_in=0.025,
        source_token_in="ETH",
    )


def _buy_swap(timestamp):
    return SwapEvent(
        tx_hash="0x7a19f4c753a99e56485eb3e666ae3e76877c1590339a142468b12ca7b7346a3d",
        wallet_address=WALLET,
        wallet_label="pe___lu",
        token_in="ETH",
        token_out="0xb200000000000000000000aaf5c1ea536c778701",
        amount_in=0.025,
        amount_out=0.0,
        timestamp=timestamp,
        is_buy=True,
    )


def _sell_swap(timestamp):
    return SwapEvent(
        tx_hash="0x0a5f7ff5ef054ca82417b275acf7fd0e88a1f0317ff059e8d82a5a8063f08ac1",
        wallet_address=WALLET,
        wallet_label="pe___lu",
        token_in="0xb200000000000000000000aaf5c1ea536c778701",
        token_out="ETH",
        amount_in=0.0,
        amount_out=0.025604226748585555,
        timestamp=timestamp,
        is_buy=False,
    )


class _Capture:
    """Per-test log capture on the poller's logger; behaves like a list."""

    def __init__(self):
        self.msgs = []
        self.handler = logging.Handler()
        self.handler.emit = lambda record: self.msgs.append(record.getMessage())
        self.logger = logging.getLogger("copy_trader")
        self.logger.addHandler(self.handler)

    def __iter__(self):
        return iter(self.msgs)

    def __len__(self):
        return len(self.msgs)

    def __getitem__(self, idx):
        return self.msgs[idx]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.logger.removeHandler(self.handler)
        self.handler.close()


def test_run_once_parks_and_retry_closes_through_real_cycle():
    # Blocker-1 compound junction: a matched sell whose price fetch returns
    # None -> park (sell already in seen hashes, so process_swaps would never
    # meet it again). The next run_once cycle's retry pass re-fetches the
    # price and closes through the real cycle path. Zero silent failures.
    with tempfile.TemporaryDirectory() as tmp:
        poller, _, journal_path = _make_poller(tmp)
        pick_id = "0x347ffc6db9acc54ac2019795173b9599e8b82bd9:0x7a19f4c7"
        _seed_position(poller, pick_id)
        sell = _sell_swap(BASE_TS)
        poller.state.seen_tx_hashes[sell.tx_hash] = sell.timestamp

        call = {"n": 0}

        def poll_wallet_stub(wallet, baseline=False):
            call["n"] += 1
            # Real daemon only sees the sell on cycle 1, so the retry pass
            # (not process_swaps) must drive the close on cycle 2.
            return ([sell] if call["n"] == 1 else []), sell.timestamp

        poller.poll_wallet = poll_wallet_stub

        # Cycle 1: price fetch fails -> park.
        saved = _patch_price(poller)
        try:
            with _Capture() as msgs:
                poller.run_once()
            assert pick_id in poller.state.pending_closes, "parked on first cycle"
            assert pick_id in poller.state.open_positions, "position still open"
            assert any("PRICE-FAIL PENDING" in m for m in msgs), (
                "price-fail logged at close time"
            )
        finally:
            _restore_price(saved)

        # Cycle 2: retry pass re-fetches -> prices -> closes via real machinery.
        def fake_price(token):
            assert token == "0xb200000000000000000000aaf5c1ea536c778701"
            return 4.202125431

        saved2 = _patch_price(poller, fake=fake_price)
        try:
            poller.run_once()
            assert pick_id not in poller.state.open_positions, (
                "position closed on price-retry"
            )
            assert pick_id not in poller.state.pending_closes, (
                "pending record cancelled on price-return"
            )
            rows = open(journal_path).read().strip().splitlines()
            assert len(rows) == 1, "exactly one mirrored_exit row written"
            row = json.loads(rows[0])
            assert row["close_reason"] == "mirrored_exit"
            assert row["pick_id"] == pick_id
            assert row["close_price"] == 4.202125431
        finally:
            _restore_price(saved2)
        print("PASS run_once_parks_then_retry_closes_through_real_cycle")


def test_days_derived_from_elapsed_not_call_count():
    # Blocker-2: the counter is DERIVED from elapsed wall-clock since the
    # FIRST failure, never incremented per call. Park three times in the same
    # second and the day must stay 1; only a full elapsed day plus one
    # advances it.
    with tempfile.TemporaryDirectory() as tmp:
        poller, _, _ = _make_poller(tmp, now_ts=BASE_TS)
        pick_id = "0x347ffc6db9acc54ac2019795173b9599e8b82bd9:0x7a19f4c7"
        _seed_position(poller, pick_id)
        _patch_price(poller)  # fail closed on this test's price chain.

        def park_at(offset):
            poller.now_fn = lambda: datetime.datetime.fromtimestamp(
                BASE_TS + offset, datetime.timezone.utc
            )
            poller.process_swaps([_sell_swap(poller.now_fn().timestamp())])

        park_at(0)
        assert poller.state.pending_closes[pick_id]["consecutive_days"] == 1
        assert "first_ts" in poller.state.pending_closes[pick_id]

        # 23h later -> still day 1 (not call-counted).
        poller.now_fn = lambda: datetime.datetime.fromtimestamp(
            BASE_TS + 23 * 3600, datetime.timezone.utc
        )
        park_at(23 * 3600)
        assert poller.state.pending_closes[pick_id]["consecutive_days"] == 1, (
            "23h is too short to cross a day"
        )

        # 25h (total) -> day 2. Park again -> day 2.
        poller.now_fn = lambda: datetime.datetime.fromtimestamp(
            BASE_TS + 25 * 3600, datetime.timezone.utc
        )
        park_at(25 * 3600)
        assert poller.state.pending_closes[pick_id]["consecutive_days"] == 2, (
            "25h crosses one calendar day"
        )

        # 48h (total) -> day 3 -> ESCALATE (exactly once).
        poller.now_fn = lambda: datetime.datetime.fromtimestamp(
            BASE_TS + 48 * 3600, datetime.timezone.utc
        )
        with _Capture() as msgs:
            park_at(48 * 3600)
        assert poller.state.pending_closes[pick_id]["consecutive_days"] == 3, (
            "48h crosses two calendar days"
        )
        assert any("ESCALATE CLOSE" in m for m in msgs), "must escalate at day 3"

        # Same simulated day again -> same day count, ESCALATE NOT re-logged.
        with _Capture() as msgs2:
            park_at(48 * 3600)
        assert poller.state.pending_closes[pick_id]["consecutive_days"] == 3
        assert sum("ESCALATE CLOSE" in m for m in msgs2) == 0, (
            "escalation must not re-log on the same simulated day"
        )
        print("PASS days derived from elapsed, not call count")


def test_junction_b_max_hold_price_failure_logs_and_parks():
    # Junction (b): max-hold path price failures now log, not silent.
    with tempfile.TemporaryDirectory() as tmp:
        poller, _, _ = _make_poller(tmp, now_ts=BASE_TS)
        pick_id = "0x347ffc6db9acc54ac2019795173b9599e8b82bd9:0x7a19f4c7"
        _seed_position(poller, pick_id)
        # Backdate 8 days into the past so it is past max_hold.
        poller.state.open_positions[pick_id].open_time = (
            datetime.datetime.fromtimestamp(BASE_TS - 8 * 86400,
                                            datetime.timezone.utc).isoformat()
        )

        _patch_price(poller)
        with _Capture() as msgs:
            poller.check_max_hold()

        assert pick_id in poller.state.open_positions, (
            "max-hold exit must NOT be silently dropped"
        )
        assert pick_id in poller.state.pending_closes, (
            "max-hold exit must be parked too"
        )
        assert any("PRICE-FAIL PENDING" in m for m in msgs), (
            "max-hold path logs the price failure"
        )
        print("PASS junction-b: max-hold price failure parks and logs (no silent path)")


def test_max_hold_success_clears_stale_pending():
    # A parked exit is still open. When it hits max_hold and prices, the
    # close must roll back the pending record too (no stale pending left).
    with tempfile.TemporaryDirectory() as tmp:
        poller, _, _ = _make_poller(tmp, now_ts=BASE_TS)
        pick_id = "0x347ffc6db9acc54ac2019795173b9599e8b82bd9:0x7a19f4c7"
        _seed_position(poller, pick_id)
        poller.state.open_positions[pick_id].open_time = (
            datetime.datetime.fromtimestamp(
                BASE_TS - 8 * 86400, datetime.timezone.utc
            ).isoformat()
        )

        # Park the exit.
        _patch_price(poller)
        poller.process_swaps([_sell_swap(BASE_TS)])
        assert pick_id in poller.state.pending_closes, "parked before max-hold"

        # Max-hold success: price returns -> close + clear pending.
        def fake_price(token):
            return 4.202125431

        saved = _patch_price(poller, fake=fake_price)
        try:
            poller.check_max_hold()
        finally:
            _restore_price(saved)

        assert pick_id not in poller.state.open_positions, (
            "max-hold closed the position"
        )
        assert pick_id not in poller.state.pending_closes, (
            "stale pending record must be cleared on max-hold success"
        )
        print("PASS max_hold success clears stale pending")


def test_escalation_shouts_once_not_relogged():
    # Escalation is loud at the 3-day threshold and is NOT re-logged on
    # repeated calls within the same simulated day.
    with tempfile.TemporaryDirectory() as tmp:
        poller, _, _ = _make_poller(tmp, now_ts=BASE_TS)
        pick_id = "0x347ffc6db9acc54ac2019795173b9599e8b82bd9:0x7a19f4c7"
        _seed_position(poller, pick_id)
        _patch_price(poller)

        def park_at(offset):
            poller.now_fn = lambda: datetime.datetime.fromtimestamp(
                BASE_TS + offset, datetime.timezone.utc
            )
            poller.process_swaps([_sell_swap(poller.now_fn().timestamp())])

        park_at(0)  # day 1
        assert poller.state.pending_closes[pick_id]["consecutive_days"] == 1

        poller.now_fn = lambda: datetime.datetime.fromtimestamp(
            BASE_TS + 48 * 3600, datetime.timezone.utc
        )
        with _Capture() as msgs:
            park_at(48 * 3600)  # day 3 -> ESCALATE
        assert any("ESCALATE CLOSE" in m for m in msgs)
        assert poller.state.pending_closes[pick_id]["escalated"] is True

        # Same simulated day again -> not escalated again, not re-logged.
        with _Capture() as msgs2:
            park_at(48 * 3600)
        assert poller.state.pending_closes[pick_id]["consecutive_days"] == 3
        assert sum("ESCALATE CLOSE" in m for m in msgs2) == 0
        print("PASS escalation shouts once, not every cycle")


def test_junction_d_price_returns_and_real_close_fires():
    # Junction (d): once the price returns, the real close path fires, the
    # journal gets a row, and the pending record cancels itself. End-to-end.
    with tempfile.TemporaryDirectory() as tmp:
        poller, _, journal_path = _make_poller(tmp, now_ts=BASE_TS)
        pick_id = "0x347ffc6db9acc54ac2019795173b9599e8b82bd9:0x7a19f4c7"
        _seed_position(poller, pick_id)

        def fake_price(token):
            assert token == "0xb200000000000000000000aaf5c1ea536c778701"
            return 4.202125431

        _patch_price(poller, fake=fake_price)
        poller.process_swaps([_sell_swap(poller.now_fn().timestamp())])

        assert pick_id not in poller.state.open_positions, (
            "position closed on price return"
        )
        assert pick_id not in poller.state.pending_closes, (
            "pending record cancelled on price return"
        )
        rows = open(journal_path).read().strip().splitlines()
        assert len(rows) == 1, "exactly one mirrored_exit row written"
        row = json.loads(rows[0])
        assert row["close_reason"] == "mirrored_exit"
        assert row["pick_id"] == pick_id
        assert row["close_price"] == 4.202125431
        print("PASS junction-d: price return fires real close, journals row, cancels pending")


def test_junction_f_max_hold_retry_closes_as_max_hold():
    # Junction (f): the common case. A max-hold close hits a transient price
    # failure and parks; the next cycle the price returns and the retry pass
    # (which runs BEFORE max-hold) closes it. It must close as max_hold — not
    # mirrored_exit — with a sane (post-open) close time, since no sell ever
    # happened (sell_timestamp 0). close_reason is the ledger the verdict
    # rests on; a mislabel corrupts it.
    with tempfile.TemporaryDirectory() as tmp:
        poller, _, journal_path = _make_poller(tmp, now_ts=BASE_TS)
        pick_id = "0x347ffc6db9acc54ac2019795173b9599e8b82bd9:0x7a19f4c7"
        _seed_position(poller, pick_id)
        open_ts = BASE_TS - 8 * 86400
        poller.state.open_positions[pick_id].open_time = (
            datetime.datetime.fromtimestamp(
                open_ts, datetime.timezone.utc
            ).isoformat()
        )

        # Cycle 1: max-hold hits a transient price failure -> parked as max_hold.
        _patch_price(poller)  # fail closed.
        poller.check_max_hold()
        assert pick_id in poller.state.open_positions, "held open on price fail"
        assert pick_id in poller.state.pending_closes, "max-hold exit parked"
        entry = poller.state.pending_closes[pick_id]
        assert entry["close_reason"] == "max_hold", "parked under max_hold reason"
        assert entry["sell_timestamp"] == 0, "no sell ever happened for max-hold"

        # Cycle 2: price returns -> retry pass closes it (runs before max-hold).
        def fake_price(token):
            return 4.202125431

        saved = _patch_price(poller, fake=fake_price)
        try:
            poller._retry_pending_closes()
        finally:
            _restore_price(saved)

        assert pick_id not in poller.state.open_positions, "position closed"
        assert pick_id not in poller.state.pending_closes, "pending cancelled"
        rows = open(journal_path).read().strip().splitlines()
        assert len(rows) == 1, "exactly one close row written"
        row = json.loads(rows[0])
        assert row["close_reason"] == "max_hold", (
            "max-hold park must NOT close as mirrored_exit — ledger corruption"
        )
        # close_time must be a sane post-open wall-clock time (sell ts was 0).
        close_time = datetime.datetime.fromisoformat(row["close_time"])
        assert close_time.timestamp() > open_ts, "close_time after open_time"
        print("PASS junction-f: max-hold retry closes as max_hold with sane time")


if __name__ == "__main__":
    tests = [
        "test_junction_b_max_hold_price_failure_logs_and_parks",
        "test_max_hold_success_clears_stale_pending",
        "test_days_derived_from_elapsed_not_call_count",
        "test_escalation_shouts_once_not_relogged",
        "test_run_once_parks_and_retry_closes_through_real_cycle",
        "test_junction_d_price_returns_and_real_close_fires",
        "test_junction_f_max_hold_retry_closes_as_max_hold",
    ]
    for name in tests:
        getattr(sys.modules[__name__], name)()
    print("ALL %d TESTS PASSED" % len(tests))
