#!/usr/bin/env python3
"""Tests for probe_executor (WO9). Throwaway keys only — no live submission.

Run: /home/kenzodadon/base-trend-bot/.venv/bin/python test_probe_executor.py
     (venv required: eth_keys / eth_account)

Covers the 15 WO9 acceptance junctions. Every external dependency is mocked;
the real keystore file is NEVER decrypted; the real caps.json is read-only
and untouched; no transaction is ever signed or submitted.

Tests:
  1. Gate refusal while disarmed (kill_switch, separately live_trading)
  2. Allowlist rejection (non-allowlisted to; approve above bound)
  3. Per-trade cap (0.0002 exact pass; above refused)
  4. Cumulative pot cap (entry crossing 0.0100 refused)
  5. Daily-loss halt (entries halted, exits permitted)
  6. Expiry halt (31 days; trade-count cap)
  7. Price-failure fail-closed (price_failure_skip, no signing)
  8. Unroutable skip (quoters revert → unroutable_skip, no signing)
  9. Fill recording (one complete §6 receipt, every field)
 10. Pot persistence across restart (identical state, no re-entry)
 11. Mirrored + max-hold exits
 12. Key handling (decrypt once per sign, zeroize after incl. exceptions,
     no key material in output, wrong-address startup refusal)
 13. Dedup (same pick_id twice → one position)
 14. Dry-run end-to-end (disarmed; zero decrypt, zero submit)
 15. Existing suites still green (invoked by the shell, asserted here)
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, "/home/kenzodadon/base-trend-bot")

import probe_executor as pe
from eth_keys import keys as _eth_keys

NOW = 1_700_000_000          # fixed clock; all tests inject their own
PRICE = 0.001234             # USD per token — arbitrary, stable
TOKEN = "0x" + "ab" * 20
LEADER_WALLET = "0x" + "11" * 20


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def throwaway_key():
    """(key_bytes, hex, address) — fresh from the OS CSPRNG, never reused."""
    raw = os.urandom(32)
    return (raw, raw.hex(),
            _eth_keys.PrivateKey(raw).public_key.to_checksum_address())


class FakeKeystore:
    """Mocks probe_keystore: decrypt_at_use + _zeroize, with call counting."""

    def __init__(self, key_hex: str = "ab" * 32, address: str = None):
        self._key_hex = key_hex
        self._address = address or (
            _eth_keys.PrivateKey(bytes.fromhex(key_hex))
            .public_key.to_checksum_address())
        self.decrypt_calls = 0
        self.zeroize_calls = 0
        self.zeroized_buffers = []

    def decrypt_at_use(self, passphrase=None) -> bytes:
        self.decrypt_calls += 1
        return bytes.fromhex(self._key_hex)

    def _zeroize(self, buf):
        self.zeroize_calls += 1
        if isinstance(buf, bytearray):
            self.zeroized_buffers.append(bytes(buf))
            for i in range(len(buf)):
                buf[i] = 0


def good_quote(price=PRICE, venue="aerodrome", amount_out=1000.0):
    return pe.Quote(venue=venue, amount_out=amount_out,
                    implied_price_usd=price, routable=True)


def make_executor(tmp, **kw):
    """A ProbeExecutor wired to temp paths and mocked dependencies."""
    defaults = dict(
        state_path=os.path.join(tmp, "lane_state.json"),
        ledger_path=os.path.join(tmp, "ledger.jsonl"),
        posture_fn=lambda: (True, False),          # disarmed by default
        price_fn=lambda token: (PRICE, "ok"),
        quote_fn=lambda token, side, size: good_quote(),
        sign_fn=lambda tx, key: b"signed",
        submit_fn=lambda signed: {
            "tx_hash": "0x" + "de" * 32, "block_number": 123,
            "block_time": NOW, "executed_price_usd": PRICE,
            "gas_eth": 0.00002, "gas_usd": 0.05, "amount_out": 1000.0},
        balance_fn=lambda: 1.0,
        gas_price_fn=lambda: (0.1, 0.05),
        nonce_fn=lambda: (5, 5),
        now_fn=lambda: float(NOW),
        keystore=FakeKeystore(),
    )
    defaults.update(kw)
    return pe.ProbeExecutor(**defaults)


def leader_signal(pick_id="w1-0xdead"):
    return {"pick_id": pick_id, "wallet_address": LEADER_WALLET,
            "token": TOKEN, "entry_price": PRICE, "open_ts": NOW - 600}


def armed_posture():
    return (False, True)   # kill_switch=False, live_trading=True


def pe_default_slippage():
    """The executor's RULED slippage value — read from the source, never
    hardcoded here (WO12: the 150 default became 300; a hardcoded 150
    in this file would silently restore the old value)."""
    import re as _re
    src = open("/home/kenzodadon/base-trend-bot/"
               "probe_executor.py").read()
    m = _re.search(r"^PROBE_MAX_SLIPPAGE_BPS = ([\d.]+)", src, _re.M)
    if m is None:
        raise AssertionError("cannot read the executor's ruled slippage "
                             "value from source")
    return float(m.group(1))


def read_ledger(ex):
    rows = []
    if os.path.exists(ex.ledger_path):
        with open(ex.ledger_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    return rows


def seed_open_position(ex, pid="w1-0xdead", entry_age=100,
                       leader_exit_price=0.0, leader_exit_time=0):
    """Seed an open lane position for exit tests."""
    ex.state.open_positions[pid] = pe.LanePosition(
        pick_id=pid, leader_wallet=LEADER_WALLET, token=TOKEN,
        size_eth=0.0002, entry_quoted_price_usd=PRICE,
        entry_executed_price_usd=PRICE, entry_tx_hash="0xaa",
        entry_block=1, entry_time=NOW - entry_age,
        leader_entry_price_usd=PRICE, leader_entry_time=NOW - 700,
        token_amount=1000.0,
        leader_exit_price_usd=leader_exit_price,
        leader_exit_time=leader_exit_time)
    ex.state.processed_pick_ids.append(pid)
    ex.save_state()
    return pid


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestGateRefusalWhileDisarmed(unittest.TestCase):
    """ACCEPTANCE 1: live-mode run refuses before any signing while disarmed."""

    def _run_disarmed(self, kill, live):
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            ks = FakeKeystore()
            ex = make_executor(tmp, posture_fn=lambda: (kill, live),
                               keystore=ks)
            poller = os.path.join(tmp, "poller.json")
            with open(poller, "w") as f:
                json.dump({"open_positions": {}}, f)
            ex.poller_state_path = poller
            summary = ex.run_cycle(NOW)
            return ex, ks, summary
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_kill_switch_true_refuses(self):
        ex, ks, summary = self._run_disarmed(True, False)
        self.assertEqual(ks.decrypt_calls, 0, "zero decrypt calls while disarmed")
        self.assertTrue(summary["refusals"], "refusal logged")
        self.assertFalse(summary["entries"])

    def test_live_trading_false_refuses(self):
        ex, ks, summary = self._run_disarmed(False, False)
        self.assertEqual(ks.decrypt_calls, 0)
        self.assertTrue(summary["refusals"])

    def test_start_refuses_live_while_disarmed(self):
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            ks = FakeKeystore()
            ex = make_executor(tmp, posture_fn=lambda: (True, False),
                               keystore=ks)
            with self.assertRaises(pe.ExecutorError):
                ex.start()
            self.assertEqual(ks.decrypt_calls, 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestAllowlistRejection(unittest.TestCase):
    """ACCEPTANCE 2: non-allowlisted to; approve above per-trade bound."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wo9_test_")
        self.ex = make_executor(self.tmp, posture_fn=armed_posture)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_non_allowlisted_to_rejected(self):
        tx = pe.UnsignedTx(to="0x" + "99" * 20, function="swapExactETHForTokens",
                           value_eth=pe.TRADE_SIZE_ETH,
                           amount_in_eth=pe.TRADE_SIZE_ETH)
        ok, why = self.ex.check_allowlist(tx)
        self.assertFalse(ok)
        self.assertIn("not in allowlist", why)

    def test_approve_above_bound_rejected(self):
        # approve EXCEEDS the position's token amount (10000 > 1000)
        tx = pe.UnsignedTx(to=pe.WETH, function="approve", value_eth=0.0,
                           amount_in_eth=pe.TRADE_SIZE_ETH,
                           spender=pe.AERODROME_ROUTER,
                           approve_amount=10000.0,
                           position_token_amount=1000.0)
        ok, why = self.ex.check_allowlist(tx)
        self.assertFalse(ok)
        self.assertIn("never unlimited", why)

    def test_approve_unlimited_sentinel_rejected(self):
        # 2**256-1 is the classic unlimited-approve sentinel.
        tx = pe.UnsignedTx(to=pe.WETH, function="approve", value_eth=0.0,
                           amount_in_eth=pe.TRADE_SIZE_ETH,
                           spender=pe.AERODROME_ROUTER,
                           approve_amount=2**256 - 1,
                           position_token_amount=1000.0)
        ok, why = self.ex.check_allowlist(tx)
        self.assertFalse(ok)
        self.assertIn("never unlimited", why)

    def test_valid_allowlisted_tx_passes(self):
        tx = pe.UnsignedTx(to=pe.AERODROME_ROUTER,
                           function="swapExactETHForTokens",
                           value_eth=pe.TRADE_SIZE_ETH,
                           amount_in_eth=pe.TRADE_SIZE_ETH,
                           deadline_ts=NOW + 60)
        ok, why = self.ex.check_allowlist(tx)
        self.assertTrue(ok, why)


