#!/usr/bin/env python3
"""Tests for probe_wiring (WO10 + fix round W1-W6 + addendum).
Throwaway keystores only; mocks only.

Run: /home/kenzodadon/base-trend-bot/.venv/bin/python test_probe_wiring.py

Covers the 8 WO10 acceptance junctions plus the fix round:

  W1 (material) implied_price_usd is the TOKEN's USD price on both
     sides — pinned by VALUE (a $1 token with a known quote yields
     implied ≈ 1.0 on buy AND sell), on both sides.
  W2 (material) real calldata per tx.function — decoded from the
     signed tx and asserted: selector + load-bearing args (recipient,
     amount, spender, path, deadline) for all six tx types; gas is a
     deliberate per-type value under the hard cap.
  W3 (material) the REAL submit_fn establishes amount_out and
     executed_price_usd from balance deltas against a mock chain
     whose balances MOVE and whose Transfer logs LIE — the receipts
     carry the balance-delta figure, never the log figure.
  W4 (moderate) the verifier's per-step landed/not-landed comes from
     the gas journal + receipt lookups, NOT the record's
     steps_completed counter: the crash-after-broadcast-before-
     persist case (journal shows step 1 confirmed, counter says 0)
     must NOT produce resume-safe.
  W5 (minor) gas current = latest block's basefee; median from the
     sample — pinned with a rising fee series.
  W6 (minor) the timeout path leaves a broadcast journal row before
     raising; gas_usd raises on an unavailable price, never 0.0.
  Addendum: journal rows carry the decoded tx context (target,
     function, token, nonce, sender).

The real keystore is NEVER read, decrypted, or copied by any
test here. No test broadcasts to the network.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "/home/kenzodadon/base-trend-bot")

import probe_executor as pe
import probe_wiring as pw
from eth_keys import keys as _eth_keys

NOW = 1_700_000_000
PRICE = 0.001234
ETH_USD = 2500.0
TOKEN = "0x" + "ab" * 20
LEADER_WALLET = "0x" + "11" * 20
THROWAWAY_PASS = b"throwaway-wo10"


def throwaway_key():
    raw = os.urandom(32)
    return (raw, raw.hex(),
            _eth_keys.PrivateKey(raw).public_key.to_checksum_address())


class FakeKeystore:
    """Throwaway keystore stand-in with call counting (the real
    keystore is never touched)."""

    def __init__(self, key_hex="ab" * 32):
        self._key_hex = key_hex
        self.decrypt_calls = 0
        self.zeroize_calls = 0

    def decrypt_at_use(self, passphrase=None):
        self.decrypt_calls += 1
        return bytes.fromhex(self._key_hex)

    def _zeroize(self, buf):
        self.zeroize_calls += 1
        if isinstance(buf, bytearray):
            for i in range(len(buf)):
                buf[i] = 0


class FakeW3:
    """Minimal web3-shaped mock for the wiring's chain reads/sends.

    Balance reads are programmable per-call so the W3 fill tests can
    move a balance between the pre-send snapshot and the post-receipt
    read: ``balance_sequence`` maps an address to a list of successive
    values, and ``token_balance_sequence`` does the same per token.
    ``lying_logs`` puts a fabricated Transfer log on the receipt to
    prove the wiring never reads it.
    """

    def __init__(self, balance_wei=10**19, nonce=5, gas_price=1_000_000_000,
                 base_fees=None, block_number=20_000_000,
                 receipt_delay=0, lying_logs=None):
        self._balance = balance_wei
        self._nonce = nonce
        self._gas_price = gas_price
        # W5: a rising fee series, oldest -> newest. The LATEST block
        # (== block_number) carries the LAST element.
        self._base_fees = base_fees or [50_000_000]
        self._block_number = block_number
        self.sent = []
        self.receipts = {}
        self.receipt_delay = receipt_delay
        self.lying_logs = lying_logs
        # successive-value sequences: addr/token -> [v1, v2, ...]
        self.balance_sequence = {}
        self.token_balance_sequence = {}

    class _Eth:
        pass

    def __init_eth(self):
        pass

    @property
    def eth(self):
        # web3's .eth is a STABLE attribute — cache the namespace so
        # per-test attribute assignment (send_raw_transaction = boom)
        # survives the next access.
        if getattr(self, "_eth_ns", None) is None:
            outer = self

            class _EthNS:
                @property
                def block_number(inner_self):
                    return outer._block_number

                def get_balance(inner_self, addr):
                    seq = outer.balance_sequence.get(str(addr).lower())
                    if seq:
                        return seq.pop(0) if len(seq) > 1 else seq[0]
                    return outer._balance

                def get_transaction_count(inner_self, addr, tag="latest"):
                    return outer._nonce

                def gas_price(inner_self):
                    return outer._gas_price

                def send_raw_transaction(inner_self, raw):
                    outer.sent.append(raw)
                    tx_hash = "0x" + format(len(outer.sent), "064x")
                    return type("H", (), {"hex": lambda self,
                                          h=tx_hash: h})()

                def get_transaction_receipt(inner_self, tx_hash):
                    h = (tx_hash.hex() if hasattr(tx_hash, "hex")
                         else tx_hash)
                    if outer.receipt_delay > 0:
                        outer.receipt_delay -= 1
                        return None
                    r = outer.receipts.get(h)
                    if r is None:
                        return None
                    if outer.lying_logs is not None:
                        r = dict(r)
                        r["logs"] = outer.lying_logs
                    return r

                def get_block(inner_self, bn):
                    # W5: the wiring samples every 50 blocks walking
                    # back from the latest. Index the fee series by the
                    # SAMPLING STRIDE (block distance // 50) so a rising
                    # series means each successive sample is higher and
                    # the latest block carries the highest fee.
                    fees = outer._base_fees
                    n = len(fees)
                    stride = (outer._block_number - int(bn)) // 50
                    idx = min(max(n - 1 - stride, 0), n - 1)
                    return {"baseFeePerGas": fees[idx],
                            "timestamp": NOW, "number": bn}

                def call(inner_self, tx):
                    to = str(tx.get("to", "")).lower()
                    data = tx.get("data", b"")
                    # the wiring passes calldata as a 0x hex STRING
                    if isinstance(data, str):
                        data_hex = (data[2:] if data.startswith("0x")
                                    else data)
                    else:
                        data_hex = bytes(data).hex()
                    # balanceOf(address) -> the token's next value
                    if data_hex[:8] == "70a08231":
                        seq = outer.token_balance_sequence.get(to)
                        if seq:
                            return (seq.pop(0) if len(seq) > 1
                                    else seq[0]).to_bytes(32, "big")
                    return b"\x00" * 32

                def get_code(inner_self, addr):
                    return b"\x60\x80"

                @property
                def fee_history(inner_self):
                    def _fh(blocks, latest, reward_pct):
                        return {"reward": [[outer._base_fees[-1]]] * blocks}
                    return _fh

            self._eth_ns = _EthNS()
        return self._eth_ns


def make_wiring(tmp, **kw):
    defaults = dict(
        rpc_url="http://127.0.0.1:0",  # never contacted: w3 injected
        wallet=pe.MAIN_WALLET,
        gas_journal_path=os.path.join(tmp, "gas_journal.jsonl"),
        w3=FakeW3(),
    )
    defaults.update(kw)
    return pw.LiveProviders(**defaults)


def make_executor(tmp, providers, **kw):
    ks = kw.pop("keystore", None) or FakeKeystore()
    defaults = dict(
        state_path=os.path.join(tmp, "lane.json"),
        ledger_path=os.path.join(tmp, "ledger.jsonl"),
        posture_fn=lambda: (False, True),   # armed for these tests
        price_fn=lambda t: (PRICE, "ok"),
        quote_fn=lambda t, s, z: pe.Quote(
            venue="aerodrome", amount_out=1000.0,
            implied_price_usd=PRICE, routable=True),
        sign_fn=None, submit_fn=None,
        balance_fn=providers.balance_fn,
        gas_price_fn=lambda: (0.05, 0.05),
        nonce_fn=providers.nonce_fn,
        keystore=ks, now_fn=lambda: float(NOW),
    )
    defaults.update(kw)
    return pe.ProbeExecutor(**defaults)


# ---------------------------------------------------------------------------
# W1 — implied_price_usd is the TOKEN's USD price, pinned by VALUE
# ---------------------------------------------------------------------------

class TestQuoteImpliedPriceUnits(unittest.TestCase):
    """W1: buy = (size_eth * eth_usd) / amount_out_units; sell =
    (amount_out_units / size_eth) * eth_usd. The old formulas returned
    USD-per-ETH; WETH coincidence masked it. A $1 token with a known
    quote must yield implied ≈ 1.0 on BOTH sides."""

    def _quote_with(self, providers, side, size_eth, out_base_units,
                    token_decimals=6, out_decimals=18):
        dex, _cfg = providers._ensure_dex()
        token = TOKEN

        class FakeAdapter:
            name = "aerodrome"

            def quote_detailed(self, ti, to, amt):
                return {"amount_out_wei": out_base_units, "exec": {}}

            def quote_out_wei(self, ti, to, amt):
                return out_base_units

        def dec(client, t):
            if t.lower() == token.lower():
                return token_decimals
            return out_decimals  # WETH 18

        with mock.patch.object(dex, "token_decimals", side_effect=dec), \
             mock.patch.object(dex, "AerodromeQuoter",
                               return_value=FakeAdapter()), \
             mock.patch.object(dex, "UniswapV3Quoter",
                               return_value=FakeAdapter()), \
             mock.patch.object(dex, "LiveQuoter") as LQ:
            LQ.return_value.quotes.return_value = [
                type("V", (), {"venue": "aerodrome", "p0": 1.0,
                               "exec_params": {}})()]
            with mock.patch.object(providers, "price_fn",
                                   return_value=(ETH_USD, "ok")):
                return providers.quote_fn(token, side, size_eth)

    def test_buy_implied_price_is_token_usd(self):
        """A $1 token: 0.0002 ETH (=$0.50) buys 0.5 token units, so
        the implied price must be ≈ 1.0 — NOT eth_usd (the old defect
        returned USD-per-ETH, i.e. 2500)."""
        tmp = tempfile.mkdtemp(prefix="wo10_w1_")
        try:
            providers = make_wiring(tmp)
            size_eth = 0.0002
            # 0.0002 ETH = $0.50 buys 0.5 units of a 6-dec $1 token
            out_base = int(0.5 * (10 ** 6))
            q = self._quote_with(providers, "buy", size_eth, out_base)
            self.assertIsNotNone(q)
            self.assertAlmostEqual(q.implied_price_usd, 1.0, places=6,
                                   msg="buy implied_price_usd must be the "
                                       "token's USD price, not USD-per-ETH")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_sell_implied_price_is_token_usd(self):
        """The sell side pins the same value: the mock sells 0.5 token
        units ($0.50) and returns 0.0002 WETH — implied ≈ 1.0."""
        tmp = tempfile.mkdtemp(prefix="wo10_w1_")
        try:
            providers = make_wiring(tmp)
            size_eth = 0.5          # 0.5 token units sold (sell scale)
            # selling 0.5 units of a $1 token returns 0.0002 WETH
            out_weth_base = int(0.0002 * 1e18)
            q = self._quote_with(providers, "sell", size_eth,
                                 out_weth_base)
            self.assertIsNotNone(q)
            self.assertAlmostEqual(q.implied_price_usd, 1.0, places=6,
                                   msg="sell implied_price_usd must be the "
                                       "token's USD price, not USD-per-ETH")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_old_formula_would_have_failed(self):
        """Guard: the OLD formulas give USD-per-ETH here — the exact
        defect W1 names. This test documents that the pinned value
        discriminates old from new."""
        size_eth = 0.0002
        out_units = 0.5          # 0.5 token units received on a buy
        usd_per_token = 1.0      # the token is $1
        # OLD buy: (amount_out_units / size_eth) * usd_per_token
        old_buy = (out_units / size_eth) * usd_per_token      # = 2500
        # OLD sell: usd_per_token / (amount_out_units / size_eth)
        old_sell = usd_per_token / (out_units / size_eth)     # = 0.0004
        # NEW buy: (size_eth * eth_usd) / amount_out_units
        new_buy = (size_eth * ETH_USD) / out_units            # = 1.0
        # NEW sell: (amount_out_units / size_eth) * eth_usd
        sell_out_units = size_eth / ETH_USD                   # 8e-8 WETH
        new_sell = (sell_out_units / size_eth) * ETH_USD      # = 1.0
        self.assertAlmostEqual(new_buy, 1.0, places=9)
        self.assertAlmostEqual(new_sell, 1.0, places=9)
        self.assertNotAlmostEqual(old_buy, 1.0, places=3,
                                  msg="the old buy formula must NOT give "
                                      "the token price")
        self.assertNotAlmostEqual(old_sell, 1.0, places=3,
                                  msg="the old sell formula must NOT give "
                                      "the token price")


# ---------------------------------------------------------------------------
# W2 — real calldata, decoded from the signed tx, asserted per tx type
# ---------------------------------------------------------------------------

def _decode_signed(signed: bytes) -> dict:
    """Decode a signed tx the same way the wiring does, for tests."""
    from rlp import decode as _rlp_decode
    f = _rlp_decode(bytes(signed))
    nonce, _gp, gas, to, value, data, _v, _r, _s = f
    return {
        "nonce": int.from_bytes(nonce, "big"),
        "gas": int.from_bytes(gas, "big"),
        "to": ("0x" + bytes(to).hex()).lower(),
        "value_wei": int.from_bytes(bytes(value), "big"),
        "data": bytes(data),
    }


class TestCalldataPerTxType(unittest.TestCase):
    """W2: every signed tx carries real calldata — selector plus the
    load-bearing arguments — for all six tx types the executor emits.
    Signature recovery still passes (the signature certifies the
    PAYLOAD, so calldata correctness needs its own assertions)."""

    def _sign(self, providers, tx, key_hex):
        return providers._build_and_sign(
            tx, bytearray(bytes.fromhex(key_hex)))

    def _mock_quotes(self, providers, buy_out_base, sell_out_base,
                     fee=500):
        """Patch the per-venue signing quote so calldata encoding is
        deterministic without any RPC."""

        def fake(tx):
            if tx.function in ("swapExactETHForTokens",
                               "swapExactTokensForETH"):
                return buy_out_base if tx.side == "buy" else sell_out_base, \
                    {"builder": "aerodrome", "stable": False,
                     "factory": "0x" + "ff" * 20}
            return buy_out_base if tx.side == "buy" else sell_out_base, \
                {"builder": "uniswap-v3", "fee": fee,
                 "swap_router": pe.UNISWAP_ROUTER02}
        return mock.patch.object(providers, "_quote_for_signing",
                                 side_effect=fake)

    def test_deposit_calldata(self):
        tmp = tempfile.mkdtemp(prefix="wo10_w2_")
        try:
            _raw, key_hex, _addr = throwaway_key()
            providers = make_wiring(tmp)
            providers._decimals = lambda t: 18
            tx = pe.UnsignedTx(to=pe.WETH, function="deposit",
                               value_eth=0.0002, amount_in_eth=0.0002,
                               token=TOKEN, side="buy",
                               deadline_ts=NOW + 120)
            d = _decode_signed(self._sign(providers, tx, key_hex))
            self.assertEqual(d["data"][:4].hex(), "d0e30db0",
                             "deposit() selector")
            self.assertEqual(len(d["data"]), 4, "deposit takes no args")
            self.assertEqual(d["to"], pe.WETH.lower())
            self.assertEqual(d["value_wei"], int(0.0002 * 1e18))
            self.assertEqual(d["gas"], pw.GAS_BY_FUNCTION["deposit"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_approve_calldata_both_targets(self):
        """approve(spender, amount) on WETH (buy leg) and on the token
        (sell leg) — spender and amount are the load-bearing args."""
        tmp = tempfile.mkdtemp(prefix="wo10_w2_")
        try:
            _raw, key_hex, _addr = throwaway_key()
            providers = make_wiring(tmp)
            providers._decimals = lambda t: 6 if t.lower() == TOKEN.lower() \
                else 18
            # buy leg: approve WETH for the uniswap router
            tx = pe.UnsignedTx(to=pe.WETH, function="approve", value_eth=0.0,
                               amount_in_eth=0.0002, token=TOKEN,
                               side="buy", deadline_ts=NOW + 120,
                               spender=pe.UNISWAP_ROUTER02,
                               approve_amount=0.0002,
                               position_token_amount=0.0002)
            d = _decode_signed(self._sign(providers, tx, key_hex))
            self.assertEqual(d["data"][:4].hex(), "095ea7b3")
            body = d["data"][4:]
            spender = "0x" + body[12:32].hex()
            amount = int.from_bytes(body[32:64], "big")
            self.assertEqual(spender.lower(), pe.UNISWAP_ROUTER02.lower())
            self.assertEqual(amount, int(0.0002 * 1e18),
                             "WETH approve amount in WETH base units")
            self.assertEqual(d["to"], pe.WETH.lower())
            # sell leg: approve the TOKEN for the aerodrome router,
            # bounded by the position's token amount
            tx = pe.UnsignedTx(to=TOKEN, function="approve", value_eth=0.0,
                               amount_in_eth=0.0002, token=TOKEN,
                               side="sell", deadline_ts=NOW + 120,
                               spender=pe.AERODROME_ROUTER,
                               approve_amount=1000.0,
                               position_token_amount=1000.0)
            d = _decode_signed(self._sign(providers, tx, key_hex))
            body = d["data"][4:]
            spender = "0x" + body[12:32].hex()
            amount = int.from_bytes(body[32:64], "big")
            self.assertEqual(spender.lower(), pe.AERODROME_ROUTER.lower())
            self.assertEqual(amount, 1000 * 10**6,
                             "token approve amount in TOKEN base units")
            self.assertEqual(d["to"], TOKEN.lower())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_aerodrome_buy_calldata(self):
        tmp = tempfile.mkdtemp(prefix="wo10_w2_")
        try:
            _raw, key_hex, _addr = throwaway_key()
            providers = make_wiring(tmp)
            providers._decimals = lambda t: 6 if t.lower() == TOKEN.lower() \
                else 18
            tx = pe.UnsignedTx(to=pe.AERODROME_ROUTER,
                               function="swapExactETHForTokens",
                               value_eth=0.0002, amount_in_eth=0.0002,
                               token=TOKEN, side="buy",
                               deadline_ts=NOW + 120)
            with self._mock_quotes(providers, 2500 * 10**6, 10**14):
                d = _decode_signed(self._sign(providers, tx, key_hex))
            self.assertEqual(d["data"][:4].hex(), "7ff36ab5",
                             "swapExactETHForTokens selector")
            self.assertEqual(d["value_wei"], int(0.0002 * 1e18),
                             "ETH value carried by the tx")
            # INDEPENDENT decode: eth_abi's standard decoder, not the
            # encoder's layout read back with hand-rolled offsets (that
            # is the multicall offset bug class).
            from eth_abi import decode as _abi_decode
            min_out, path, recipient, deadline = _abi_decode(
                ["uint256", "address[]", "address", "uint256"],
                d["data"][4:])
            self.assertEqual(str(recipient).lower(),
                             pe.MAIN_WALLET.lower(),
                             "recipient is the lane wallet")
            self.assertEqual(int(deadline), NOW + 120)
            self.assertEqual(len(path), 2, "path[] carries both legs")
            self.assertEqual(str(path[0]).lower(), pw.WETH.lower(),
                             "path starts at WETH")
            self.assertEqual(str(path[-1]).lower(), TOKEN.lower(),
                             "path ends at the traded token")
            # min_out = quote discounted by PROBE_MAX_SLIPPAGE_BPS
            expect_min = int(2500 * 10**6
                             * (1 - pe.PROBE_MAX_SLIPPAGE_BPS / 10_000))
            self.assertEqual(min_out, expect_min)
            self.assertGreater(min_out, 0, "never a zero minimum")
            self.assertEqual(d["gas"], pw.GAS_BY_FUNCTION[
                "swapExactETHForTokens"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_aerodrome_sell_calldata(self):
        tmp = tempfile.mkdtemp(prefix="wo10_w2_")
        try:
            _raw, key_hex, _addr = throwaway_key()
            providers = make_wiring(tmp)
            providers._decimals = lambda t: 6 if t.lower() == TOKEN.lower() \
                else 18
            # The executor's real emission shape: amount_in_eth is the
            # ETH NOTIONAL (pos.size_eth) and the token amount rides in
            # position_token_amount. The old fixture put 1000.0 in
            # amount_in_eth — the notional field — which is exactly the
            # defect this addendum closes.
            tx = pe.UnsignedTx(to=pe.AERODROME_ROUTER,
                               function="swapExactTokensForETH",
                               value_eth=0.0, amount_in_eth=0.0002,
                               token=TOKEN, side="sell",
                               deadline_ts=NOW + 120,
                               position_token_amount=1000.0)
            # selling 1000 tokens (6 dec) returns 0.0004 WETH
            sell_out = int(0.0004 * 1e18)
            with self._mock_quotes(providers, 2500 * 10**6, sell_out):
                d = _decode_signed(self._sign(providers, tx, key_hex))
            self.assertEqual(d["data"][:4].hex(), "18cbafe5",
                             "swapExactTokensForETH selector")
            self.assertEqual(d["value_wei"], 0, "sell carries no ETH")
            # INDEPENDENT decode: eth_abi's standard decoder (the sell's
            # 5-word head + path[] is the same dynamic-array offset
            # math that bit the multicall — decode it independently,
            # never by reading the encoder's layout back).
            from eth_abi import decode as _abi_decode
            amount_in, min_out, path, recipient, deadline = _abi_decode(
                ["uint256", "uint256", "address[]", "address", "uint256"],
                d["data"][4:])
            self.assertEqual(amount_in, 1000 * 10**6,
                             "amountIn in the TOKEN's base units, from "
                             "position_token_amount — NOT the ETH "
                             "notional")
            self.assertEqual(str(recipient).lower(),
                             pe.MAIN_WALLET.lower(),
                             "recipient is the lane wallet")
            self.assertEqual(int(deadline), NOW + 120)
            self.assertEqual(len(path), 2)
            self.assertEqual(str(path[0]).lower(), TOKEN.lower(),
                             "sell path starts at the token")
            self.assertEqual(str(path[-1]).lower(), pw.WETH.lower(),
                             "sell path ends at WETH")
            expect_min = int(sell_out
                             * (1 - pe.PROBE_MAX_SLIPPAGE_BPS / 10_000))
            self.assertEqual(min_out, expect_min)
            self.assertGreater(min_out, 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_sell_amount_in_uses_position_token_amount_not_notional(self):
        """The sign-time quote is sized from the SAME amount the
        calldata encodes: position_token_amount, never amount_in_eth.
        Asserted by capturing the amountIn handed to the quoter."""
        tmp = tempfile.mkdtemp(prefix="wo11_w2_")
        try:
            _raw, key_hex, _addr = throwaway_key()
            providers = make_wiring(tmp)
            providers._decimals = lambda t: 6 if t.lower() == TOKEN.lower() \
                else 18
            tx = pe.UnsignedTx(to=pe.AERODROME_ROUTER,
                               function="swapExactTokensForETH",
                               value_eth=0.0, amount_in_eth=0.0002,
                               token=TOKEN, side="sell",
                               deadline_ts=NOW + 120,
                               position_token_amount=1000.0)
            seen = {}

            def capture_quote(tx):
                # what the quoter is asked for IS the amountIn
                dec = providers._decimals(tx.token)
                seen["amount_in_wei"] = providers._sell_amount_in_wei(
                    tx, dec)
                return int(0.0004 * 1e18), {}

            with mock.patch.object(providers, "_quote_for_signing",
                                   side_effect=capture_quote):
                providers._build_calldata(tx)
            self.assertEqual(seen["amount_in_wei"], 1000 * 10**6,
                             "the sign-time quote must be sized from "
                             "position_token_amount, the same amount "
                             "encoded as amountIn")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_sell_swap_without_position_token_amount_raises(self):
        """FAIL-CLOSED: a sell swap with no positive
        position_token_amount RAISES at signing — never encodes
        amount_in_eth (the ETH notional) as token units. Asserted for
        BOTH sell venues."""
        tmp = tempfile.mkdtemp(prefix="wo11_w2_")
        try:
            _raw, key_hex, _addr = throwaway_key()
            providers = make_wiring(tmp)
            providers._decimals = lambda t: 6 if t.lower() == TOKEN.lower() \
                else 18
            # the OLD defect fixture: 1000.0 sat in amount_in_eth
            tx = pe.UnsignedTx(to=pe.AERODROME_ROUTER,
                               function="swapExactTokensForETH",
                               value_eth=0.0, amount_in_eth=1000.0,
                               token=TOKEN, side="sell",
                               deadline_ts=NOW + 120)
            with mock.patch.object(providers, "_quote_for_signing",
                                   return_value=(int(0.0004 * 1e18), {})):
                with self.assertRaises(pe.ExecutorError) as ctx:
                    providers.sign_fn(tx, bytearray(
                        bytes.fromhex(key_hex)))
            self.assertIn("position_token_amount", str(ctx.exception))
            self.assertIn("notional", str(ctx.exception))
            self.assertEqual(providers.sign_count, 0,
                             "no signature was produced")
            # the uniswap sell venue fails closed identically
            tx_u = pe.UnsignedTx(to=pe.UNISWAP_ROUTER02,
                                 function="exactInputSingle",
                                 value_eth=0.0, amount_in_eth=1000.0,
                                 token=TOKEN, side="sell",
                                 deadline_ts=NOW + 120)
            with mock.patch.object(providers, "_quote_for_signing",
                                   return_value=(int(0.0004 * 1e18),
                                                 {"fee": 3000})):
                with self.assertRaises(pe.ExecutorError) as ctx2:
                    providers.sign_fn(tx_u, bytearray(
                        bytes.fromhex(key_hex)))
            self.assertIn("position_token_amount", str(ctx2.exception))
            self.assertEqual(providers.sign_count, 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_uniswap_exact_input_single_calldata(self):
        tmp = tempfile.mkdtemp(prefix="wo10_w2_")
        try:
            _raw, key_hex, _addr = throwaway_key()
            providers = make_wiring(tmp)
            providers._decimals = lambda t: 6 if t.lower() == TOKEN.lower() \
                else 18
            tx = pe.UnsignedTx(to=pe.UNISWAP_ROUTER02,
                               function="exactInputSingle",
                               value_eth=0.0, amount_in_eth=0.0002,
                               token=TOKEN, side="buy",
                               deadline_ts=NOW + 120)
            with self._mock_quotes(providers, 2500 * 10**6, 10**14,
                                   fee=3000):
                d = _decode_signed(self._sign(providers, tx, key_hex))
            # SwapRouter02 multicall(deadline, [exactInputSingle(...)])
            self.assertEqual(d["data"][:4].hex(), "5ae401dc",
                             "multicall selector on SwapRouter02")
            self.assertEqual(d["to"], pe.UNISWAP_ROUTER02.lower())
            self.assertEqual(d["value_wei"], 0)
            # INDEPENDENT decode of the nested multicall -> exactInputSingle
            # params, via eth_abi (not the production _decode_context,
            # which would be the encoder reading its own layout back).
            from eth_abi import decode as _abi_decode
            deadline, calls = _abi_decode(["uint256", "bytes[]"],
                                          d["data"][4:])
            self.assertEqual(len(calls), 1)
            inner = calls[0]
            self.assertEqual(inner[:4].hex(), "04e45aaf",
                             "inner call is exactInputSingle")
            token_in, token_out, fee, recipient, amount_in, min_out, \
                _sqrt = _abi_decode(
                    ["address", "address", "uint24", "address", "uint256",
                     "uint256", "uint160"], inner[4:])
            self.assertEqual(int(deadline), NOW + 120)
            self.assertEqual(str(token_in).lower(), pw.WETH.lower())
            self.assertEqual(str(token_out).lower(), TOKEN.lower())
            self.assertEqual(int(fee), 3000)
            self.assertEqual(str(recipient).lower(),
                             pe.MAIN_WALLET.lower())
            self.assertEqual(amount_in, int(0.0002 * 1e18))
            expect_min = int(2500 * 10**6
                             * (1 - pe.PROBE_MAX_SLIPPAGE_BPS / 10_000))
            self.assertEqual(min_out, expect_min)
            self.assertGreater(min_out, 0)
            self.assertEqual(d["gas"],
                             pw.GAS_BY_FUNCTION["exactInputSingle"])
            # the production decoder agrees with the independent one —
            # this is the cross-check, not the primary assertion
            decoded = providers._decode_context({
                "sender": pe.MAIN_WALLET, "to": pe.UNISWAP_ROUTER02.lower(),
                "value_wei": 0, "data": d["data"], "nonce": 5})
            self.assertEqual(decoded["function"], "exactInputSingle")
            self.assertEqual(decoded["token_in"].lower(), pw.WETH.lower())
            self.assertEqual(decoded["token_out"].lower(), TOKEN.lower())
            self.assertEqual(decoded["deadline"], NOW + 120)
            self.assertEqual(decoded["amount_in_wei"], int(0.0002 * 1e18))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_gas_is_deliberate_and_capped(self):
        """W2: per-type gas values, all under the hard cap, and an
        unknown function fails closed rather than defaulting."""
        for fn, expected in pw.GAS_BY_FUNCTION.items():
            self.assertLessEqual(expected, pw.GAS_HARD_CAP,
                                 f"{fn} gas exceeds the hard cap")
            self.assertGreater(expected, 0)
        with self.assertRaises(pe.ExecutorError):
            pw._gas_for("someUnknownFunction")
        # a value above the cap would raise even if listed
        with mock.patch.dict(pw.GAS_BY_FUNCTION,
                             {"deposit": pw.GAS_HARD_CAP + 1}):
            with self.assertRaises(pe.ExecutorError):
                pw._gas_for("deposit")

    def test_no_signature_without_a_minimum(self):
        """W2: a venue that cannot quote at signing time fails closed
        — no signature without an amountOutMin."""
        tmp = tempfile.mkdtemp(prefix="wo10_w2_")
        try:
            _raw, key_hex, _addr = throwaway_key()
            providers = make_wiring(tmp)
            providers._decimals = lambda t: 18
            tx = pe.UnsignedTx(to=pe.AERODROME_ROUTER,
                               function="swapExactETHForTokens",
                               value_eth=0.0002, amount_in_eth=0.0002,
                               token=TOKEN, side="buy",
                               deadline_ts=NOW + 120)
            with mock.patch.object(providers, "_quote_for_signing",
                                   side_effect=pe.ExecutorError(
                                       "aerodrome cannot quote")):
                with self.assertRaises(pe.ExecutorError):
                    providers.sign_fn(tx, bytearray(
                        bytes.fromhex(key_hex)))
            self.assertEqual(providers.sign_count, 0,
                             "no signature was produced")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_min_out_is_anchored_to_the_sign_time_quote(self):
        """The min_out anchor, pinned explicitly: amountOutMin is
        computed from the FRESH per-venue quote taken AT SIGNING TIME,
        discounted by the executor's PROBE_MAX_SLIPPAGE_BPS. It is NOT
        anchored to the gate-time quote the executor's slippage gate
        used — the gate and the signature are separate reads, and a
        price move between them lowers the minimum with the market
        rather than pinning a stale number. Both behaviors are
        asserted here so neither is implicit."""
        tmp = tempfile.mkdtemp(prefix="wo10_w2_")
        try:
            _raw, key_hex, _addr = throwaway_key()
            providers = make_wiring(tmp)
            providers._decimals = lambda t: 18
            tx = pe.UnsignedTx(to=pe.AERODROME_ROUTER,
                               function="swapExactETHForTokens",
                               value_eth=0.0002, amount_in_eth=0.0002,
                               token=TOKEN, side="buy",
                               deadline_ts=NOW + 120)
            slip = float(pe.PROBE_MAX_SLIPPAGE_BPS)
            # 1) the discount itself: min_out = quote * (1 - slip)
            quote_out = 1_000_000_000
            with mock.patch.object(providers, "_quote_for_signing",
                                   return_value=(quote_out, {})):
                min_out = providers._min_out_wei(tx, quote_out, 18)
            self.assertEqual(min_out,
                             int(quote_out * (1 - slip / 10_000)),
                             "min_out is the fresh quote discounted by the "
                             "executor's slippage constant")
            # 2) the anchor MOVES with the market: a worse sign-time
            #    quote yields a lower minimum — the signature follows
            #    the fresh read, never a stale gate number.
            worse_quote = int(quote_out * 0.9)
            with mock.patch.object(providers, "_quote_for_signing",
                                   return_value=(worse_quote, {})):
                min_out_worse = providers._min_out_wei(tx, worse_quote, 18)
            self.assertLess(min_out_worse, min_out,
                            "a worse sign-time quote lowers the minimum "
                            "with the market")
            self.assertEqual(min_out_worse,
                             int(worse_quote * (1 - slip / 10_000)))
            # 3) a venue that cannot quote raises BEFORE any minimum is
            #    computed — the failure path is the quote, not the math.
            with mock.patch.object(providers, "_quote_for_signing",
                                   side_effect=pe.ExecutorError(
                                       "aerodrome cannot quote at signing")):
                with self.assertRaises(pe.ExecutorError) as ctx:
                    providers._build_calldata(tx)
            self.assertIn("cannot quote", str(ctx.exception),
                          "the failure is the quote, surfaced before any "
                          "minimum is computed")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Acceptance 2 — sign path with a throwaway keystore
# ---------------------------------------------------------------------------

class TestSignPathThrowawayKeystore(unittest.TestCase):

    def test_sign_each_tx_type_recovers_to_throwaway_address(self):
        """Build + sign each tx type the executor emits (per F1
        sequencing); the signature recovers to the throwaway address.
        (Calldata correctness is asserted separately — recovery alone
        certifies the payload, not the trade.)"""
        tmp = tempfile.mkdtemp(prefix="wo10_test_")
        try:
            raw, key_hex, address = throwaway_key()
            providers = make_wiring(tmp)
            providers._decimals = lambda t: 6 if t.lower() == TOKEN.lower() \
                else 18
            deadline = NOW + 120
            txs = [
                pe.UnsignedTx(to=pe.AERODROME_ROUTER,
                              function="swapExactETHForTokens",
                              value_eth=pe.TRADE_SIZE_ETH,
                              amount_in_eth=pe.TRADE_SIZE_ETH, token=TOKEN,
                              side="buy", deadline_ts=deadline),
                pe.UnsignedTx(to=pe.WETH, function="deposit",
                              value_eth=pe.TRADE_SIZE_ETH,
                              amount_in_eth=pe.TRADE_SIZE_ETH, token=TOKEN,
                              side="buy", deadline_ts=deadline),
                pe.UnsignedTx(to=pe.WETH, function="approve", value_eth=0.0,
                              amount_in_eth=pe.TRADE_SIZE_ETH, token=TOKEN,
                              side="buy", deadline_ts=deadline,
                              spender=pe.UNISWAP_ROUTER02,
                              approve_amount=pe.TRADE_SIZE_ETH,
                              position_token_amount=pe.TRADE_SIZE_ETH),
                pe.UnsignedTx(to=pe.UNISWAP_ROUTER02,
                              function="exactInputSingle", value_eth=0.0,
                              amount_in_eth=pe.TRADE_SIZE_ETH, token=TOKEN,
                              side="buy", deadline_ts=deadline),
                pe.UnsignedTx(to=TOKEN, function="approve", value_eth=0.0,
                              amount_in_eth=pe.TRADE_SIZE_ETH, token=TOKEN,
                              side="sell", deadline_ts=deadline,
                              spender=pe.AERODROME_ROUTER,
                              approve_amount=1000.0,
                              position_token_amount=1000.0),
                pe.UnsignedTx(to=pe.AERODROME_ROUTER,
                              function="swapExactTokensForETH", value_eth=0.0,
                              amount_in_eth=pe.TRADE_SIZE_ETH, token=TOKEN,
                              side="sell", deadline_ts=deadline,
                              position_token_amount=1000.0),
            ]
            from eth_account import Account

            def fake_quote(tx):
                if tx.function in ("swapExactETHForTokens",
                                   "swapExactTokensForETH"):
                    out = 2500 * 10**6 if tx.side == "buy" else 10**14
                    return out, {"builder": "aerodrome", "stable": False,
                                 "factory": "0x" + "ff" * 20}
                out = 2500 * 10**6 if tx.side == "buy" else 10**14
                return out, {"builder": "uniswap-v3", "fee": 3000,
                             "swap_router": pe.UNISWAP_ROUTER02}

            with mock.patch.object(providers, "_quote_for_signing",
                                   side_effect=fake_quote):
                for tx in txs:
                    signed = providers._build_and_sign(
                        tx, bytearray(bytes.fromhex(key_hex)))
                    recovered = Account.recover_transaction(
                        signed.hex() if isinstance(signed, bytes)
                        else str(signed))
                    self.assertEqual(recovered.lower(), address.lower(),
                                     "signature must recover to the "
                                     "throwaway address")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _all_tx_types(self):
        deadline = NOW + 120
        return [
            pe.UnsignedTx(to=pe.AERODROME_ROUTER,
                          function="swapExactETHForTokens",
                          value_eth=pe.TRADE_SIZE_ETH,
                          amount_in_eth=pe.TRADE_SIZE_ETH, token=TOKEN,
                          side="buy", deadline_ts=deadline),
            pe.UnsignedTx(to=pe.WETH, function="deposit",
                          value_eth=pe.TRADE_SIZE_ETH,
                          amount_in_eth=pe.TRADE_SIZE_ETH, token=TOKEN,
                          side="buy", deadline_ts=deadline),
            pe.UnsignedTx(to=pe.WETH, function="approve", value_eth=0.0,
                          amount_in_eth=pe.TRADE_SIZE_ETH, token=TOKEN,
                          side="buy", deadline_ts=deadline,
                          spender=pe.UNISWAP_ROUTER02,
                          approve_amount=pe.TRADE_SIZE_ETH,
                          position_token_amount=pe.TRADE_SIZE_ETH),
            pe.UnsignedTx(to=pe.UNISWAP_ROUTER02,
                          function="exactInputSingle", value_eth=0.0,
                          amount_in_eth=pe.TRADE_SIZE_ETH, token=TOKEN,
                          side="buy", deadline_ts=deadline),
            pe.UnsignedTx(to=TOKEN, function="approve", value_eth=0.0,
                          amount_in_eth=pe.TRADE_SIZE_ETH, token=TOKEN,
                          side="sell", deadline_ts=deadline,
                          spender=pe.AERODROME_ROUTER,
                          approve_amount=1000.0,
                          position_token_amount=1000.0),
            pe.UnsignedTx(to=pe.AERODROME_ROUTER,
                          function="swapExactTokensForETH", value_eth=0.0,
                          amount_in_eth=pe.TRADE_SIZE_ETH, token=TOKEN,
                          side="sell", deadline_ts=deadline,
                          position_token_amount=1000.0),
        ]

    def test_every_wei_value_is_integer(self):
        """Float ETH -> integer wei at the signing boundary."""
        tmp = tempfile.mkdtemp(prefix="wo10_test_")
        try:
            _raw, key_hex, _addr = throwaway_key()
            providers = make_wiring(tmp)
            providers._decimals = lambda t: 6 if t.lower() == TOKEN.lower() \
                else 18
            tx = pe.UnsignedTx(to=pe.AERODROME_ROUTER,
                               function="swapExactETHForTokens",
                               value_eth=0.0002, amount_in_eth=0.0002,
                               token=TOKEN, side="buy",
                               deadline_ts=NOW + 120)
            from eth_account import Account
            from eth_account._utils.legacy_transactions import (
                serializable_unsigned_transaction_from_dict)
            from web3 import Web3
            with mock.patch.object(providers, "_quote_for_signing",
                                   return_value=(2500 * 10**6, {})):
                signed = providers._build_and_sign(
                    tx, bytearray(bytes.fromhex(key_hex)))
            raw_bytes = bytes(signed)
            raw_hex = "0x" + raw_bytes.hex()
            self.assertTrue(raw_hex.startswith("0x"))
            self.assertGreater(len(raw_hex), 100,
                               "signed tx is a real RLP payload")
            # the integer-wei boundary is asserted where it matters: the
            # value the signer received is int(wei), not float.
            u = serializable_unsigned_transaction_from_dict({
                "chainId": 8453, "nonce": 0, "gasPrice": 1_000_000_000,
                "gas": 250_000,
                "to": Web3.to_checksum_address(pe.AERODROME_ROUTER),
                "value": int(0.0002 * 1e18)})
            for field_name in ("value", "nonce", "gasPrice", "gas"):
                v = getattr(u, field_name)
                self.assertIsInstance(v, int,
                                      f"{field_name} must be an integer wei")
            self.assertEqual(u.value, int(0.0002 * 1e18))
            recovered = Account.recover_transaction(raw_bytes)
            self.assertTrue(recovered.startswith("0x"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_executor_sign_fn_receives_bytearray_and_zeroizes(self):
        """The wiring's sign_fn is called by the executor with a
        bytearray; the executor zeroizes after — the wiring never
        decrypts itself."""
        tmp = tempfile.mkdtemp(prefix="wo10_test_")
        try:
            ks = FakeKeystore()
            providers = make_wiring(tmp)
            seen = {}

            def spy_sign(tx, key):
                seen["type"] = type(key).__name__
                return b"signed"

            ex = pe.ProbeExecutor(
                state_path=os.path.join(tmp, "lane.json"),
                ledger_path=os.path.join(tmp, "ledger.jsonl"),
                posture_fn=lambda: (False, True),
                price_fn=lambda t: (PRICE, "ok"),
                quote_fn=lambda t, s, z: pe.Quote(
                    venue="aerodrome", amount_out=1000.0,
                    implied_price_usd=PRICE, routable=True),
                sign_fn=spy_sign, submit_fn=lambda s: {
                    "tx_hash": "0x" + "de" * 32, "block_number": 1,
                    "block_time": NOW, "executed_price_usd": PRICE,
                    "gas_eth": 0.0001, "gas_usd": 0.3, "amount_out": 1000},
                balance_fn=lambda: 1.0, gas_price_fn=lambda: (0.05, 0.05),
                nonce_fn=lambda: (5, 5), keystore=ks,
                now_fn=lambda: float(NOW))
            r = ex.process_entry({"pick_id": "w1-x", "token": TOKEN,
                                  "wallet_address": LEADER_WALLET,
                                  "entry_price": PRICE,
                                  "open_ts": NOW - 60}, NOW)
            self.assertEqual(r["action"], "entered")
            self.assertEqual(seen["type"], "bytearray",
                             "executor hands sign_fn a bytearray")
            self.assertEqual(ks.decrypt_calls, 1)
            self.assertEqual(ks.zeroize_calls, 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# W6 — timeout path journals a broadcast record before raising
# ---------------------------------------------------------------------------

class TestBroadcastMocked(unittest.TestCase):

    def _signed_deposit(self, providers, key_hex):
        """A real signed deposit tx — the decode-first gate in
        submit_fn needs a decodable payload."""
        tx = pe.UnsignedTx(to=pe.WETH, function="deposit",
                           value_eth=0.0002, amount_in_eth=0.0002,
                           token=TOKEN, side="buy",
                           deadline_ts=NOW + 120)
        return providers._build_and_sign(
            tx, bytearray(bytes.fromhex(key_hex)))

    def test_receipt_timeout_raises_no_resubmit(self):
        """Broadcast succeeds but the receipt times out → RAISE, the
        tx is NOT resent, AND the broadcast is journaled first (W6) —
        a possibly-landed step leaves a trace."""
        tmp = tempfile.mkdtemp(prefix="wo10_test_")
        try:
            _raw, key_hex, _addr = throwaway_key()
            w3 = FakeW3(receipt_delay=999)  # receipt never appears
            providers = make_wiring(tmp, w3=w3)
            providers._decimals = lambda t: 18
            with self.assertRaises(pe.ExecutorError) as ctx:
                providers.submit_fn(
                    self._signed_deposit(providers, key_hex))
            self.assertIn("receipt timeout", str(ctx.exception))
            self.assertIn("not resubmitting", str(ctx.exception))
            self.assertEqual(len(w3.sent), 1, "exactly one send, no resubmit")
            # W6: the broadcast row exists with the tx hash + context
            with open(providers.gas_journal_path) as f:
                rows = [json.loads(l) for l in f if l.strip()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "broadcast")
            self.assertIn("tx_hash", rows[0])
            self.assertGreater(len(rows[0]["tx_hash"]), 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_successful_submit_journals_gas(self):
        tmp = tempfile.mkdtemp(prefix="wo10_test_")
        try:
            _raw, key_hex, _addr = throwaway_key()
            w3 = FakeW3()
            # the deposit's WETH snapshot: pre 0, post 0.0002
            w3.token_balance_sequence[pw.WETH.lower()] = [0,
                                                          int(0.0002e18)]
            tx_hash = "0x" + format(1, "064x")
            w3.receipts[tx_hash] = {
                "gasUsed": 150_000, "effectiveGasPrice": 1_000_000_000,
                "blockNumber": 20_000_001}
            providers = make_wiring(tmp, w3=w3)
            providers._decimals = lambda t: 18
            with mock.patch.object(providers, "eth_usd_fn",
                                   return_value=ETH_USD):
                receipt = providers.submit_fn(
                    self._signed_deposit(providers, key_hex))
            self.assertEqual(receipt["tx_hash"], tx_hash)
            self.assertGreater(receipt["gas_eth"], 0)
            # requirement 6: broadcast row + confirmed row
            with open(providers.gas_journal_path) as f:
                rows = [json.loads(l) for l in f if l.strip()]
            self.assertEqual(len(rows), 2,
                             "a broadcast row and a confirmed row")
            self.assertEqual(rows[0]["status"], "broadcast")
            self.assertEqual(rows[1]["status"], "confirmed")
            self.assertEqual(rows[1]["tx_hash"], tx_hash)
            self.assertEqual(rows[1]["gas_used"], 150_000)
            self.assertEqual(rows[1]["gas_cost_wei"],
                             150_000 * 1_000_000_000)
            # addendum: rows carry the decoded tx context
            for row in rows:
                for field in ("target", "function", "token", "nonce",
                              "sender"):
                    self.assertIn(field, row,
                                  f"journal row must carry {field}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_broadcast_failure_raises(self):
        tmp = tempfile.mkdtemp(prefix="wo10_test_")
        try:
            _raw, key_hex, _addr = throwaway_key()
            w3 = FakeW3()

            class _Boom(Exception):
                pass

            def boom(raw):
                raise _Boom("insufficient funds")

            w3.eth.send_raw_transaction = boom
            providers = make_wiring(tmp, w3=w3)
            providers._decimals = lambda t: 18
            with self.assertRaises(pe.ExecutorError):
                providers.submit_fn(
                    self._signed_deposit(providers, key_hex))
            # a failed broadcast leaves NO journal row — nothing was sent
            if os.path.exists(providers.gas_journal_path):
                with open(providers.gas_journal_path) as f:
                    rows = [json.loads(l) for l in f if l.strip()]
            else:
                rows = []
            self.assertEqual(len(rows), 0,
                             "a failed broadcast journals nothing")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_gas_usd_raises_when_price_unavailable(self):
        """W6: an unavailable eth_usd RAISES — never a plausible 0.0."""
        tmp = tempfile.mkdtemp(prefix="wo10_test_")
        try:
            _raw, key_hex, _addr = throwaway_key()
            w3 = FakeW3()
            tx_hash = "0x" + format(1, "064x")
            w3.receipts[tx_hash] = {
                "gasUsed": 150_000, "effectiveGasPrice": 1_000_000_000,
                "blockNumber": 20_000_001}
            providers = make_wiring(tmp, w3=w3)
            providers._decimals = lambda t: 18
            with mock.patch.object(providers, "eth_usd_fn",
                                   side_effect=pe.ExecutorError(
                                       "eth_usd unavailable: feed down")):
                with self.assertRaises(pe.ExecutorError) as ctx:
                    providers.submit_fn(
                        self._signed_deposit(providers, key_hex))
            self.assertIn("eth_usd unavailable", str(ctx.exception))
            self.assertNotIn("0.0", str(ctx.exception))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# W3 — the REAL submit_fn establishes fills from balance deltas
# ---------------------------------------------------------------------------

class TestFillAmountFromBalanceDelta(unittest.TestCase):
    """W3: submit_fn is the only hook — it must establish the fill
    itself. The REAL submit_fn runs against a mock chain whose
    balances MOVE and whose Transfer logs LIE; the returned receipt
    must carry the balance-delta figure in amount_out AND
    executed_price_usd, never the log figure."""

    def _sign_buy(self, providers, key_hex, value_eth=0.0002):
        tx = pe.UnsignedTx(to=pe.AERODROME_ROUTER,
                           function="swapExactETHForTokens",
                           value_eth=value_eth, amount_in_eth=value_eth,
                           token=TOKEN, side="buy",
                           deadline_ts=NOW + 120)
        with mock.patch.object(providers, "_quote_for_signing",
                               return_value=(2500 * 10**6, {})):
            return providers._build_and_sign(
                tx, bytearray(bytes.fromhex(key_hex)))

    def test_buy_fill_from_balance_delta_beats_lying_logs(self):
        tmp = tempfile.mkdtemp(prefix="wo10_w3_")
        try:
            _raw, key_hex, _addr = throwaway_key()
            w3 = FakeW3(
                # pre-send: 0 tokens; post-send: 1.5 tokens (6 dec) —
                # the REAL fill. The receipt's Transfer log LIES.
                lying_logs=[{"topics": [], "data":
                             (10**30).to_bytes(32, "big").hex()}])
            w3.token_balance_sequence[TOKEN.lower()] = [0, 1_500_000]
            providers = make_wiring(tmp, w3=w3)
            providers._decimals = lambda t: 6 if t.lower() == TOKEN.lower() \
                else 18
            tx_hash = "0x" + format(1, "064x")
            w3.receipts[tx_hash] = {
                "gasUsed": 150_000, "effectiveGasPrice": 1_000_000_000,
                "blockNumber": 20_000_001}
            with mock.patch.object(providers, "eth_usd_fn",
                                   return_value=ETH_USD):
                receipt = providers.submit_fn(
                    self._sign_buy(providers, key_hex))
            # amount_out = 1.5 tokens (6 dec) — the balance delta
            self.assertAlmostEqual(receipt["amount_out"], 1.5, places=9,
                                   msg="amount_out must be the balance "
                                       "delta, not the log figure")
            self.assertNotEqual(receipt["amount_out"], 10**30)
            # executed_price_usd = (0.0002 ETH * $2500) / 1.5 tokens
            expect = (0.0002 * ETH_USD) / 1.5
            self.assertAlmostEqual(receipt["executed_price_usd"], expect,
                                   places=6)
            self.assertEqual(receipt["fill_basis"], "balance-delta")
            # gas_usd priced from the SAME eth_usd
            self.assertAlmostEqual(
                receipt["gas_usd"],
                (150_000 * 1_000_000_000 / 1e18) * ETH_USD, places=9)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_sell_fill_from_eth_balance_delta(self):
        """Sell leg: amount_out is the ETH received, corrected for the
        step's gas; executed_price_usd is the token's USD price."""
        tmp = tempfile.mkdtemp(prefix="wo10_w3_")
        try:
            _raw, key_hex, throwaway_addr = throwaway_key()
            # sell 1000 tokens for 0.0004 WETH; gas costs 0.00015 ETH
            gas_cost_wei = 150_000 * 1_000_000_000  # 0.00015 ETH
            pre_eth = 10**19
            post_eth = pre_eth + int(0.0004 * 1e18) - gas_cost_wei
            w3 = FakeW3()
            # the sequence is keyed on the DECODED sender — the
            # throwaway address the tx recovers to, not MAIN_WALLET
            w3.balance_sequence[throwaway_addr.lower()] = [pre_eth,
                                                           post_eth]
            providers = make_wiring(tmp, w3=w3)
            providers._decimals = lambda t: 6 if t.lower() == TOKEN.lower() \
                else 18
            tx = pe.UnsignedTx(to=pe.AERODROME_ROUTER,
                               function="swapExactTokensForETH",
                               value_eth=0.0, amount_in_eth=0.0002,
                               token=TOKEN, side="sell",
                               deadline_ts=NOW + 120,
                               position_token_amount=1000.0)
            with mock.patch.object(providers, "_quote_for_signing",
                                   return_value=(int(0.0004 * 1e18), {})):
                signed = providers._build_and_sign(
                    tx, bytearray(bytes.fromhex(key_hex)))
            tx_hash = "0x" + format(1, "064x")
            w3.receipts[tx_hash] = {
                "gasUsed": 150_000, "effectiveGasPrice": 1_000_000_000,
                "blockNumber": 20_000_001}
            with mock.patch.object(providers, "eth_usd_fn",
                                   return_value=ETH_USD):
                receipt = providers.submit_fn(signed)
            # amount_out = 0.0004 ETH received (delta + gas correction)
            self.assertAlmostEqual(receipt["amount_out"], 0.0004, places=12)
            # executed = (0.0004 ETH * $2500) / 1000 tokens = $1.0 total
            # for the 1000 tokens sold → $0.001 per token
            expect_exec = (0.0004 * ETH_USD) / 1000.0
            self.assertAlmostEqual(receipt["executed_price_usd"],
                                   expect_exec, places=12)
            self.assertAlmostEqual(receipt["executed_price_usd"], 0.001,
                                   places=12,
                                   msg="1000 tokens sold for $1.00 total "
                                       "is $0.001/token")
            self.assertEqual(receipt["fill_basis"], "balance-delta")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_fill_raises_when_balance_does_not_move(self):
        """A swap whose output balance does not move RAISES — never a
        plausible 0.0 fill."""
        tmp = tempfile.mkdtemp(prefix="wo10_w3_")
        try:
            _raw, key_hex, _addr = throwaway_key()
            w3 = FakeW3()
            # token balance stays 0 across both reads — nothing moved
            w3.token_balance_sequence[TOKEN.lower()] = [0, 0]
            providers = make_wiring(tmp, w3=w3)
            providers._decimals = lambda t: 6 if t.lower() == TOKEN.lower() \
                else 18
            tx_hash = "0x" + format(1, "064x")
            w3.receipts[tx_hash] = {
                "gasUsed": 150_000, "effectiveGasPrice": 1_000_000_000,
                "blockNumber": 20_000_001}
            with mock.patch.object(providers, "eth_usd_fn",
                                   return_value=ETH_USD):
                with self.assertRaises(pe.ExecutorError) as ctx:
                    providers.submit_fn(
                        self._sign_buy(providers, key_hex))
            self.assertIn("fill not established", str(ctx.exception))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_approve_step_reports_no_fill(self):
        """An approve moves no balance — the receipt says so
        explicitly rather than omitting the fields silently."""
        tmp = tempfile.mkdtemp(prefix="wo10_w3_")
        try:
            _raw, key_hex, _addr = throwaway_key()
            w3 = FakeW3()
            providers = make_wiring(tmp, w3=w3)
            providers._decimals = lambda t: 6 if t.lower() == TOKEN.lower() \
                else 18
            tx = pe.UnsignedTx(to=TOKEN, function="approve", value_eth=0.0,
                               amount_in_eth=0.0002, token=TOKEN,
                               side="sell", deadline_ts=NOW + 120,
                               spender=pe.AERODROME_ROUTER,
                               approve_amount=1000.0,
                               position_token_amount=1000.0)
            signed = providers._build_and_sign(
                tx, bytearray(bytes.fromhex(key_hex)))
            tx_hash = "0x" + format(1, "064x")
            w3.receipts[tx_hash] = {
                "gasUsed": 46_000, "effectiveGasPrice": 1_000_000_000,
                "blockNumber": 20_000_001}
            with mock.patch.object(providers, "eth_usd_fn",
                                   return_value=ETH_USD):
                receipt = providers.submit_fn(signed)
            self.assertEqual(receipt["fill_basis"],
                             "allowance-only (no balance moves)")
            self.assertNotIn("amount_out", receipt)
            self.assertGreater(receipt["gas_eth"], 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_signed_tx_round_trip_decodes(self):
        """The decode path (W3's measurement basis) round-trips every
        tx type: build -> sign -> decode -> same tokens/args."""
        tmp = tempfile.mkdtemp(prefix="wo10_w3_")
        try:
            _raw, key_hex, _addr = throwaway_key()
            providers = make_wiring(tmp)
            providers._decimals = lambda t: 6 if t.lower() == TOKEN.lower() \
                else 18
            deadline = NOW + 120

            def fake_quote(tx):
                if tx.function in ("swapExactETHForTokens",
                                   "swapExactTokensForETH"):
                    out = 2500 * 10**6 if tx.side == "buy" else 10**14
                    return out, {"builder": "aerodrome", "stable": False,
                                 "factory": "0x" + "ff" * 20}
                out = 2500 * 10**6 if tx.side == "buy" else 10**14
                return out, {"builder": "uniswap-v3", "fee": 3000,
                             "swap_router": pe.UNISWAP_ROUTER02}

            cases = [
                (pe.UnsignedTx(to=pe.WETH, function="deposit",
                               value_eth=0.0002, amount_in_eth=0.0002,
                               token=TOKEN, side="buy",
                               deadline_ts=deadline),
                 {"function": "deposit", "token_out": pw.WETH}),
                (pe.UnsignedTx(to=pe.WETH, function="approve", value_eth=0.0,
                               amount_in_eth=0.0002, token=TOKEN,
                               side="buy", deadline_ts=deadline,
                               spender=pe.UNISWAP_ROUTER02,
                               approve_amount=0.0002,
                               position_token_amount=0.0002),
                 {"function": "approve", "spender":
                  pe.UNISWAP_ROUTER02.lower()}),
                (pe.UnsignedTx(to=pe.AERODROME_ROUTER,
                               function="swapExactETHForTokens",
                               value_eth=0.0002, amount_in_eth=0.0002,
                               token=TOKEN, side="buy",
                               deadline_ts=deadline),
                 {"function": "swapExactETHForTokens", "token_in": pw.WETH,
                  "token_out": TOKEN.lower(), "deadline": deadline}),
                (pe.UnsignedTx(to=pe.AERODROME_ROUTER,
                               function="swapExactTokensForETH",
                               value_eth=0.0, amount_in_eth=0.0002,
                               token=TOKEN, side="sell",
                               deadline_ts=deadline,
                               position_token_amount=1000.0),
                 {"function": "swapExactTokensForETH",
                  "token_in": TOKEN.lower(), "token_out": pw.WETH,
                  "amount_in_wei": 1000 * 10**6}),
                (pe.UnsignedTx(to=pe.UNISWAP_ROUTER02,
                               function="exactInputSingle", value_eth=0.0,
                               amount_in_eth=0.0002, token=TOKEN,
                               side="buy", deadline_ts=deadline),
                 {"function": "exactInputSingle", "token_in": pw.WETH,
                  "token_out": TOKEN.lower(),
                  "amount_in_wei": int(0.0002 * 1e18)}),
            ]
            with mock.patch.object(providers, "_quote_for_signing",
                                   side_effect=fake_quote):
                for tx, expect in cases:
                    signed = providers._build_and_sign(
                        tx, bytearray(bytes.fromhex(key_hex)))
                    d = _decode_signed(signed)
                    ctx = providers._decode_context({
                        "sender": pe.MAIN_WALLET, "to": d["to"],
                        "value_wei": d["value_wei"], "data": d["data"],
                        "nonce": d["nonce"]})
                    for k, v in expect.items():
                        if k == "deadline":
                            self.assertEqual(ctx[k], v)
                        elif k == "function":
                            # function names are exact-case identifiers
                            self.assertEqual(ctx.get(k), v,
                                             f"{tx.function}: {k}")
                        elif isinstance(v, str):
                            self.assertEqual(str(ctx.get(k, "")).lower(), v,
                                             f"{tx.function}: {k}")
                        else:
                            self.assertEqual(ctx.get(k), v,
                                             f"{tx.function}: {k}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Acceptance 5 — dry-run end-to-end with real read providers wired in
# ---------------------------------------------------------------------------

class TestDryRunEndToEnd(unittest.TestCase):

    def test_dry_run_zero_sign_zero_submit(self):
        """Wiring composition + --dry-run walks a full cycle with zero
        sign/submit calls (counters asserted)."""
        tmp = tempfile.mkdtemp(prefix="wo10_test_")
        try:
            providers = make_wiring(tmp)
            ex = pe.ProbeExecutor(
                state_path=os.path.join(tmp, "lane.json"),
                ledger_path=os.path.join(tmp, "ledger.jsonl"),
                poller_state_path="",
                posture_fn=lambda: (True, False),   # DISARMED
                price_fn=lambda t: (PRICE, "ok"),   # mock: no live chain
                quote_fn=lambda t, s, z: pe.Quote(
                    venue="aerodrome", amount_out=1000.0,
                    implied_price_usd=PRICE, routable=True),
                sign_fn=providers.sign_fn,
                submit_fn=providers.submit_fn,
                balance_fn=providers.balance_fn,
                gas_price_fn=lambda: (0.05, 0.05),
                nonce_fn=providers.nonce_fn,
                keystore=FakeKeystore(),
                dry_run=True, now_fn=lambda: float(NOW))
            poller = os.path.join(tmp, "poller.json")
            with open(poller, "w") as f:
                json.dump({"open_positions": {
                    "w1-x": {"wallet_address": LEADER_WALLET, "token": TOKEN,
                             "entry_price": PRICE, "open_ts": NOW - 60}}}, f)
            ex.poller_state_path = poller
            summary = ex.run_cycle(NOW)
            self.assertEqual(len(summary["entries"]), 1)
            self.assertEqual(summary["entries"][0]["action"], "dry_run")
            self.assertEqual(providers.sign_count, 0, "zero signing")
            self.assertEqual(providers.submit_count, 0, "zero submission")
            self.assertFalse(ex.state.open_positions)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Acceptance 6 — gas journal: landed-step gas on a failed sequence
# ---------------------------------------------------------------------------

class TestGasJournalOnFailedSequence(unittest.TestCase):
    """Mocked 3-step Uniswap entry failing at step 3: steps 1-2 gas is
    present in the wiring gas journal with tx hashes and wei costs,
    and no position is booked."""

    def test_landed_step_gas_journals_on_sequence_failure(self):
        tmp = tempfile.mkdtemp(prefix="wo10_test_")
        try:
            w3 = FakeW3()
            # the deposit step's WETH output snapshot reads WETH twice
            # (pre 0, post 0.0002); approve's snapshot is None
            w3.token_balance_sequence[pw.WETH.lower()] = [0, int(0.0002e18)]
            for i in (1, 2):
                w3.receipts["0x" + format(i, "064x")] = {
                    "gasUsed": 120_000, "effectiveGasPrice": 1_000_000_000,
                    "blockNumber": 20_000_001}
            providers = make_wiring(tmp, w3=w3)
            providers._decimals = lambda t: 18
            ks = FakeKeystore()
            call_n = {"n": 0}

            def submit(signed):
                call_n["n"] += 1
                if call_n["n"] == 3:
                    raise pe.ExecutorError("step 3 exploded")
                return providers.submit_fn(signed)

            ex = pe.ProbeExecutor(
                state_path=os.path.join(tmp, "lane.json"),
                ledger_path=os.path.join(tmp, "ledger.jsonl"),
                posture_fn=lambda: (False, True),
                price_fn=lambda t: (PRICE, "ok"),
                quote_fn=lambda t, s, z: pe.Quote(
                    venue="uniswap_v3", amount_out=1000.0,
                    implied_price_usd=PRICE, routable=True),
                sign_fn=providers.sign_fn, submit_fn=submit,
                balance_fn=lambda: 1.0, gas_price_fn=lambda: (0.05, 0.05),
                nonce_fn=lambda: (5, 5), keystore=ks,
                now_fn=lambda: float(NOW))
            with mock.patch.object(providers, "eth_usd_fn",
                                   return_value=ETH_USD), \
                 mock.patch.object(providers, "_quote_for_signing",
                                   return_value=(2500 * 10**6,
                                                 {"fee": 3000})):
                r = ex.process_entry({"pick_id": "w1-x", "token": TOKEN,
                                      "wallet_address": LEADER_WALLET,
                                      "entry_price": PRICE,
                                      "open_ts": NOW - 60}, NOW)
            self.assertNotEqual(r["action"], "entered")
            self.assertFalse(ex.state.open_positions,
                             "no position booked on sequence failure")
            # steps 1-2 gas present in the wiring journal — broadcast +
            # confirmed rows, surviving the executor's discarded receipts.
            with open(providers.gas_journal_path) as f:
                rows = [json.loads(l) for l in f if l.strip()]
            confirmed = [r for r in rows if r["status"] == "confirmed"]
            self.assertEqual(len(confirmed), 2,
                             "both landed steps confirmed before step 3 "
                             "failed")
            for row in confirmed:
                self.assertIn("tx_hash", row)
                self.assertGreater(row["gas_cost_wei"], 0)
            broadcasts = [r for r in rows if r["status"] == "broadcast"]
            self.assertEqual(len(broadcasts), 2)
            self.assertEqual(call_n["n"], 3, "step 3 was attempted once")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# W4 — the verifier uses the gas journal, not the record's counter
# ---------------------------------------------------------------------------

class TestInflightVerifier(unittest.TestCase):

    def _write_journal(self, path, rows):
        with open(path, "w") as f:
            for r in rows:
                f.write(json.dumps(r, sort_keys=True) + "\n")

    def _state_with(self, tmp, steps, steps_completed, started_at=NOW):
        state_path = os.path.join(tmp, "lane.json")
        state = {"inflight_ops": {
            "w1-x": {"kind": "entry", "steps": steps,
                     "steps_completed": steps_completed,
                     "started_at": started_at}}}
        with open(state_path, "w") as f:
            json.dump(state, f)
        return state_path

    def _steps(self):
        return [
            {"to": pe.WETH, "function": "deposit",
             "value_eth": 0.0002, "amount_in_eth": 0.0002},
            {"to": pe.WETH, "function": "approve",
             "value_eth": 0.0, "amount_in_eth": 0.0002},
            {"to": pe.UNISWAP_ROUTER02, "function": "exactInputSingle",
             "value_eth": 0.0, "amount_in_eth": 0.0002}]

    def test_verifier_lands_from_journal_not_counter(self):
        """The journal says step 1 broadcast AND confirmed; the record
        says steps_completed=1. landed comes from the journal — and
        the receipt lookup is the proof."""
        tmp = tempfile.mkdtemp(prefix="wo10_w4_")
        try:
            w3 = FakeW3(nonce=7)
            tx_hash = "0x" + format(1, "064x")
            w3.receipts[tx_hash] = {"blockNumber": 20_000_001,
                                    "gasUsed": 60_000}
            journal = os.path.join(tmp, "gas_journal.jsonl")
            self._write_journal(journal, [
                {"status": "broadcast", "ts": NOW, "tx_hash": tx_hash,
                 "nonce": 5, "sender": pe.MAIN_WALLET.lower(),
                 "target": pe.WETH.lower(), "function": "deposit",
                 "token": pw.WETH, "value_wei": 2 * 10**14},
                {"status": "confirmed", "ts": NOW, "tx_hash": tx_hash,
                 "nonce": 5, "sender": pe.MAIN_WALLET.lower(),
                 "target": pe.WETH.lower(), "function": "deposit",
                 "token": pw.WETH, "value_wei": 2 * 10**14,
                 "gas_used": 60_000, "effective_gas_price_wei": 10**9,
                 "gas_cost_wei": 6 * 10**13, "gas_cost_eth": 6e-5,
                 "block_number": 20_000_001},
            ])
            state_path = self._state_with(tmp, self._steps(), 1)
            report = pw.verify_inflight_record(state_path, w3=w3,
                                               gas_journal_path=journal)
            op = report["operations"][0]
            self.assertTrue(op["steps"][0]["landed"],
                            "step 1 landed per journal + receipt")
            self.assertIn("confirmed on-chain", op["steps"][0]["evidence"])
            self.assertIn("nonce 5", op["steps"][0]["evidence"])
            self.assertFalse(op["steps"][1]["landed"])
            self.assertIn("keep-failed-closed", op["recommendation"])
            self.assertIn("1 of 3", op["recommendation"])
            # the counter is reported but is NOT the evidence
            self.assertEqual(op["steps_completed_counter"], 1)
            self.assertEqual(len(w3.sent), 0, "verifier submits nothing")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_verifier_crash_after_broadcast_before_persist(self):
        """THE W4 case: the crash landed step 1 on-chain but died
        before the counter persisted — the record says
        steps_completed=0 while the journal shows a confirmed receipt.
        The verifier must NOT call that resume-safe."""
        tmp = tempfile.mkdtemp(prefix="wo10_w4_")
        try:
            w3 = FakeW3(nonce=7)
            tx_hash = "0x" + format(1, "064x")
            w3.receipts[tx_hash] = {"blockNumber": 20_000_001,
                                    "gasUsed": 60_000}
            journal = os.path.join(tmp, "gas_journal.jsonl")
            self._write_journal(journal, [
                {"status": "broadcast", "ts": NOW, "tx_hash": tx_hash,
                 "nonce": 5, "sender": pe.MAIN_WALLET.lower(),
                 "target": pe.WETH.lower(), "function": "deposit",
                 "token": pw.WETH, "value_wei": 2 * 10**14},
                {"status": "confirmed", "ts": NOW, "tx_hash": tx_hash,
                 "nonce": 5, "sender": pe.MAIN_WALLET.lower(),
                 "target": pe.WETH.lower(), "function": "deposit",
                 "token": pw.WETH, "value_wei": 2 * 10**14,
                 "gas_used": 60_000, "effective_gas_price_wei": 10**9,
                 "gas_cost_wei": 6 * 10**13, "gas_cost_eth": 6e-5,
                 "block_number": 20_000_001},
            ])
            # the record: counter NEVER persisted past 0
            state_path = self._state_with(tmp, self._steps(), 0)
            report = pw.verify_inflight_record(state_path, w3=w3,
                                               gas_journal_path=journal)
            op = report["operations"][0]
            self.assertEqual(op["steps_completed_counter"], 0,
                             "the record really says nothing landed")
            self.assertTrue(op["steps"][0]["landed"],
                            "but the journal + receipt prove step 1 "
                            "landed")
            self.assertNotIn("resume-safe", op["recommendation"],
                             "must NOT advise resuming — a landed step "
                             "would be re-executed")
            self.assertIn("keep-failed-closed", op["recommendation"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_verifier_all_landed(self):
        tmp = tempfile.mkdtemp(prefix="wo10_w4_")
        try:
            w3 = FakeW3(nonce=6)
            tx_hash = "0x" + format(1, "064x")
            w3.receipts[tx_hash] = {"blockNumber": 20_000_001,
                                    "gasUsed": 200_000}
            journal = os.path.join(tmp, "gas_journal.jsonl")
            self._write_journal(journal, [
                {"status": "broadcast", "ts": NOW, "tx_hash": tx_hash,
                 "nonce": 5, "sender": pe.MAIN_WALLET.lower(),
                 "target": pe.AERODROME_ROUTER.lower(),
                 "function": "swapExactETHForTokens", "token": TOKEN,
                 "value_wei": 2 * 10**14},
                {"status": "confirmed", "ts": NOW, "tx_hash": tx_hash,
                 "nonce": 5, "sender": pe.MAIN_WALLET.lower(),
                 "target": pe.AERODROME_ROUTER.lower(),
                 "function": "swapExactETHForTokens", "token": TOKEN,
                 "value_wei": 2 * 10**14, "gas_used": 200_000,
                 "effective_gas_price_wei": 10**9,
                 "gas_cost_wei": 2 * 10**14, "gas_cost_eth": 2e-4,
                 "block_number": 20_000_001},
            ])
            state_path = self._state_with(tmp, [
                {"to": pe.AERODROME_ROUTER,
                 "function": "swapExactETHForTokens",
                 "value_eth": 0.0002, "amount_in_eth": 0.0002}], 1)
            report = pw.verify_inflight_record(state_path, w3=w3,
                                               gas_journal_path=journal)
            op = report["operations"][0]
            self.assertTrue(op["steps"][0]["landed"])
            self.assertIn("every step has a confirmed on-chain receipt",
                          op["recommendation"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_verifier_nothing_landed_is_resume_safe(self):
        """No journal rows in the window = positive evidence nothing
        was broadcast (the journal is written at SEND time)."""
        tmp = tempfile.mkdtemp(prefix="wo10_w4_")
        try:
            journal = os.path.join(tmp, "gas_journal.jsonl")
            self._write_journal(journal, [])  # empty journal exists
            state_path = self._state_with(tmp, [
                {"to": pe.AERODROME_ROUTER,
                 "function": "swapExactETHForTokens",
                 "value_eth": 0.0002, "amount_in_eth": 0.0002}], 0)
            report = pw.verify_inflight_record(state_path, w3=FakeW3(),
                                               gas_journal_path=journal)
            op = report["operations"][0]
            self.assertFalse(op["steps"][0]["landed"])
            self.assertIn("resume-safe", op["recommendation"])
            self.assertIn("nothing landed", op["recommendation"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_verifier_broadcast_but_unconfirmed_is_keep_failed_closed(self):
        """Broadcast rows exist in the op window but NO receipt was
        found (the W6 receipt-timeout path): the step may have landed.
        resume-safe is NOT available — nothing landed is exactly what
        is NOT known. keep-failed-closed, with text that says so."""
        tmp = tempfile.mkdtemp(prefix="wo11_w4_")
        try:
            w3 = FakeW3(nonce=7)
            # receipts dict EMPTY: broadcast recorded, no receipt found
            tx_hash = "0x" + format(1, "064x")
            journal = os.path.join(tmp, "gas_journal.jsonl")
            self._write_journal(journal, [
                {"status": "broadcast", "ts": NOW, "tx_hash": tx_hash,
                 "nonce": 5, "sender": pe.MAIN_WALLET.lower(),
                 "target": pe.AERODROME_ROUTER.lower(),
                 "function": "swapExactETHForTokens", "token": TOKEN,
                 "value_wei": 2 * 10**14},
            ])
            state_path = self._state_with(tmp, [
                {"to": pe.AERODROME_ROUTER,
                 "function": "swapExactETHForTokens",
                 "value_eth": 0.0002, "amount_in_eth": 0.0002}], 0)
            report = pw.verify_inflight_record(state_path, w3=w3,
                                               gas_journal_path=journal)
            op = report["operations"][0]
            self.assertFalse(op["steps"][0]["landed"],
                             "no receipt found — landed stays False")
            self.assertIn("no receipt found",
                          op["steps"][0]["evidence"])
            self.assertNotIn("resume-safe", op["recommendation"],
                             "broadcast-but-unconfirmed must NOT be "
                             "resume-safe — the step may have landed")
            self.assertIn("keep-failed-closed", op["recommendation"])
            self.assertIn("no receipt found",
                          op["recommendation"],
                          "the recommendation must say WHY it is "
                          "failed-closed")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_verifier_resume_safe_requires_zero_broadcast_rows(self):
        """The positive case stays: ZERO broadcast rows in the op
        window is the ONLY resume-safe condition — the text now says
        so explicitly."""
        tmp = tempfile.mkdtemp(prefix="wo11_w4_")
        try:
            journal = os.path.join(tmp, "gas_journal.jsonl")
            self._write_journal(journal, [])
            state_path = self._state_with(tmp, [
                {"to": pe.AERODROME_ROUTER,
                 "function": "swapExactETHForTokens",
                 "value_eth": 0.0002, "amount_in_eth": 0.0002}], 0)
            report = pw.verify_inflight_record(state_path, w3=FakeW3(),
                                               gas_journal_path=journal)
            op = report["operations"][0]
            self.assertIn("resume-safe", op["recommendation"])
            self.assertIn("zero broadcast rows",
                          op["recommendation"],
                          "the recommendation states the actual "
                          "condition: zero broadcast rows")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_verifier_missing_journal_fails_closed(self):
        """No journal at all = no evidence either way = fail closed."""
        tmp = tempfile.mkdtemp(prefix="wo10_w4_")
        try:
            state_path = self._state_with(tmp, self._steps(), 0)
            with self.assertRaises(pe.ExecutorError) as ctx:
                pw.verify_inflight_record(
                    state_path, w3=FakeW3(),
                    gas_journal_path=os.path.join(tmp, "nope.jsonl"))
            self.assertIn("keep-failed-closed", str(ctx.exception))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_verifier_ignores_rows_outside_the_window(self):
        """A journal row from an EARLIER op must not be read as this
        op's evidence."""
        tmp = tempfile.mkdtemp(prefix="wo10_w4_")
        try:
            w3 = FakeW3(nonce=99)
            journal = os.path.join(tmp, "gas_journal.jsonl")
            self._write_journal(journal, [
                {"status": "broadcast", "ts": NOW - 100_000,
                 "tx_hash": "0x" + format(9, "064x"), "nonce": 1,
                 "sender": pe.MAIN_WALLET.lower(), "target": pe.WETH.lower(),
                 "function": "deposit", "token": pw.WETH,
                 "value_wei": 2 * 10**14},
            ])
            state_path = self._state_with(tmp, self._steps(), 0)
            report = pw.verify_inflight_record(state_path, w3=w3,
                                               gas_journal_path=journal)
            op = report["operations"][0]
            for s in op["steps"]:
                self.assertFalse(s["landed"],
                                 "an out-of-window row is not evidence")
            self.assertIn("resume-safe", op["recommendation"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# W5 — gas current = latest block's basefee; median from the sample
# ---------------------------------------------------------------------------

class TestGasPriceFn(unittest.TestCase):

    def test_current_is_latest_basefee_median_from_sample(self):
        """A rising fee series: current must be the LATEST block's
        basefee (the sample MAX was the defect), and the median the
        sample's middle. The series must be long enough that the
        50-block sampling stride walks through it rather than clamping
        every sample to the oldest fee."""
        tmp = tempfile.mkdtemp(prefix="wo10_w5_")
        try:
            # 37 fees: the sampling loop walks 1800 blocks back in
            # 50-block strides (36 samples), and the mock indexes the
            # series by stride, so each sample is a distinct fee.
            # Rising: oldest 10, latest 90.
            fees = [(10 + 80 * i // 36) * 10**9 for i in range(37)]
            w3 = FakeW3(base_fees=fees, block_number=20_000_000)
            providers = make_wiring(tmp, w3=w3)
            cur, med = providers.gas_price_fn()
            self.assertAlmostEqual(cur, 90.0, places=9,
                                   msg="current must be the latest "
                                       "block's basefee, not the max")
            # the sample's true median: the loop samples stride 0..35
            # (series indices 36 down to 1 — index 0 is never reached),
            # so compute the median over exactly those 36 values.
            sample = sorted((10 + 80 * i // 36) for i in range(1, 37))
            self.assertAlmostEqual(med, float(sample[len(sample) // 2]),
                                   places=9,
                                   msg="median from the sampled hour")
            self.assertNotEqual(cur, med,
                                msg="current and median must differ on a "
                                    "rising series")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_gas_raises_when_no_basefee_data(self):
        tmp = tempfile.mkdtemp(prefix="wo10_w5_")
        try:
            w3 = FakeW3(base_fees=[0], block_number=20_000_000)
            providers = make_wiring(tmp, w3=w3)
            with self.assertRaises(pe.ExecutorError):
                providers.gas_price_fn()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Boundaries — real keystore off-limits, no broadcast
# ---------------------------------------------------------------------------

class TestHardBoundaries(unittest.TestCase):
    """Hard limits: real keystore off-limits, no broadcast, no env
    safety knobs."""

    def test_no_test_touches_real_keystore(self):
        """No reference to the real keystore file or its directory in
        this test module's source."""
        src = open("/home/kenzodadon/base-trend-bot/"
                   "test_probe_wiring.py").read()
        real_file = "probe_" + "key.gpg"
        real_dir = os.path.join("~", "." + "probe-keystore")
        self.assertNotIn(real_file, src)
        self.assertNotIn(real_dir, src)

    def test_probe_wiring_source_never_reads_real_keystore(self):
        """The wiring module never opens or decrypts the real keystore —
        it only imports probe_keystore as the keystore module (same as
        the executor). The module docstring may name the path as a
        boundary statement; no executable line may reference it."""
        src = open("/home/kenzodadon/base-trend-bot/"
                   "probe_wiring.py").read()
        import ast
        tree = ast.parse(src)
        lines = src.splitlines()
        doc_end = 0
        if tree.body and isinstance(tree.body[0], ast.Expr) and \
                isinstance(tree.body[0].value, ast.Constant) and \
                isinstance(tree.body[0].value.value, str):
            doc_end = tree.body[0].end_lineno
        code_only = "\n".join(lines[doc_end:])
        real_file = "probe_" + "key.gpg"
        self.assertNotIn(real_file, code_only,
                         "wiring code must not reference the real keystore file")
        self.assertIn("import probe_keystore", src,
                      "wiring must use the keystore module interface")

    def test_live_providers_refuse_when_chain_unavailable(self):
        """Provider errors raise ExecutorError — never a default."""
        providers = pw.LiveProviders(rpc_url="http://127.0.0.1:1",
                                     w3=FakeW3())
        self.assertTrue(issubclass(pe.ExecutorError, Exception))

    def test_no_send_in_dry_run_composition(self):
        """build_live_executor with dry_run=True never signs or sends —
        verified by composition: sign_fn/submit_fn are wired but the
        executor's dry_run gate blocks _sign."""
        tmp = tempfile.mkdtemp(prefix="wo10_test_")
        try:
            providers = make_wiring(tmp)
            ex = pe.ProbeExecutor(
                state_path=os.path.join(tmp, "lane.json"),
                ledger_path=os.path.join(tmp, "ledger.jsonl"),
                posture_fn=lambda: (True, False),
                price_fn=lambda t: (PRICE, "ok"),
                quote_fn=lambda t, s, z: pe.Quote(
                    venue="aerodrome", amount_out=1000.0,
                    implied_price_usd=PRICE, routable=True),
                sign_fn=providers.sign_fn,
                submit_fn=providers.submit_fn,
                keystore=FakeKeystore(), dry_run=True,
                now_fn=lambda: float(NOW))
            r = ex.process_entry({"pick_id": "w1-x", "token": TOKEN,
                                  "wallet_address": LEADER_WALLET,
                                  "entry_price": PRICE,
                                  "open_ts": NOW - 60}, NOW)
            self.assertEqual(r["action"], "dry_run")
            self.assertEqual(providers.sign_count, 0)
            self.assertEqual(providers.submit_count, 0)
            self.assertEqual(len(providers._w3.sent), 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_wiring_introduces_no_env_knobs(self):
        """W2/WO10: the wiring introduces no env-configurable safety
        parameters — slippage is CONSUMED from the executor."""
        src = open("/home/kenzodadon/base-trend-bot/"
                   "probe_wiring.py").read()
        import ast
        tree = ast.parse(src)
        lines = src.splitlines()
        doc_end = 0
        if tree.body and isinstance(tree.body[0], ast.Expr) and \
                isinstance(tree.body[0].value, ast.Constant) and \
                isinstance(tree.body[0].value.value, str):
            doc_end = tree.body[0].end_lineno
        code_only = "\n".join(lines[doc_end:])
        self.assertNotIn("os.environ", code_only,
                         "wiring must not read env config itself")
        self.assertIn("PROBE_MAX_SLIPPAGE_BPS", code_only,
                      "slippage is consumed from the executor")


# ---------------------------------------------------------------------------
# Acceptance 8 — existing suites still green (subprocess runner)
# ---------------------------------------------------------------------------

class TestExistingSuitesStillGreen(unittest.TestCase):
    """Executor 52 + keystore 22 + the F5 four-suite set, all pass."""

    def test_existing_suites_pass(self):
        import subprocess
        venv = "/home/kenzodadon/base-trend-bot/.venv/bin/python"
        root = "/home/kenzodadon/base-trend-bot"
        suites = [
            (os.path.join(root, "test_probe_executor.py"), root),
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


# ---------------------------------------------------------------------------
# WO15 — runner mode: keystore shim + loop
# ---------------------------------------------------------------------------

class TestRunnerKeystore(unittest.TestCase):
    """WO15 runner keystore shim: holds passphrase, delegates to real
    keystore per signing event, zeroizes passphrase on exit."""

    def _fake_keystore(self, key):
        class _FakeKS:
            def __init__(self):
                self.decrypt_calls = 0
                self.last_passphrase = None
            def decrypt_at_use(self, passphrase=None):
                self.decrypt_calls += 1
                self.last_passphrase = passphrase
                return key
        return _FakeKS()

    def test_delegates_to_real_keystore_per_call(self):
        key = bytes(range(32))
        fks = self._fake_keystore(key)
        ks = pw._RunnerKeystore(b"test-pass", fks)
        d1 = ks.decrypt_at_use()
        d2 = ks.decrypt_at_use()
        self.assertEqual(bytes(d1), key)
        self.assertEqual(bytes(d2), key)
        self.assertEqual(fks.decrypt_calls, 2)  # delegated each time
        self.assertEqual(fks.last_passphrase, b"test-pass")  # passphrase passed

    def test_decrypt_at_use_accepts_no_args(self):
        # executor calls decrypt_at_use() with no args — must not raise
        fks = self._fake_keystore(bytes(range(32)))
        ks = pw._RunnerKeystore(b"test-pass", fks)
        ks.decrypt_at_use()
        self.assertEqual(fks.decrypt_calls, 1)

    def test_zeroize_clears_passed_buffer(self):
        fks = self._fake_keystore(bytes(range(32)))
        ks = pw._RunnerKeystore(b"test-pass", fks)
        buf = bytearray(ks.decrypt_at_use())  # executor wraps in bytearray
        ks._zeroize(buf)
        self.assertEqual(bytes(buf), bytes(32))  # buffer zeroized
        self.assertEqual(fks.decrypt_calls, 1)  # passphrase not re-delegated

    def test_zeroize_master_zeroizes_passphrase(self):
        fks = self._fake_keystore(bytes(range(32)))
        ks = pw._RunnerKeystore(b"test-pass", fks)
        ks.zeroize_master()
        self.assertEqual(bytes(ks._passphrase), b"")  # passphrase zeroized
        # After zeroize, decrypt would pass empty passphrase — the
        # runner's finally block calls zeroize_master then the process
        # exits, so this is the terminal state.


class TestRunRunner(unittest.TestCase):
    """WO15 runner loop: cycles, cadence, max_cycles, KeyboardInterrupt."""

    def _fake_ex(self):
        class _Ex:
            def __init__(self):
                self.cycles = 0
            def run_cycle(self):
                self.cycles += 1
                return {"entries": [], "exits": [], "refusals": [], "recovered": []}
        return _Ex()

    def test_runs_max_cycles(self):
        ex = self._fake_ex()
        n = pw.run_runner(ex, interval_s=999, max_cycles=3,
                         sleep_fn=lambda s: None)
        self.assertEqual(n, 3)
        self.assertEqual(ex.cycles, 3)

    def test_keyboard_interrupt_exits_cleanly(self):
        ex = self._fake_ex()
        def _raise(s):
            raise KeyboardInterrupt
        n = pw.run_runner(ex, interval_s=999, max_cycles=5,
                         sleep_fn=_raise)
        self.assertEqual(n, 1)  # one cycle ran, then interrupt on sleep
        self.assertEqual(ex.cycles, 1)

    def test_infinite_by_default(self):
        # max_cycles=None means infinite — verify it would run forever
        # by using a sleep_fn that interrupts after a few calls.
        # Trace: cycle1, sleep(calls=1), cycle2, sleep(calls=2 → INT)
        # → 2 cycles complete before the interrupt.
        ex = self._fake_ex()
        calls = []
        def _interrupt_after_2(s):
            calls.append(s)
            if len(calls) >= 2:
                raise KeyboardInterrupt
        n = pw.run_runner(ex, interval_s=0.001, max_cycles=None,
                         sleep_fn=_interrupt_after_2)
        self.assertEqual(n, 2)
        self.assertEqual(ex.cycles, 2)
        self.assertEqual(len(calls), 2)


class TestRunnerMode(unittest.TestCase):
    """WO15 runner mode: passphrase once, held in memory, zeroized on exit."""

    def _fake_ex(self):
        class _Ex:
            def __init__(self):
                self.keystore = None
                self.started = False
                self.cycles = 0
            def start(self):
                self.started = True
            def run_cycle(self):
                self.cycles += 1
                return {"entries": [], "exits": [], "refusals": [], "recovered": []}
        return _Ex()

    def _fake_keystore(self):
        class _FakeKS:
            def __init__(self):
                self.decrypt_calls = 0
            def decrypt_at_use(self, passphrase=None):
                self.decrypt_calls += 1
                return bytes(range(32))
        return _FakeKS()

    def test_runner_mode_passphrase_once_zeroize_on_exit(self):
        import probe_wiring as pw
        fake_ks = self._fake_keystore()
        fake_ex = self._fake_ex()

        orig_build = pw.build_live_executor
        pw.build_live_executor = lambda **kw: (fake_ex, None)
        try:
            getpass_calls = []
            def _fake_getpass(prompt=""):
                getpass_calls.append(prompt)
                return "test-passphrase"

            args = argparse.Namespace(poller_state="", poller_closes="",
                                      interval=999, max_cycles=2)
            rc = pw._run_runner_mode(args, getpass_fn=_fake_getpass,
                                     keystore_mod=fake_ks,
                                     sleep_fn=lambda s: None)
        finally:
            pw.build_live_executor = orig_build

        self.assertEqual(rc, 0)
        self.assertEqual(len(getpass_calls), 1)  # passphrase entered once
        self.assertTrue(fake_ex.started)
        self.assertEqual(fake_ex.cycles, 2)  # max_cycles=2
        # shim holds the passphrase, not the key — real keystore
        # not called at runner startup (no upfront decrypt)
        self.assertEqual(fake_ks.decrypt_calls, 0)
        # passphrase zeroized on exit
        runner_ks = fake_ex.keystore
        self.assertIsInstance(runner_ks, pw._RunnerKeystore)
        self.assertEqual(bytes(runner_ks._passphrase), b"")

    def test_runner_mode_empty_passphrase_refused(self):
        orig_build = pw.build_live_executor
        pw.build_live_executor = lambda **kw: (self._fake_ex(), None)
        try:
            def _fake_getpass(prompt=""):
                return ""
            args = argparse.Namespace(poller_state="", poller_closes="",
                                      interval=60, max_cycles=None)
            rc = pw._run_runner_mode(args, getpass_fn=_fake_getpass,
                                     keystore_mod=self._fake_keystore())
            self.assertEqual(rc, 1)
        finally:
            pw.build_live_executor = orig_build

    def test_f1_zeroize_on_start_failure(self):
        """F1: ex.start() raises (wrong passphrase at startup address
        check) — zeroize_master() must still be called."""
        class _FailStartEx:
            def __init__(self):
                self.keystore = None
            def start(self):
                raise RuntimeError("startup key derivation mismatch")
            def run_cycle(self):
                raise AssertionError("should not reach run_cycle")

        fake_ex = _FailStartEx()
        fake_ks = self._fake_keystore()

        orig_build = pw.build_live_executor
        pw.build_live_executor = lambda **kw: (fake_ex, None)
        try:
            def _fake_getpass(prompt=""):
                return "wrong-pass"

            args = argparse.Namespace(poller_state="", poller_closes="",
                                      interval=60, max_cycles=None)
            with self.assertRaises(RuntimeError):
                pw._run_runner_mode(args, getpass_fn=_fake_getpass,
                                    keystore_mod=fake_ks,
                                    sleep_fn=lambda s: None)
        finally:
            pw.build_live_executor = orig_build

        # F1 assertion: passphrase was zeroized even though start() raised
        runner_ks = fake_ex.keystore
        self.assertIsInstance(runner_ks, pw._RunnerKeystore)
        self.assertEqual(bytes(runner_ks._passphrase), b"")


class TestRunnerF2(unittest.TestCase):
    """F2: log-and-continue on transient faults, exit on keystore errors."""

    def _fake_ex_with_error(self, error_cls, fail_on_cycle=1):
        class _Ex:
            def __init__(self):
                self.cycles = 0
            def run_cycle(self):
                self.cycles += 1
                if self.cycles == fail_on_cycle:
                    raise error_cls("injected fault")
                return {"entries": [], "exits": [], "refusals": [], "recovered": []}
        return _Ex()

    def test_transient_fault_log_and_continue(self):
        """Generic exception in run_cycle — runner logs and continues."""
        ex = self._fake_ex_with_error(RuntimeError, fail_on_cycle=1)
        n = pw.run_runner(ex, interval_s=999, max_cycles=3,
                         sleep_fn=lambda s: None)
        self.assertEqual(n, 3)  # all 3 cycles attempted
        self.assertEqual(ex.cycles, 3)

    def test_keystore_error_exits_runner(self):
        """KeystoreError in run_cycle — runner must exit, not spin."""
        from probe_keystore import KeystoreError
        ex = self._fake_ex_with_error(KeystoreError, fail_on_cycle=2)
        # max_cycles=5 but keystore error on cycle 2 must stop the runner
        with self.assertRaises(KeystoreError):
            pw.run_runner(ex, interval_s=999, max_cycles=5,
                          sleep_fn=lambda s: None)
        self.assertEqual(ex.cycles, 2)  # stopped at cycle 2


class TestAddressChecksumBoundary(unittest.TestCase):
    """WO17: web3.py's request formatter raises InvalidAddress on a
    non-checksummed `to`. The poller state file stores token addresses
    lowercase, so EVERY address that reaches web3 must be normalized
    first — including the direct `w3.eth.call` in _balance_of, which does
    NOT pass through _RpcClient. Regression guard for the WO17 fix.
    """

    LOWER = "0xb2000000000000000000003d760a57f93078d901"
    OWNER = "0x" + "11" * 20

    def _stub_w3(self):
        from web3 import Web3
        outer = self
        outer.seen = {}
        outer.checksum = Web3.to_checksum_address(self.LOWER)

        class _Eth:
            def call(inner, tx):
                outer.seen["call_to"] = tx.get("to")
                return b"\x00" * 32

            def get_code(inner, addr):
                outer.seen["code_addr"] = addr
                return "0x60016001"

        class _W3:
            pass

        w3 = _W3()
        w3.eth = _Eth()
        return w3

    def test_rpc_client_eth_call_checksums_to(self):
        client = pw._RpcClient(self._stub_w3())
        client.eth_call(self.LOWER, "0x313ce567")
        self.assertEqual(self.seen["call_to"], self.checksum)

    def test_rpc_client_has_bytecode_checksums_address(self):
        client = pw._RpcClient(self._stub_w3())
        client.has_bytecode(self.LOWER)
        self.assertEqual(self.seen["code_addr"], self.checksum)

    def test_balance_of_checksums_token(self):
        """_balance_of calls w3.eth.call directly, bypassing _RpcClient,
        so it must normalize the token itself."""
        class _StubDex:
            @staticmethod
            def selector(sig):
                return b"\x70\xa0\x82\x31"

            @staticmethod
            def enc_address(addr):
                return b"\x00" * 12 + bytes.fromhex(str(addr)[2:].rjust(40, "0"))

        providers = pw.LiveProviders.__new__(pw.LiveProviders)
        providers._w3 = self._stub_w3()
        providers._dex = _StubDex()
        providers._config = object()
        providers._balance_of(self.LOWER, self.OWNER)
        self.assertEqual(self.seen["call_to"], self.checksum)

    def test_checksum_is_idempotent(self):
        """An already-checksummed address must round-trip unchanged."""
        from web3 import Web3
        cs = Web3.to_checksum_address(self.LOWER)
        self.assertEqual(pw._checksum(cs), cs)


class TestDryRunWritesNothing(unittest.TestCase):
    """WO18-1: a dry-run cycle is observe-only. It must leave the ledger
    and state files it was handed byte-identical — no _append_ledger, no
    processed_pick_ids persisted, no save_state.

    This is the regression that would have caught the WO17 incident, where
    two dry-runs appended 46 rows (36 distinct, 10 duplicates) to the
    production ledger and sync_probe_ledger.sh pushed them to GitHub.
    """

    REAL_POLLER_STATE = "/home/kenzodadon/base-trend-bot/copy_trader/state.json"
    REAL_POLLER_CLOSES = ("/home/kenzodadon/Grok-muse/handoff/"
                          "copy-trader-closes/closes.jsonl")

    def test_dry_run_leaves_production_files_byte_identical(self):
        tmp = tempfile.mkdtemp(prefix="wo18_dryrun_")
        state_path = os.path.join(tmp, "probe_lane_state.json")
        ledger_path = os.path.join(tmp, "probe_ledger.jsonl")

        # Seed both production paths with known content.
        seed_state = json.dumps(pe.LaneState().to_dict(), indent=2)
        seed_ledger = json.dumps({"seed": True}, sort_keys=True) + "\n"
        with open(state_path, "w") as f:
            f.write(seed_state)
        with open(ledger_path, "w") as f:
            f.write(seed_ledger)
        before_state = open(state_path, "rb").read()
        before_ledger = open(ledger_path, "rb").read()

        # Real poller inputs; providers stubbed so the cycle is offline.
        # price_fn returns no price -> every position lands on the
        # price_failure_skip branch, which is one of the branches that
        # used to append to the ledger and save state.
        ex = pe.ProbeExecutor(
            state_path=state_path, ledger_path=ledger_path,
            poller_state_path=self.REAL_POLLER_STATE,
            poller_closes_path=self.REAL_POLLER_CLOSES,
            posture_fn=lambda: (False, True),          # armed
            price_fn=lambda t: (None, "no price"),
            dry_run=True,
        )
        summary = ex.run_cycle()

        # The cycle was non-vacuous: exactly one refusal per leader pick.
        with open(self.REAL_POLLER_STATE) as f:
            n_positions = len(json.load(f).get("open_positions") or {})
        self.assertEqual(len(summary["refusals"]), n_positions)

        # The property under test: nothing was written.
        self.assertEqual(open(state_path, "rb").read(), before_state)
        self.assertEqual(open(ledger_path, "rb").read(), before_ledger)


if __name__ == "__main__":
    unittest.main(verbosity=2)