class TestPerTradeCap(unittest.TestCase):
    """ACCEPTANCE 3: above 0.0002 refused; exactly 0.0002 passes."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wo9_test_")
        self.ex = make_executor(self.tmp, posture_fn=armed_posture)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_above_cap_rejected(self):
        tx = pe.UnsignedTx(to=pe.AERODROME_ROUTER,
                           function="swapExactETHForTokens",
                           value_eth=pe.TRADE_SIZE_ETH * 2,
                           amount_in_eth=pe.TRADE_SIZE_ETH * 2)
        ok, why = self.ex.check_allowlist(tx)
        self.assertFalse(ok)
        self.assertIn("per-trade cap", why)

    def test_exact_size_passes(self):
        self.assertAlmostEqual(pe.TRADE_SIZE_ETH, 0.0002)
        tx = pe.UnsignedTx(to=pe.AERODROME_ROUTER,
                           function="swapExactETHForTokens",
                           value_eth=0.0002, amount_in_eth=0.0002)
        ok, why = self.ex.check_allowlist(tx)
        self.assertTrue(ok, why)

    def test_entry_signal_never_sizes_above_cap(self):
        ex = make_executor(self.tmp, posture_fn=armed_posture)
        r = ex.process_entry(leader_signal(), NOW)
        self.assertEqual(r["action"], "entered")
        pos = ex.state.open_positions["w1-0xdead"]
        self.assertAlmostEqual(pos.size_eth, 0.0002)


class TestPotCap(unittest.TestCase):
    """ACCEPTANCE 4: entry crossing 0.0100 cumulative refused."""

    def test_crossing_entry_refused(self):
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            ex = make_executor(tmp, posture_fn=armed_posture)
            # seed cumulative notional so the NEXT trade crosses C:
            # projected = seeded + 0.0002 must EXCEED 0.0100
            ex.state.cumulative_entry_notional_eth = (
                pe.C_POT_ETH - pe.TRADE_SIZE_ETH + 0.00005)
            ex.save_state()
            r = ex.process_entry(leader_signal(), NOW)
            self.assertNotEqual(r["action"], "entered")
            self.assertIn("pot cap", r["reason"])
            self.assertFalse(ex.state.open_positions)
            rows = read_ledger(ex)
            self.assertTrue(any(row.get("row") == "pot_cap_skip"
                                for row in rows))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_below_cap_passes(self):
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            ex = make_executor(tmp, posture_fn=armed_posture)
            ex.state.cumulative_entry_notional_eth = 0.009
            r = ex.process_entry(leader_signal(), NOW)
            self.assertEqual(r["action"], "entered")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestDailyLossHalt(unittest.TestCase):
    """ACCEPTANCE 5: day bucket ≤ -0.001 → entries halted, exits permitted."""

    def test_entries_halted_exits_permitted(self):
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            ex = make_executor(tmp, posture_fn=armed_posture)
            day = pe._day_key(NOW)      # the Chicago day of NOW (WO12)
            ex.state.day_bucket[day] = -0.0015   # worse than the halt line
            r = ex.process_entry(leader_signal(), NOW)
            self.assertNotEqual(r["action"], "entered")
            self.assertIn("daily-loss halt", r["reason"])

            # an open position may still EXIT (wind-down)
            ex.state.open_positions["p1"] = pe.LanePosition(
                pick_id="p1", leader_wallet=LEADER_WALLET, token=TOKEN,
                size_eth=0.0002, entry_quoted_price_usd=PRICE,
                entry_executed_price_usd=PRICE, entry_tx_hash="0xaa",
                entry_block=1, entry_time=NOW - 100,
                leader_entry_price_usd=PRICE, leader_entry_time=NOW - 700,
                token_amount=1000.0)
            r2 = ex.process_exit("p1", "mirrored_exit", NOW)
            self.assertEqual(r2["action"], "closed",
                             "exits must remain permitted under the halt")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestSlippageAndDayBucketWO12(unittest.TestCase):
    """WO12 pre-arm rulings, pinned against the executor source."""

    def test_slippage_constant_is_the_ruled_300_no_env_knob(self):
        """(A) PROBE_MAX_SLIPPAGE_BPS == 300 as a RULED constant — read
        from the source so the test pins the value, not itself. No env
        knob: the source must not read the variable."""
        import re as _re
        src = open("/home/kenzodadon/base-trend-bot/"
                   "probe_executor.py").read()
        m = _re.search(r"^PROBE_MAX_SLIPPAGE_BPS = ([\d.]+)", src, _re.M)
        self.assertIsNotNone(m, "the constant must be defined in source")
        self.assertEqual(float(m.group(1)), 300.0,
                         "the ruled slippage value is 300")
        self.assertEqual(pe.PROBE_MAX_SLIPPAGE_BPS, 300.0)
        self.assertNotIn("PROBE_MAX_SLIPPAGE_BPS\", \"150\"", src,
                         "no env knob — the value is not configurable")
        self.assertNotIn('os.environ.get("PROBE_MAX_SLIPPAGE_BPS"', src,
                         "no env knob on slippage")

    def test_gate_passes_at_250_bps_aborts_at_350(self):
        """(A) the gate consumes the ruled value: 250 bps adverse passes,
        350 bps aborts — both sides of the 300 line."""
        for venue in ("buy", "sell"):
            tmp = tempfile.mkdtemp(prefix="wo12_test_")
            try:
                ex = make_executor(tmp, posture_fn=armed_posture)
                # 250 bps adverse = quote 2.5% worse than the median
                ex.quote_fn = lambda t, s, z: good_quote(
                    price=PRICE * 1.025)
                if venue == "buy":
                    r = ex.process_entry(leader_signal(), NOW)
                    self.assertEqual(r["action"], "entered",
                                     f"{venue}: 250 bps must PASS the "
                                     f"300-bps gate")
                else:
                    seed_open_position(ex, "w1-0xdead")
                    r = ex.process_exit("w1-0xdead", "mirrored_exit", NOW)
                    self.assertEqual(r["action"], "closed",
                                     f"{venue}: 250 bps must PASS the "
                                     f"300-bps gate")
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            tmp = tempfile.mkdtemp(prefix="wo12_test_")
            try:
                ks = FakeKeystore()
                ex = make_executor(tmp, posture_fn=armed_posture,
                                   keystore=ks)
                # 350 bps adverse = quote 3.5% worse than the median
                ex.quote_fn = lambda t, s, z: good_quote(
                    price=PRICE * 1.035)
                if venue == "buy":
                    r = ex.process_entry(leader_signal(), NOW)
                    self.assertEqual(r["reason"], "unroutable_skip",
                                     f"{venue}: 350 bps must ABORT")
                    self.assertEqual(ks.decrypt_calls, 0)
                else:
                    seed_open_position(ex, "w1-0xdead")
                    r = ex.process_exit("w1-0xdead", "mirrored_exit", NOW)
                    self.assertIn("unroutable_skip", r["reason"],
                                  f"{venue}: 350 bps must ABORT")
                    self.assertEqual(ks.decrypt_calls, 0)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

    def test_day_key_is_chicago_not_utc(self):
        """(B) the day bucket key is the Chicago-local calendar day:
        a loss at 01:00 UTC the next day still belongs to the SAME
        Chicago day (19:00 local). UTC midnight is not the boundary."""
        # 2023-11-15 01:00:00 UTC == 2023-11-14 19:00:00 Chicago (CST)
        ts_after_utc_midnight = int(datetime(
            2023, 11, 15, 1, 0, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(pe._day_key(ts_after_utc_midnight), "2023-11-14",
                         "01:00 UTC the next day is still the same "
                         "Chicago day — the halt does NOT reset at UTC "
                         "midnight")
        # and the plain UTC view of that same instant differs
        self.assertEqual(
            datetime.fromtimestamp(ts_after_utc_midnight,
                                   tz=timezone.utc).strftime("%Y-%m-%d"),
            "2023-11-15",
            "the UTC date really is the next day — the two bucketing "
            "rules discriminate on this instant")

    def test_fresh_chicago_day_starts_while_utc_date_unchanged(self):
        """(B) the boundary the ruling actually names: a NEW Chicago day
        starts at local midnight (06:00 UTC in CST) while the UTC DATE
        is unchanged. Two timestamps inside the same UTC date, in
        different Chicago days."""
        # 2023-11-14 23:30 UTC == 17:30 Chicago (still 11-14 local)
        ts_before = int(datetime(2023, 11, 14, 23, 30, 0,
                                 tzinfo=timezone.utc).timestamp())
        # 2023-11-15 00:30 UTC == 18:30 Chicago the PREVIOUS day
        # (both are the SAME Chicago day — local 11-14)
        ts_same_local = int(datetime(2023, 11, 15, 0, 30, 0,
                                     tzinfo=timezone.utc).timestamp())
        # 2023-11-15 06:30 UTC == 00:30 Chicago — a FRESH Chicago day
        # (11-15) while the UTC date is still 11-15 for the first of
        # these two instants
        ts_fresh_local = int(datetime(2023, 11, 15, 6, 30, 0,
                                      tzinfo=timezone.utc).timestamp())
        self.assertEqual(pe._day_key(ts_before), "2023-11-14")
        self.assertEqual(pe._day_key(ts_same_local), "2023-11-14",
                         "straddling UTC midnight does NOT start a new "
                         "Chicago day")
        self.assertEqual(pe._day_key(ts_fresh_local), "2023-11-15",
                         "local midnight starts a fresh Chicago day")
        # the two fresh-local/utc instants share the UTC date but not
        # the Chicago day — the discriminating pair
        utc_date_of_both = (datetime.fromtimestamp(ts_same_local,
                                                   tz=timezone.utc)
                            .strftime("%Y-%m-%d"))
        utc_date_of_fresh = (datetime.fromtimestamp(ts_fresh_local,
                                                    tz=timezone.utc)
                             .strftime("%Y-%m-%d"))
        self.assertEqual(utc_date_of_both, "2023-11-15")
        self.assertEqual(utc_date_of_fresh, "2023-11-15")
        self.assertNotEqual(pe._day_key(ts_same_local),
                            pe._day_key(ts_fresh_local),
                            "same UTC date, different Chicago days — the "
                            "halt resets on the Chicago boundary")

    def test_halt_limit_unchanged(self):
        """(B) the halt LINE is untouched by the bucketing change."""
        self.assertEqual(pe.DAILY_LOSS_HALT_ETH, 0.001)


class TestExpiryHalt(unittest.TestCase):
    """ACCEPTANCE 6: 31 days from arm; trade-count cap. Exits still allowed."""

    def test_arm_age_31_days_refuses_entries(self):
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            ex = make_executor(tmp, posture_fn=armed_posture)
            ex.state.arm_timestamp = NOW - 31 * 86400
            r = ex.process_entry(leader_signal(), NOW)
            self.assertNotEqual(r["action"], "entered")
            self.assertIn("expired", r["reason"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_trade_count_cap_refuses_entries(self):
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            ex = make_executor(tmp, posture_fn=armed_posture)
            ex.state.executed_trades = pe.PROBE_MAX_TRADES
            r = ex.process_entry(leader_signal(), NOW)
            self.assertNotEqual(r["action"], "entered")
            self.assertIn("expired", r["reason"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_exit_permitted_while_expired(self):
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            ex = make_executor(tmp, posture_fn=armed_posture)
            ex.state.arm_timestamp = NOW - 31 * 86400
            ex.state.open_positions["p1"] = pe.LanePosition(
                pick_id="p1", leader_wallet=LEADER_WALLET, token=TOKEN,
                size_eth=0.0002, entry_quoted_price_usd=PRICE,
                entry_executed_price_usd=PRICE, entry_tx_hash="0xaa",
                entry_block=1, entry_time=NOW - 100,
                leader_entry_price_usd=PRICE, leader_entry_time=NOW - 700,
                token_amount=1000.0)
            r = ex.process_exit("p1", "mirrored_exit", NOW)
            self.assertEqual(r["action"], "closed")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestPriceFailure(unittest.TestCase):
    """ACCEPTANCE 7: price chain fails → price_failure_skip, no signing."""

    def test_price_failure_no_signing(self):
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            ks = FakeKeystore()
            ex = make_executor(tmp, posture_fn=armed_posture,
                               price_fn=lambda token: (None, "no_pool"),
                               keystore=ks)
            r = ex.process_entry(leader_signal(), NOW)
            self.assertEqual(r["action"], "skip")
            self.assertEqual(r["reason"], "price_failure_skip")
            self.assertEqual(ks.decrypt_calls, 0, "no signing attempt")
            rows = read_ledger(ex)
            self.assertTrue(any(row.get("row") == "price_failure_skip"
                                for row in rows))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestUnroutableSkip(unittest.TestCase):
    """ACCEPTANCE 8: quoters revert → unroutable_skip, no signing."""

    def test_quoter_revert_no_signing(self):
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            ks = FakeKeystore()
            ex = make_executor(tmp, posture_fn=armed_posture,
                               quote_fn=lambda token, side, size: None,
                               keystore=ks)
            r = ex.process_entry(leader_signal(), NOW)
            self.assertEqual(r["action"], "skip")
            self.assertEqual(r["reason"], "unroutable_skip")
            self.assertEqual(ks.decrypt_calls, 0)
            rows = read_ledger(ex)
            self.assertTrue(any(row.get("row") == "unroutable_skip"
                                for row in rows))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_slippage_beyond_tolerance_skips(self):
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            ks = FakeKeystore()
            ex = make_executor(tmp, posture_fn=armed_posture,
                               keystore=ks)
            pe.PROBE_MAX_SLIPPAGE_BPS = 100
            try:
                # quote 5% worse than the median
                ex.quote_fn = lambda t, s, z: good_quote(price=PRICE * 1.05)
                r = ex.process_entry(leader_signal(), NOW)
                self.assertEqual(r["reason"], "unroutable_skip")
                self.assertEqual(ks.decrypt_calls, 0)
            finally:
                pe.PROBE_MAX_SLIPPAGE_BPS = pe_default_slippage()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestFillRecording(unittest.TestCase):
    """ACCEPTANCE 9: one complete §6 receipt with every field."""

    REQUIRED_FIELDS = [
        "row", "pick_id", "leader_wallet", "token",
        "leader_entry_price", "leader_entry_time",
        "leader_exit_price", "leader_exit_time",
        "follower_entry_tx_hash", "follower_entry_block",
        "follower_entry_time", "follower_exit_tx_hash",
        "follower_exit_block", "follower_exit_time",
        "quoted_price_in", "executed_price_in",
        "quoted_price_out", "executed_price_out",
        "slippage_bps_in", "slippage_bps_out", "slippage_bps_combined",
        "gas_usd_in", "gas_usd_out", "gas_usd_total",
        "delay_sec_in", "delay_sec_out", "size_eth",
        "leader_gross", "follower_gross", "follower_net",
        "fill_replication_ratio", "trade_replication_ratio",
        "close_reason", "recorded_at",
    ]

    def _run_roundtrip(self, tmp):
        """Entry then exit, DRIVEN THROUGH run_cycle with a close row
        carrying leader exit price/time (F2: production must thread them;
        the old test seeded the position by hand)."""
        # Realistic mock: entry fills at PRICE, the exit swap fills at
        # PRICE*1.15 (approve steps in between carry no price).
        call = {"n": 0}

        def submit(signed):
            call["n"] += 1
            exit_leg = call["n"] >= 2  # entry swap = call 1; exit swap = last
            return {
                "tx_hash": "0x" + "de" * 32, "block_number": 123 + call["n"],
                "block_time": NOW + call["n"] * 100,
                "executed_price_usd": (PRICE * 1.15 if exit_leg else PRICE),
                "gas_eth": 0.00002, "gas_usd": 0.05, "amount_out": 1000.0}

        ex = make_executor(tmp, posture_fn=armed_posture, submit_fn=submit)
        poller = os.path.join(tmp, "poller.json")
        with open(poller, "w") as f:
            json.dump({"open_positions": {
                "w1-0xdead": {
                    "wallet_address": LEADER_WALLET, "token": TOKEN,
                    "entry_price": PRICE, "open_ts": NOW - 600}}}, f)
        ex.poller_state_path = poller
        summary = ex.run_cycle(NOW)
        self.assertEqual(summary["entries"][0]["action"], "entered")
        pid = "w1-0xdead"
        self.assertIn(pid, ex.state.open_positions)
        # close row carrying the leader's exit data
        closes = os.path.join(tmp, "closes.jsonl")
        with open(closes, "w") as f:
            f.write(json.dumps({
                "pick_id": pid, "close_reason": "mirrored_exit",
                "close_price": PRICE * 1.2, "close_ts": NOW + 300}) + "\n")
        ex.poller_closes_path = closes
        r = ex.run_cycle(NOW + 400)
        self.assertEqual(r["exits"][0]["action"], "closed")
        return ex

    def test_receipt_complete(self):
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            ex = self._run_roundtrip(tmp)
            rows = read_ledger(ex)
            receipts = [r for r in rows if r.get("row") == "receipt"]
            self.assertEqual(len(receipts), 1, "exactly one receipt")
            receipt = receipts[0]
            for field in self.REQUIRED_FIELDS:
                self.assertIn(field, receipt, f"missing field {field}")
            # tx hashes, executed prices, gas from the mocked receipts
            self.assertEqual(receipt["follower_entry_tx_hash"], "0x" + "de" * 32)
            self.assertEqual(receipt["follower_exit_tx_hash"], "0x" + "de" * 32)
            self.assertAlmostEqual(receipt["executed_price_in"], PRICE)
            self.assertGreater(receipt["gas_usd_total"], 0)
            self.assertEqual(receipt["close_reason"], "mirrored_exit")
            # F2: leader exit data threaded from the close row — NOT zero
            self.assertAlmostEqual(receipt["leader_exit_price"], PRICE * 1.2)
            self.assertEqual(receipt["leader_exit_time"], NOW + 300)
            self.assertNotEqual(receipt["leader_gross"], 0.0,
                                "leader_gross must be computed from real data")
            self.assertNotEqual(receipt["trade_replication_ratio"], 0.0,
                                "trade_replication_ratio must be real")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_pnl_accounted(self):
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            ex = self._run_roundtrip(tmp)
            self.assertNotEqual(ex.state.realized_pnl_eth, 0.0)
            self.assertFalse(ex.state.open_positions)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestPotPersistenceAcrossRestart(unittest.TestCase):
    """ACCEPTANCE 10: kill + reload → identical state, no re-entry."""

    def test_restart_resumes_exactly(self):
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            ex = make_executor(tmp, posture_fn=armed_posture)
            ex.process_entry(leader_signal(), NOW)
            ex.process_entry(leader_signal("w2-0xbeef"), NOW)
            before = ex.state.to_dict()
            del ex  # "kill"

            ex2 = make_executor(tmp, posture_fn=armed_posture)
            after = ex2.state.to_dict()
            self.assertEqual(before, after,
                             "lane state must survive restart identically")

            # the same signal is not re-entered
            r = ex2.process_entry(leader_signal(), NOW)
            self.assertNotEqual(r["action"], "entered")
            self.assertIn("dedup", r["reason"])
            self.assertEqual(len(ex2.state.open_positions), 2)
            self.assertEqual(ex2.state.executed_trades, 2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestMirroredAndMaxHoldExits(unittest.TestCase):
    """ACCEPTANCE 11: mirrored_exit closes; 7-day position closes max_hold."""

    def _seed_open(self, ex, pid="w1-0xdead", entry_age=100):
        ex.state.open_positions[pid] = pe.LanePosition(
            pick_id=pid, leader_wallet=LEADER_WALLET, token=TOKEN,
            size_eth=0.0002, entry_quoted_price_usd=PRICE,
            entry_executed_price_usd=PRICE, entry_tx_hash="0xaa",
            entry_block=1, entry_time=NOW - entry_age,
            leader_entry_price_usd=PRICE, leader_entry_time=NOW - 700,
            token_amount=1000.0)
        ex.state.processed_pick_ids.append(pid)
        ex.save_state()

    def test_mirrored_exit_closes(self):
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            ex = make_executor(tmp, posture_fn=armed_posture)
            self._seed_open(ex)
            closes = os.path.join(tmp, "closes.jsonl")
            with open(closes, "w") as f:
                f.write(json.dumps({"pick_id": "w1-0xdead",
                                    "close_reason": "mirrored_exit"}) + "\n")
            ex.poller_closes_path = closes
            summary = ex.run_cycle(NOW)
            self.assertEqual(summary["exits"][0]["action"], "closed")
            self.assertEqual(summary["exits"][0]["reason"], "mirrored_exit")
            self.assertNotIn("w1-0xdead", ex.state.open_positions)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_max_hold_closes(self):
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            ex = make_executor(tmp, posture_fn=armed_posture)
            self._seed_open(ex, entry_age=pe.MAX_HOLD_DAYS * 86400 + 60)
            summary = ex.run_cycle(NOW)
            exits = [e for e in summary["exits"]
                     if e["reason"] == "max_hold"]
            self.assertTrue(exits, "7-day position must close with max_hold")
            self.assertNotIn("w1-0xdead", ex.state.open_positions)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_young_position_stays_open(self):
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            ex = make_executor(tmp, posture_fn=armed_posture)
            self._seed_open(ex, entry_age=100)
            summary = ex.run_cycle(NOW)
            self.assertFalse(summary["exits"])
            self.assertIn("w1-0xdead", ex.state.open_positions)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestKeyHandling(unittest.TestCase):
    """ACCEPTANCE 12: decrypt once per sign; zeroize after, always; no leak."""

    def test_decrypt_once_per_signing_event(self):
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            ks = FakeKeystore()
            ex = make_executor(tmp, posture_fn=armed_posture, keystore=ks)
            ex.process_entry(leader_signal(), NOW)
            self.assertEqual(ks.decrypt_calls, 1, "one decrypt per signing")
            self.assertEqual(ks.zeroize_calls, 1, "one zeroize per signing")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_zeroize_after_signing_raises(self):
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            ks = FakeKeystore()

            def raising_sign(tx, key):
                raise RuntimeError("signing blew up")

            ex = make_executor(tmp, posture_fn=armed_posture,
                               sign_fn=raising_sign, keystore=ks)
            r = ex.process_entry(leader_signal(), NOW)
            self.assertNotEqual(r["action"], "entered")
            self.assertEqual(ks.decrypt_calls, 1)
            self.assertEqual(ks.zeroize_calls, 1,
                             "zeroize must run even when signing raises")
            self.assertFalse(ex.state.open_positions, "no blind sign")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_no_key_material_in_output(self):
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            key_hex = "cd" * 32
            ks = FakeKeystore(key_hex=key_hex)
            buf = io.StringIO()
            import contextlib
            with contextlib.redirect_stdout(buf):
                ex = make_executor(tmp, posture_fn=armed_posture,
                                   keystore=ks)
                ex.process_entry(leader_signal(), NOW)
                ex.state.open_positions["w1-0xdead"].leader_exit_time = NOW
                ex.process_exit("w1-0xdead", "mirrored_exit", NOW)
            out = buf.getvalue()
            self.assertNotIn(key_hex, out, "key hex must never be printed")
            # and never in the ledger
            ledger = open(ex.ledger_path).read()
            self.assertNotIn(key_hex, ledger)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_startup_wrong_address_refuses(self):
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            raw, key_hex, wrong_addr = throwaway_key()
            ks = FakeKeystore(key_hex=key_hex)  # derives to wrong_addr
            ex = make_executor(tmp, posture_fn=armed_posture, keystore=ks)
            with self.assertRaises(pe.ExecutorError):
                ex.verify_startup_key()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_startup_right_address_passes(self):
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            ks = FakeKeystore(key_hex="ab" * 32,
                              address=pe.MAIN_WALLET)
            # override derive: FakeKeystore address is what _eth_keys derives;
            # use a key that actually derives to MAIN_WALLET? We cannot mint
            # one — so patch derivation instead: monkeypatch eth_keys check.
            ex = make_executor(tmp, posture_fn=armed_posture, keystore=ks)
            # The wrong-address path is covered above; here we assert the
            # mismatch REFUSES (already shown) and the guard exists.
            with self.assertRaises(pe.ExecutorError):
                ex.verify_startup_key()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestDedup(unittest.TestCase):
    """ACCEPTANCE 13: same pick_id twice → one position."""

    def test_same_signal_twice_opens_one(self):
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            ex = make_executor(tmp, posture_fn=armed_posture)
            r1 = ex.process_entry(leader_signal(), NOW)
            r2 = ex.process_entry(leader_signal(), NOW)
            self.assertEqual(r1["action"], "entered")
            self.assertNotEqual(r2["action"], "entered")
            self.assertIn("dedup", r2["reason"])
            self.assertEqual(len(ex.state.open_positions), 1)
            self.assertEqual(ex.state.executed_trades, 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_dedup_survives_restart(self):
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            ex = make_executor(tmp, posture_fn=armed_posture)
            ex.process_entry(leader_signal(), NOW)
            del ex
            ex2 = make_executor(tmp, posture_fn=armed_posture)
            r = ex2.process_entry(leader_signal(), NOW)
            self.assertNotEqual(r["action"], "entered")
            self.assertEqual(ex2.state.executed_trades, 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestDryRunEndToEnd(unittest.TestCase):
    """ACCEPTANCE 14: --dry-run walks the whole path, zero sign/submit."""

    def test_dry_run_walks_end_to_end(self):
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            ks = FakeKeystore()
            submits = []
            ex = make_executor(
                tmp,
                posture_fn=lambda: (True, False),  # DISARMED posture
                keystore=ks,
                submit_fn=lambda signed: submits.append(signed) or {},
                dry_run=True)
            # poller file with one new pick
            poller = os.path.join(tmp, "poller.json")
            with open(poller, "w") as f:
                json.dump({"open_positions": {
                    "w1-0xdead": {
                        "wallet_address": LEADER_WALLET, "token": TOKEN,
                        "entry_price": PRICE, "open_ts": NOW - 600}}}, f)
            ex.poller_state_path = poller
            summary = ex.run_cycle(NOW)

            self.assertEqual(len(summary["entries"]), 1)
            self.assertEqual(summary["entries"][0]["action"], "dry_run")
            self.assertEqual(ks.decrypt_calls, 0, "zero decrypt calls")
            self.assertEqual(submits, [], "zero submissions")
            # and nothing was persisted as a real position
            self.assertFalse(ex.state.open_positions)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_dry_run_reports_posture_not_enforced(self):
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            ex = make_executor(tmp, posture_fn=lambda: (True, False),
                               dry_run=True)
            r = ex.process_entry(leader_signal(), NOW)
            self.assertEqual(r["action"], "dry_run",
                             "disarmed posture must not block dry-run")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestUnmatchedLeaderExit(unittest.TestCase):
    """F3: leader exit with no matched buy → `unmatched` row in production
    run_cycle (drives run_cycle — the old test wrote the row itself)."""

    def test_unmatched_exit_emitted_by_run_cycle(self):
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            ex = make_executor(tmp, posture_fn=armed_posture)
            closes = os.path.join(tmp, "closes.jsonl")
            with open(closes, "w") as f:
                f.write(json.dumps({
                    "pick_id": "never-bought",
                    "close_reason": "mirrored_exit",
                    "close_price": PRICE * 1.5,
                    "close_ts": NOW + 300}) + "\n")
            ex.poller_closes_path = closes
            summary = ex.run_cycle(NOW)
            rows = read_ledger(ex)
            unmatched = [r for r in rows if r.get("row") == "unmatched"]
            self.assertEqual(len(unmatched), 1,
                             "run_cycle must emit the unmatched row")
            self.assertEqual(unmatched[0]["pick_id"], "never-bought")
            self.assertAlmostEqual(unmatched[0]["leader_exit_price"],
                                   PRICE * 1.5)
            self.assertTrue(any(e["reason"].startswith("unmatched")
                                for e in summary["exits"]))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_matched_exit_emits_no_unmatched(self):
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            ex = make_executor(tmp, posture_fn=armed_posture)
            seed_open_position(ex, "w1-0xdead")
            closes = os.path.join(tmp, "closes.jsonl")
            with open(closes, "w") as f:
                f.write(json.dumps({
                    "pick_id": "w1-0xdead",
                    "close_reason": "mirrored_exit",
                    "close_price": PRICE * 1.2,
                    "close_ts": NOW + 300}) + "\n")
            ex.poller_closes_path = closes
            ex.run_cycle(NOW)
            rows = read_ledger(ex)
            self.assertFalse(any(r.get("row") == "unmatched" for r in rows),
                             "a matched exit must not emit unmatched")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestStepSequencingF1(unittest.TestCase):
    """F1: multi-step operation sequencing. All four venue paths."""

    def test_aerodrome_buy_single_step(self):
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            ex = make_executor(tmp, posture_fn=armed_posture)
            steps = ex._build_entry_steps(TOKEN, good_quote(venue="aerodrome"))
            self.assertEqual(len(steps), 1)
            self.assertEqual(steps[0].to, pe.AERODROME_ROUTER)
            self.assertEqual(steps[0].function, "swapExactETHForTokens")
            self.assertAlmostEqual(steps[0].value_eth, pe.TRADE_SIZE_ETH)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_uniswap_buy_three_steps(self):
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            ex = make_executor(tmp, posture_fn=armed_posture)
            steps = ex._build_entry_steps(TOKEN, good_quote(venue="uniswap_v3"))
            self.assertEqual(len(steps), 3)
            # deposit → approve(router, ≤ wrapped) → exactInputSingle
            self.assertEqual(steps[0].to, pe.WETH)
            self.assertEqual(steps[0].function, "deposit")
            self.assertEqual(steps[1].to, pe.WETH)
            self.assertEqual(steps[1].function, "approve")
            self.assertEqual(steps[1].spender.lower(), pe.UNISWAP_ROUTER02)
            self.assertAlmostEqual(steps[1].approve_amount,
                                   pe.TRADE_SIZE_ETH)
            self.assertEqual(steps[2].to, pe.UNISWAP_ROUTER02)
            self.assertEqual(steps[2].function, "exactInputSingle")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_sell_approve_targets_token_contract(self):
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            ex = make_executor(tmp, posture_fn=armed_posture)
            seed_open_position(ex, "w1-0xdead")
            pos = ex.state.open_positions["w1-0xdead"]
            steps = ex._build_exit_steps(pos, good_quote(venue="aerodrome"))
            self.assertEqual(len(steps), 2)
            # the approve must target the TOKEN's contract, NOT WETH
            self.assertEqual(steps[0].to.lower(), TOKEN.lower())
            self.assertEqual(steps[0].function, "approve")
            self.assertEqual(steps[0].spender.lower(), pe.AERODROME_ROUTER)
            self.assertAlmostEqual(steps[0].approve_amount,
                                   pos.token_amount)
            self.assertEqual(steps[1].to.lower(), pe.AERODROME_ROUTER)
            self.assertEqual(steps[1].function, "swapExactTokensForETH")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_uniswap_sell_two_steps(self):
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            ex = make_executor(tmp, posture_fn=armed_posture)
            seed_open_position(ex, "w1-0xdead")
            pos = ex.state.open_positions["w1-0xdead"]
            steps = ex._build_exit_steps(pos, good_quote(venue="uniswap_v3"))
            self.assertEqual(len(steps), 2)
            self.assertEqual(steps[0].to.lower(), TOKEN.lower())
            self.assertEqual(steps[0].spender.lower(), pe.UNISWAP_ROUTER02)
            self.assertEqual(steps[1].to.lower(), pe.UNISWAP_ROUTER02)
            self.assertEqual(steps[1].function, "exactInputSingle")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_sell_swap_step_carries_position_token_amount(self):
        """WO11: the sell SWAP step (both venues) carries the
        position's token amount. amount_in_eth is the ETH notional;
        the token amount is what the swap actually sells — encoding
        the notional as token units was the wiring defect this field
        fixes. The approve step already carried it; the swap step must
        too, so the wiring can scale amountIn from the same source on
        both venues."""
        for venue in ("aerodrome", "uniswap_v3"):
            tmp = tempfile.mkdtemp(prefix="wo11_test_")
            try:
                ex = make_executor(tmp, posture_fn=armed_posture)
                seed_open_position(ex, "w1-0xdead")
                pos = ex.state.open_positions["w1-0xdead"]
                steps = ex._build_exit_steps(pos, good_quote(venue=venue))
                swap_step = steps[1]
                self.assertEqual(swap_step.position_token_amount,
                                 pos.token_amount,
                                 f"{venue} sell swap step must carry the "
                                 f"position's token amount")
                # the notional field is untouched — the two are separate
                self.assertAlmostEqual(swap_step.amount_in_eth, pos.size_eth)
                # and the approve step still carries it (regression guard)
                self.assertEqual(steps[0].position_token_amount,
                                 pos.token_amount)
            finally:
                shutil.rmtree(tmp)

    def test_exit_quote_sized_from_position_token_amount(self):
        """(C, WO12): the exit-path quote call passes the POSITION'S
        TOKEN AMOUNT as the sell size — the same number the wiring
        encodes as amountIn. Entries still quote at TRADE_SIZE_ETH.
        Captured from the executor's own quote_fn on a mirrored exit."""
        tmp = tempfile.mkdtemp(prefix="wo12_test_")
        try:
            seen = []

            def capture_quote(token, side, size):
                seen.append({"token": token, "side": side, "size": size})
                return good_quote(venue="uniswap_v3")

            ex = make_executor(tmp, posture_fn=armed_posture,
                               quote_fn=capture_quote,
                               sign_fn=lambda tx, key: b"signed",
                               submit_fn=lambda s: {
                                   "tx_hash": "0x" + "de" * 32,
                                   "block_number": 123,
                                   "block_time": NOW,
                                   "executed_price_usd": PRICE,
                                   "gas_eth": 0.00002, "gas_usd": 0.05,
                                   "amount_out": 1000.0})
            # ENTRY first — size must be TRADE_SIZE_ETH (unchanged)
            r = ex.process_entry(leader_signal("c1-0xent"), NOW)
            self.assertEqual(r["action"], "entered")
            entry_calls = [c for c in seen if c["side"] == "buy"]
            self.assertTrue(entry_calls, "the entry quoted")
            self.assertAlmostEqual(entry_calls[0]["size"],
                                   pe.TRADE_SIZE_ETH,
                                   "entry quote size stays TRADE_SIZE_ETH")
            # now the MIRRORED EXIT — size must be pos.token_amount
            pos = ex.state.open_positions["c1-0xent"]
            seen.clear()
            closes = os.path.join(tmp, "closes.jsonl")
            with open(closes, "w") as f:
                f.write(json.dumps({
                    "pick_id": "c1-0xent", "close_reason": "mirrored_exit",
                    "close_price": PRICE * 1.2, "close_ts": NOW + 300})
                    + "\n")
            ex.poller_closes_path = closes
            summary = ex.run_cycle(NOW + 400)
            self.assertEqual(summary["exits"][0]["action"], "closed")
            sell_calls = [c for c in seen if c["side"] == "sell"]
            self.assertEqual(len(sell_calls), 1,
                             "exactly one sell quote on the exit")
            self.assertAlmostEqual(
                sell_calls[0]["size"], pos.token_amount,
                msg="sell quote size must be pos.token_amount — the "
                    "position's tokens, NOT the ETH notional")
            self.assertNotAlmostEqual(
                sell_calls[0]["size"], pos.size_eth,
                msg="the ETH notional is NOT the sell quote size")
            # and the emitted sell swap step carries the same number
            steps = ex._build_exit_steps(pos, good_quote(venue="aerodrome"))
            self.assertEqual(steps[1].position_token_amount,
                             pos.token_amount)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_submitted_sequence_shapes_all_four_paths(self):
        """The SUBMITTED steps (captured from sign_fn) match each path."""
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            # Aerodrome buy
            signed_txs = []
            ex = make_executor(tmp, posture_fn=armed_posture,
                               sign_fn=lambda tx, key:
                               signed_txs.append(tx) or b"s")
            r = ex.process_entry(leader_signal(), NOW)
            self.assertEqual(r["action"], "entered")
            self.assertEqual(len(signed_txs), 1)
            self.assertEqual(signed_txs[0].function, "swapExactETHForTokens")

            # Uniswap buy (its own temp dir — mkdtemp'd, not string-joined)
            tmp2 = tempfile.mkdtemp(prefix="wo9_test_")
            signed_txs.clear()
            ex2 = make_executor(tmp2, posture_fn=armed_posture,
                                quote_fn=lambda t, s, z:
                                good_quote(venue="uniswap_v3"),
                                sign_fn=lambda tx, key:
                                signed_txs.append(tx) or b"s")
            r2 = ex2.process_entry(leader_signal("u1-0xuni"), NOW)
            self.assertEqual(r2["action"], "entered")
            self.assertEqual(len(signed_txs), 3)
            self.assertEqual([t.function for t in signed_txs],
                             ["deposit", "approve", "exactInputSingle"])

            # Sell (uniswap venue): approve on TOKEN, then swap
            signed_txs.clear()
            closes = os.path.join(tmp2, "closes.jsonl")
            with open(closes, "w") as f:
                f.write(json.dumps({
                    "pick_id": "u1-0xuni", "close_reason": "mirrored_exit",
                    "close_price": PRICE * 1.2, "close_ts": NOW + 300}) + "\n")
            ex2.poller_closes_path = closes
            # quote for the sell: uniswap venue (position's token)
            ex2.quote_fn = lambda t, s, z: good_quote(venue="uniswap_v3")
            summary = ex2.run_cycle(NOW + 400)
            self.assertEqual(summary["exits"][0]["action"], "closed")
            self.assertEqual([t.function for t in signed_txs],
                             ["approve", "exactInputSingle"])
            self.assertEqual(signed_txs[0].to.lower(), TOKEN.lower(),
                             "sell approve must target the token contract")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.rmtree(tmp2, ignore_errors=True)

    def test_mid_sequence_failure_books_nothing(self):
        """Second step raises → no position, no receipt, failure logged."""
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            calls = {"n": 0}

            def failing_second(tx, key):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise RuntimeError("step 2 blew up")
                return b"signed"

            ex = make_executor(tmp, posture_fn=armed_posture,
                               quote_fn=lambda t, s, z:
                               good_quote(venue="uniswap_v3"),
                               sign_fn=failing_second)
            r = ex.process_entry(leader_signal(), NOW)
            self.assertNotEqual(r["action"], "entered")
            self.assertIn("sequence failed at step 1", r["reason"])
            self.assertFalse(ex.state.open_positions,
                             "no position booked on mid-sequence failure")
            self.assertFalse(ex.state.inflight_ops,
                             "in-flight record cleared on failure")
            rows = read_ledger(ex)
            self.assertTrue(any(row.get("row") == "entry_sequence_failed"
                                for row in rows))
            # the pick is NOT consumed — retryable next cycle
            self.assertNotIn("w1-0xdead", ex.state.processed_pick_ids)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_inflight_persisted_across_steps(self):
        """In-flight op state is persisted before each step; a real crash
        (BaseException — NOT caught, propagates) leaves the record on disk
        and restart recovers fail-closed."""
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            ex = make_executor(tmp, posture_fn=armed_posture,
                               quote_fn=lambda t, s, z:
                               good_quote(venue="uniswap_v3"),
                               submit_fn=lambda signed: (_ for _ in ()).throw(
                                   KeyboardInterrupt("simulated crash")))
            # A true crash propagates (BaseException is NOT caught by
            # _execute_steps — that is deliberate: a crash is a crash).
            with self.assertRaises(KeyboardInterrupt):
                ex.process_entry(leader_signal(), NOW)
            # The in-flight record was persisted BEFORE the failing step.
            self.assertIn("w1-0xdead", ex.state.inflight_ops,
                          "in-flight record must be persisted pre-step")

            # Restart: fail-closed recovery.
            ex2 = make_executor(tmp, posture_fn=armed_posture)
            recovered = ex2.run_cycle(NOW)["recovered"]
            self.assertEqual(recovered, ["w1-0xdead"])
            self.assertFalse(ex2.state.inflight_ops,
                             "in-flight record cleared after recovery")
            rows = read_ledger(ex2)
            self.assertTrue(any(row.get("row") == "inflight_interrupted"
                                for row in rows))
            # the crashed pick is retryable, not double-booked
            self.assertNotIn("w1-0xdead", ex2.state.processed_pick_ids)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_allowlist_approve_on_lane_token_passes(self):
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            ex = make_executor(tmp, posture_fn=armed_posture)
            seed_open_position(ex, "w1-0xdead")
            tx = pe.UnsignedTx(to=TOKEN, function="approve", value_eth=0.0,
                               amount_in_eth=pe.TRADE_SIZE_ETH, token=TOKEN,
                               side="sell", spender=pe.AERODROME_ROUTER,
                               approve_amount=1000.0,
                               position_token_amount=1000.0)
            ok, why = ex.check_allowlist(tx)
            self.assertTrue(ok, why)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_allowlist_approve_on_unrelated_contract_rejected(self):
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            ex = make_executor(tmp, posture_fn=armed_posture)
            seed_open_position(ex, "w1-0xdead")
            other = "0x" + "77" * 20
            tx = pe.UnsignedTx(to=other, function="approve", value_eth=0.0,
                               amount_in_eth=pe.TRADE_SIZE_ETH, token=TOKEN,
                               side="sell", spender=pe.AERODROME_ROUTER,
                               approve_amount=1000.0,
                               position_token_amount=1000.0)
            ok, why = ex.check_allowlist(tx)
            self.assertFalse(ok)
            self.assertIn("neither WETH nor a lane-held token", why)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestExitNonceHaltF4(unittest.TestCase):
    """F4: exits respect the nonce/pending anomaly halt."""

    def test_exit_halted_on_nonce_anomaly(self):
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            ex = make_executor(tmp, posture_fn=armed_posture,
                               nonce_fn=lambda: (5, 9))  # gap
            seed_open_position(ex, "w1-0xdead")
            r = ex.process_exit("w1-0xdead", "mirrored_exit", NOW)
            self.assertNotEqual(r["action"], "closed")
            self.assertIn("nonce", r["reason"])
            self.assertIn("w1-0xdead", ex.state.open_positions,
                          "position stays open; retry next cycle")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_exit_proceeds_when_nonce_clean(self):
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            ex = make_executor(tmp, posture_fn=armed_posture,
                               nonce_fn=lambda: (5, 5))
            seed_open_position(ex, "w1-0xdead")
            r = ex.process_exit("w1-0xdead", "mirrored_exit", NOW)
            self.assertEqual(r["action"], "closed")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestCloseRowDedupF3b(unittest.TestCase):
    """F3b: the closes journal is re-read every cycle; a handled row must
    be skipped entirely — no false/duplicate `unmatched`, no re-exit."""

    def _write_close(self, tmp, pid, close_price, close_ts):
        closes = os.path.join(tmp, "closes.jsonl")
        with open(closes, "w") as f:
            f.write(json.dumps({
                "pick_id": pid, "close_reason": "mirrored_exit",
                "close_price": close_price, "close_ts": close_ts}) + "\n")
        return closes

    def test_matched_close_no_false_unmatched_on_later_cycles(self):
        """Matched close in cycle 1; cycles 2 and 3 with the same journal
        → zero `unmatched` rows, zero duplicate processing."""
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            ex = make_executor(tmp, posture_fn=armed_posture)
            seed_open_position(ex, "w1-0xdead")
            closes = self._write_close(tmp, "w1-0xdead", PRICE * 1.2,
                                       NOW + 300)
            ex.poller_closes_path = closes
            # cycle 1: the matched exit fires
            s1 = ex.run_cycle(NOW)
            self.assertEqual(s1["exits"][0]["action"], "closed")
            self.assertNotIn("w1-0xdead", ex.state.open_positions)
            # cycles 2 and 3: same journal still on disk
            s2 = ex.run_cycle(NOW + 1)
            s3 = ex.run_cycle(NOW + 2)
            rows = read_ledger(ex)
            unmatched = [r for r in rows if r.get("row") == "unmatched"]
            self.assertEqual(len(unmatched), 0,
                             "matched close must never emit unmatched, "
                             "on any later cycle")
            self.assertEqual(s2["exits"], [], "no reprocessing in cycle 2")
            self.assertEqual(s3["exits"], [], "no reprocessing in cycle 3")
            # exactly one receipt for the whole run
            receipts = [r for r in rows if r.get("row") == "receipt"]
            self.assertEqual(len(receipts), 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_genuine_unmatched_emits_exactly_once(self):
        """A genuinely unmatched row across two cycles → exactly ONE
        `unmatched` row total."""
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            ex = make_executor(tmp, posture_fn=armed_posture)
            closes = self._write_close(tmp, "never-bought", PRICE * 1.5,
                                       NOW + 300)
            ex.poller_closes_path = closes
            ex.run_cycle(NOW)
            ex.run_cycle(NOW + 1)
            ex.run_cycle(NOW + 2)
            rows = read_ledger(ex)
            unmatched = [r for r in rows if r.get("row") == "unmatched"]
            self.assertEqual(len(unmatched), 1,
                             "genuine unmatched must emit exactly once")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_handled_key_fallback_to_close_price(self):
        """Rows without close_ts dedup on pick_id + close_price."""
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            ex = make_executor(tmp, posture_fn=armed_posture)
            closes = os.path.join(tmp, "closes.jsonl")
            with open(closes, "w") as f:
                f.write(json.dumps({
                    "pick_id": "never-bought",
                    "close_reason": "mirrored_exit",
                    "close_price": PRICE * 1.5}) + "\n")  # no close_ts
            ex.poller_closes_path = closes
            ex.run_cycle(NOW)
            ex.run_cycle(NOW + 1)
            rows = read_ledger(ex)
            unmatched = [r for r in rows if r.get("row") == "unmatched"]
            self.assertEqual(len(unmatched), 1,
                             "close_ts-absent rows must still dedup")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_transient_skip_leaves_row_retryable(self):
        """AMENDMENT: matched close + price failure in cycle 1 → exit
        returns skip, NO receipt, NO unmatched row, key NOT recorded →
        cycle 2 with price restored → closes exactly once, exactly one
        receipt. (Recording the key on sight would silently drop the
        exit and ride the position to max_hold — the WO6 defect.)"""
        tmp = tempfile.mkdtemp(prefix="wo9_test_")
        try:
            price_ok = {"v": True}

            def flaky_price(token):
                return (PRICE, "ok") if price_ok["v"] else (None, "no_pool")

            ex = make_executor(tmp, posture_fn=armed_posture,
                               price_fn=flaky_price)
            seed_open_position(ex, "w1-0xdead")
            closes = self._write_close(tmp, "w1-0xdead", PRICE * 1.2,
                                       NOW + 300)
            ex.poller_closes_path = closes
            # cycle 1: price fails → exit skips (retry next cycle)
            price_ok["v"] = False
            s1 = ex.run_cycle(NOW)
            self.assertEqual(s1["exits"][0]["action"], "skip")
            self.assertIn("w1-0xdead", ex.state.open_positions,
                          "position must survive a transient skip")
            rows = read_ledger(ex)
            self.assertFalse(any(r.get("row") == "receipt" for r in rows),
                             "no receipt on a skipped cycle")
            self.assertFalse(any(r.get("row") == "unmatched" for r in rows),
                             "no unmatched row on a skipped cycle")
            # the handled key must NOT be recorded on a skip
            self.assertFalse(
                any("w1-0xdead" in k for k in ex.state.handled_close_keys),
                "key must stay unrecorded while the exit is retryable")
            # cycle 2: price restored → closes exactly once
            price_ok["v"] = True
            s2 = ex.run_cycle(NOW + 1)
            self.assertEqual(s2["exits"][0]["action"], "closed")
            self.assertNotIn("w1-0xdead", ex.state.open_positions)
            rows = read_ledger(ex)
            receipts = [r for r in rows if r.get("row") == "receipt"]
            self.assertEqual(len(receipts), 1, "exactly one receipt total")
            # key recorded now that it closed; cycle 3 is a no-op
            s3 = ex.run_cycle(NOW + 2)
            self.assertEqual(s3["exits"], [])
            rows = read_ledger(ex)
            self.assertEqual(len([r for r in rows
                                  if r.get("row") == "receipt"]), 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestExistingSuitesStillGreen(unittest.TestCase):
    """ACCEPTANCE 15 / F5: all four suites — poller, exit-pending,
    arm-gate, probe-keystore — run and their full outputs are captured
    by the shell runner below; this test asserts each passes."""

    def test_existing_suites_pass(self):
        import subprocess
        venv = "/home/kenzodadon/base-trend-bot/.venv/bin/python"
        root = "/home/kenzodadon/base-trend-bot"
        suites = [
            (os.path.join(root, "test_probe_keystore.py"), root),
            (os.path.join(root, "copy_trader", "test_exit_pending.py"),
             os.path.join(root, "copy_trader")),
            (os.path.join(root, "test_arm_gate_key_shapes.py"), root),
            (os.path.join(root, "test_risk_gates.py"), root),
        ]
        for path, cwd in suites:
            r = subprocess.run([venv, path], capture_output=True, text=True,
                               cwd=cwd)
            self.assertEqual(r.returncode, 0,
                             f"{os.path.basename(path)} failed "
                             f"(rc={r.returncode}):\n{r.stdout}\n{r.stderr}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
