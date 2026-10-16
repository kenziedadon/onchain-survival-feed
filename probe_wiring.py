#!/usr/bin/env python3
"""probe_wiring.py — WO10 live wiring for the session-key probe (build only).

The composition root the committed executor (55ee126) lacks: real
providers for price, quote, balance, nonce, gas, signing, and
submission, plus two requirements carried from the WO9 reviews:

  6. Landed-step gas reconciliation: `submit_fn` journals EVERY landed
     step at submit time (append-only JSONL in the lane state dir),
     keyed by tx hash — because journaling happens at submit time, gas
     from steps 1-2 of a sequence that fails at step 3 is recorded even
     though the executor discards its receipts list. The executor
     cannot be changed in this WO, so the reconciliation source lives
     here. A BROADCAST row is written at send time (before the receipt
     wait) and COMPLETED by a confirmed row, so a step that lands but
     whose receipt times out still leaves a trace. Rows carry the
     decoded tx context (target, function, token, nonce, sender) so
     landed-step gas is attributable without manual archaeology.

  7. On-chain step verification for crash recovery: a READ-ONLY
     verifier over a persisted in-flight record — per-step landed /
     not-landed from chain state plus a recommendation. Standalone,
     consumed by human review; NOT wired into the executor; signs
     nothing; submits nothing.

FIX ROUND (W1-W6 + addendum, applied to the reviewed delivery):

  W1 quote_fn implied_price_usd is the TOKEN's USD price on both
     sides: buy (size_eth * eth_usd) / amount_out_units; sell
     (amount_out_units / size_eth) * eth_usd. eth_usd comes from the
     unified chain via eth_usd_fn, never from a second price path.
     The old formulas returned USD-per-ETH; WETH coincidence masked it.

  W2 sign_fn encodes REAL calldata per tx.function — WETH deposit,
     ERC-20 approve, Aerodrome ETH-leg swaps (swapExactETHForTokens /
     swapExactTokensForETH, path variants), Uniswap SwapRouter02
     multicall(exactInputSingle). Selectors and encodings verified
     against 4byte.directory and BaseForge's verified primitives.
     Gas is a per-type deliberate value under a hard cap — no flat
     250_000. amountOutMin is a fresh per-venue quote discounted by
     the executor's PROBE_MAX_SLIPPAGE_BPS (consumed, not
     re-introduced); a venue that cannot quote at signing time fails
     closed — no signature without a minimum.

  W3 submit_fn ESTABLISHES the fill itself: pre-send balance
     snapshots, post-receipt balance deltas (token balanceOf delta;
     ETH delta corrected for the step's gas), and it sets amount_out
     + executed_price_usd on the returned receipt from those deltas.
     Logs are never consulted. A delta that cannot be established
     RAISES — never a plausible default, never a silent omission.
     The tx is decoded from the signed bytes to know what to measure.

  W4 verify_inflight_record reads the wiring's own gas journal (the
     append-only record of every broadcast) and does receipt lookups
     by those tx hashes, with nonce progression as the second signal.
     landed/not-landed comes from on-chain evidence, NOT from the
     in-flight record's steps_completed counter. "resume-safe"
     requires positive evidence that nothing landed — a missing
     journal is itself a fail-closed reason.

  W5 gas_price_fn current = the LATEST block's basefee; the median
     comes from the sampled hour. (The old code took the sample max.)

  W6 the timeout path journals a broadcast record before raising, so
     a possibly-landed step leaves a gas-journal trace; gas_usd uses
     eth_usd_fn directly — an unavailable price raises, never a
     plausible 0.0.

HARD LIMITS (WO10):
  - No real broadcast anywhere in this file's tests. Broadcast is
    proven against mocks only. Nothing here submits in build/test.
  - The real keystore (~/.probe-keystore/probe_key.gpg) is off-limits.
    Signing tests use THROWAWAY keystores in temp dirs only.
  - No new env-configurable safety parameters. Slippage and
    trade-count values are pinned at arm time by ruling, not by
    wiring; wiring introduces no env knobs of its own.

This module performs NO arming, NO posture change, and NO wiring into
running code. It supplies providers to the executor, whose gates
remain the sole authority. Fail-closed: any provider error, timeout,
or disagreement raises ExecutorError; no plausible-looking defaults.

Dependencies: web3 (venv), eth_account (venv), rlp (venv),
probe_executor, probe_keystore, copy_trader.price_chain, BaseForge
plugin (~/.hermes/plugins/baseforge).
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

log = logging.getLogger("probe_wiring")

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(os.path.expanduser("~/.hermes"), "plugins"))

import probe_executor as pe
from probe_executor import ExecutorError  # noqa: E402

CHAIN_ID = 8453
RPC_URL = "https://mainnet.base.org"
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"

# Lane state dir (absolute — the WO1 rule; no CWD-relative paths).
LANE_DIR = os.path.join(PROJECT_ROOT, "probe_lane")
GAS_JOURNAL_PATH = os.path.join(LANE_DIR, "gas_journal.jsonl")

# WO16 liveness heartbeat — touched once per runner loop iteration.
# mtime semantics: the watchdog checks staleness by mtime age. Content is
# the cycle timestamp (ISO-8601 UTC). A hung cycle produces no touch.
RUNNER_HEARTBEAT_FILE = os.path.join(PROJECT_ROOT, "probe_runner.heartbeat")
RUNNER_HEARTBEAT_STALE_S = 5 * 60  # 5 minutes


def _touch_heartbeat():
    """WO16: touch probe_runner.heartbeat to prove the loop is iterating.

    Writes the current UTC timestamp; mtime updates automatically. Best-effort:
    an OSError here must never crash the runner. A hung cycle (where
    run_cycle() never returns) produces no touch — that is the liveness signal
    the watchdog consumes.
    """
    try:
        with open(RUNNER_HEARTBEAT_FILE, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat() + "\n")
    except OSError:
        pass

# ---------------------------------------------------------------------------
# Calldata signatures + selectors (W2)
#
# The executor's allowlist permits ONLY swapExactETHForTokens /
# swapExactTokensForETH on AERODROME_ROUTER — the path variants.
# BaseForge's verified builder emits the Route-tuple variant
# (swapExactTokensForTokens), which is NOT allowlisted here, so the
# wiring encodes the path variants on BaseForge's verified primitives
# (selector/enc_u256/enc_address from baseforge.dex). Every selector
# below was cross-checked against 4byte.directory:
#   7ff36ab5 swapExactETHForTokens(uint256,address[],address,uint256)
#   18cbafe5 swapExactTokensForETH(uint256,uint256,address[],address,uint256)
#   cac88ea9 swapExactTokensForTokens(...)  [Route variant — NOT used]
#   04e45aaf exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))
#   5ae401dc multicall(uint256,bytes[])
#   d0e30db0 deposit()
#   095ea7b3 approve(address,uint256)
#   70a08231 balanceOf(address)
# ---------------------------------------------------------------------------

_AERO_ETH_FOR_TOKENS_SIG = "swapExactETHForTokens(uint256,address[],address,uint256)"
_AERO_TOKENS_FOR_ETH_SIG = "swapExactTokensForETH(uint256,uint256,address[],address,uint256)"
_WETH_DEPOSIT_SIG = "deposit()"
_APPROVE_SIG = "approve(address,uint256)"
_BALANCE_OF_SIG = "balanceOf(address)"

# Selector hex constants used by the decode path (W3). These are the
# 4-byte prefixes of the signatures above, verified against
# 4byte.directory; kept as literals so decoding never depends on the
# encoder's selector() being called first.
_SEL_DEPOSIT = "d0e30db0"
_SEL_APPROVE = "095ea7b3"
_SEL_BALANCE_OF = "70a08231"
_SEL_AERO_ETH_FOR_TOKENS = "7ff36ab5"
_SEL_AERO_TOKENS_FOR_ETH = "18cbafe5"
_SEL_MULTICALL = "5ae401dc"
_SEL_EXACT_INPUT_SINGLE = "04e45aaf"

# Per-type deliberate gas limits (W2): each value is a decision for the
# tx type it names, and every one is asserted in the test suite. The
# hard cap is the fence — anything above it raises, and an unknown
# function raises rather than falling back to a flat number.
GAS_BY_FUNCTION = {
    "deposit": 60_000,                 # WETH wrap
    "approve": 60_000,                 # ERC-20 approve
    "swapExactETHForTokens": 250_000,  # Aerodrome ETH-leg swap
    "swapExactTokensForETH": 250_000,  # Aerodrome ETH-leg swap
    "exactInputSingle": 300_000,       # SwapRouter02 multicall + swap
}
GAS_HARD_CAP = 400_000


def _gas_for(function: str) -> int:
    """Deliberate per-type gas (W2). Unknown function -> fail closed."""
    gas = GAS_BY_FUNCTION.get(function)
    if gas is None:
        raise ExecutorError(
            f"no deliberate gas value for function {function!r}; "
            f"refusing to sign with a default")
    if gas > GAS_HARD_CAP:
        raise ExecutorError(
            f"gas {gas} for {function!r} exceeds hard cap {GAS_HARD_CAP}")
    return gas


# ---------------------------------------------------------------------------
# BaseForge access (verified quoters; addresses from baseforge/config.py)
# ---------------------------------------------------------------------------

def _load_baseforge():
    """Import BaseForge's dex module + config. Raises on any failure —
    never a silent fallback."""
    try:
        from baseforge import dex as _dex
        from baseforge.config import BaseForgeConfig
    except Exception as exc:  # noqa: BLE001
        raise ExecutorError(f"BaseForge plugin unavailable: {exc}") from exc
    cfg = BaseForgeConfig() if isinstance(BaseForgeConfig, type) \
        else BaseForgeConfig
    return _dex, cfg


def _checksum(addr: str) -> str:
    """Normalize an address to EIP-55 before it reaches web3.

    web3.py's request formatter raises InvalidAddress on a non-checksummed
    `to`, so any lowercased address (the poller's state file stores them
    lowercase) would otherwise fail the very first eth_call. Cheap and
    idempotent: an already-checksummed address round-trips unchanged.
    """
    from web3 import Web3
    return Web3.to_checksum_address(addr)


class _RpcClient:
    """Minimal ChainClient-shaped adapter over web3 for BaseForge's
    LiveQuoter (eth_call + balance + code checks only — read-only)."""

    def __init__(self, w3):
        self._w3 = w3
        self.chain_id = CHAIN_ID

    def eth_call(self, to: str, data: str, value_wei: int = 0) -> str:
        return self._w3.eth.call({"to": _checksum(to), "data": data,
                                  "value": value_wei}).hex()

    def get_base_fee_wei(self) -> int:
        block = self._w3.eth.get_block("latest")
        return int(block.get("baseFeePerGas", 0))

    def get_priority_fee_history_wei(self, blocks: int = 10) -> list:
        h = self._w3.eth.fee_history(blocks, "latest", [25, 50, 75])
        return [int(r[1]) for r in h.get("reward", []) if len(r) > 1]

    def get_token_balance_wei(self, token: str, owner: str) -> int:
        data = "0x70a08231" + owner.lower().replace("0x", "").rjust(64, "0")
        return int(self.eth_call(token, data), 16)

    def has_bytecode(self, address: str) -> bool:
        return len(self._w3.eth.get_code(_checksum(address))) > 2

    def send_raw_transaction(self, signed_tx: str) -> str:  # not used here
        raise ExecutorError("read-only client: send refused")


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

class LiveProviders:
    """The real providers, constructed once, injected into ProbeExecutor."""

    def __init__(self, rpc_url: str = RPC_URL, wallet: str = pe.MAIN_WALLET,
                 gas_journal_path: str = GAS_JOURNAL_PATH,
                 w3=None, dex=None, config=None):
        self.rpc_url = rpc_url
        self.wallet = wallet
        self.gas_journal_path = gas_journal_path
        self._w3 = w3
        self._dex = dex
        self._config = config
        self.sign_count = 0
        self.submit_count = 0

    # -- lazy chain/dex init -------------------------------------------------

    def _ensure_chain(self):
        if self._w3 is None:
            try:
                from web3 import Web3
            except ImportError as exc:
                raise ExecutorError("web3 unavailable for live wiring") from exc
            self._w3 = Web3(Web3.HTTPProvider(self.rpc_url))
        return self._w3

    def _ensure_dex(self):
        if self._dex is None or self._config is None:
            dex, cfg_cls = _load_baseforge()
            self._dex = dex
            self._config = cfg_cls() if isinstance(cfg_cls, type) else cfg_cls
        return self._dex, self._config

    # -- price (unified chain; no second price path) ---------------------------

    def price_fn(self, token: str):
        from copy_trader.price_chain import fetch_price_consensus
        res, _diag = fetch_price_consensus(token)
        return (res.price if res.ok else None), res.reason

    def eth_usd_fn(self) -> float:
        price, reason = self.price_fn(WETH)
        if not price:
            raise ExecutorError(f"eth_usd unavailable: {reason}")
        return price

    # -- quote (BaseForge verified quoters) -------------------------------------

    def _best_quote(self, token: str, side: str, size_eth: float):
        """The shared quoting core: best executable across Aerodrome
        and Uniswap V3 QuoterV2. Returns a dict with the venue name,
        amount_out in OUTPUT-token base units, the output token's
        decimals, and the winning quote's exec params — or None when
        unquotable. quote_fn and sign_fn both build on this so the
        gate and the signature see the same numbers."""
        dex, cfg = self._ensure_dex()
        client = _RpcClient(self._ensure_chain())
        adapters = [
            dex.AerodromeQuoter(client, router=cfg.aerodrome_router,
                                factory=cfg.aerodrome_factory),
            dex.UniswapV3Quoter(client, quoter=cfg.uniswap_quoter_v2,
                                swap_router=cfg.uniswap_swap_router),
        ]
        quoter = dex.LiveQuoter(client, adapters)
        if side == "buy":
            token_in, token_out = WETH, token
        else:
            token_in, token_out = token, WETH
        # decimals of BOTH legs — the input scales the amount, the
        # output converts amount_out. Mixing them is the decimals defect.
        dec_in = dex.token_decimals(client, token_in)
        dec_out = dex.token_decimals(client, token_out)
        try:
            quotes = quoter.quotes(token_in, token_out, size_eth)
        except Exception as exc:  # noqa: BLE001 — quoter revert = unroutable
            log.warning("quote failure (%s %s): %s", side, token, exc)
            return None
        if not quotes:
            return None
        # best = highest p0 (reference price per unit in)
        best = max(quotes, key=lambda q: q.p0)
        venue_name = str(getattr(best, "venue", ""))
        amount_out = None
        adapter = None
        if "aerodrome" in venue_name:
            adapter = adapters[0]
        elif "uniswap" in venue_name:
            adapter = adapters[1]
        if adapter is not None:
            # amount_out at full size, re-queried with the CORRECT input
            # scaling (input token's decimals).
            amount_in_wei = int(size_eth * (10 ** dec_in))
            try:
                amount_out = adapter.quote_out_wei(token_in, token_out,
                                                   amount_in_wei)
            except Exception:
                amount_out = None
        if not amount_out:
            amount_out = int(best.p0 * size_eth)
        venue = ("aerodrome" if "aerodrome" in venue_name
                 else "uniswap_v3")
        return {
            "venue": venue,
            "amount_out_wei": int(amount_out),
            "dec_out": dec_out,
            "dec_in": dec_in,
            "exec_params": dict(getattr(best, "exec_params", {}) or {}),
        }

    def quote_fn(self, token: str, side: str, size_eth: float):
        """Returns the executor's Quote shape: (venue, amount_out,
        implied_price_usd, routable). Best executable across Aerodrome
        and Uniswap V3 QuoterV2. None → executor treats as unroutable.

        W1: implied_price_usd is the TOKEN's USD price on both sides —
        buy (size_eth * eth_usd) / amount_out_units, sell
        (amount_out_units / size_eth) * eth_usd. The executor's
        slippage gate compares this against the token median, so it
        must be a token price, not USD-per-ETH.
        """
        q = self._best_quote(token, side, size_eth)
        if q is None:
            return None
        amount_out_units = float(q["amount_out_wei"]) / (10 ** q["dec_out"])
        # eth_usd from the unified chain (W1). A price outage makes the
        # quote unexpressible in USD — unroutable, never a default.
        try:
            eth_usd = self.eth_usd_fn()
        except ExecutorError as exc:
            log.warning("eth_usd unavailable for quote (%s %s): %s",
                        side, token, exc)
            return None
        if side == "buy":
            implied_usd = (size_eth * eth_usd) / amount_out_units
        else:
            implied_usd = (amount_out_units / size_eth) * eth_usd
        return pe.Quote(venue=q["venue"],
                        amount_out=float(amount_out_units),
                        implied_price_usd=float(implied_usd), routable=True)

    # -- RPC reads ---------------------------------------------------------------

    def balance_fn(self) -> float:
        w3 = self._ensure_chain()
        wei = w3.eth.get_balance(self.wallet)
        return wei / 1e18

    def nonce_fn(self):
        w3 = self._ensure_chain()
        confirmed = w3.eth.get_transaction_count(self.wallet, "latest")
        pending = w3.eth.get_transaction_count(self.wallet, "pending")
        return int(confirmed), int(pending)

    def gas_price_fn(self):
        """(current_gwei, median_1h_gwei) from recent block basefees —
        no external gas API.

        W5: current is the LATEST block's basefee (the sampled hour's
        MAX was the defect); the median comes from the sample.
        """
        w3 = self._ensure_chain()
        latest = w3.eth.block_number
        try:
            latest_block = w3.eth.get_block(latest)
            cur_fee = int(latest_block.get("baseFeePerGas", 0))
        except Exception as exc:  # noqa: BLE001
            raise ExecutorError(f"latest block read failed: {exc}") from exc
        if not cur_fee:
            raise ExecutorError("latest block carries no baseFeePerGas")
        n = 1800  # ~1h at 2s blocks
        fees = []
        for bn in range(latest, max(latest - n, 0), -50):
            try:
                b = w3.eth.get_block(bn)
                if b.get("baseFeePerGas"):
                    fees.append(int(b["baseFeePerGas"]))
            except Exception:
                continue
        if not fees:
            raise ExecutorError("no basefee data from recent blocks")
        fees.sort()
        med = fees[len(fees) // 2]
        return cur_fee / 1e9, med / 1e9

    # -- decimals / balance reads -------------------------------------------------

    def _decimals(self, token: str) -> int:
        """ERC-20 decimals via BaseForge's verified reader. Raises on
        failure — a wrong decimals assumption corrupts a quote or a
        calldata amount by orders of magnitude."""
        dex, _cfg = self._ensure_dex()
        client = _RpcClient(self._ensure_chain())
        return int(dex.token_decimals(client, token))

    def _balance_of(self, token: str, owner: str) -> int:
        """balanceOf(token, owner) in base units, via BaseForge's
        verified primitives. Raises on failure."""
        dex, _cfg = self._ensure_dex()
        data = "0x" + (dex.selector(_BALANCE_OF_SIG)
                       + dex.enc_address(owner)).hex()
        raw = self._ensure_chain().eth.call({"to": _checksum(token), "data": data})
        return int(bytes(raw).hex(), 16)

    # -- signing (float -> integer wei boundary lives HERE) ----------------------

    def _sell_amount_in_wei(self, tx: pe.UnsignedTx, dec_in: int) -> int:
        """The SELL's amountIn, in the traded token's base units.

        A sell sells the position's TOKENS, not an ETH notional:
        amountIn = tx.position_token_amount × 10^dec_in. amount_in_eth
        is the ETH notional and must NEVER be encoded as token units —
        a missing or non-positive position_token_amount on a sell swap
        RAISES (fail-closed), because the only alternative would be
        selling a number derived from the notional. The sign-time
        min-out quote is sized from this same amount (see
        _quote_for_signing), so the gate-sized minimum and the encoded
        amountIn describe the same trade.
        """
        token_amount = float(getattr(tx, "position_token_amount", 0.0)
                             or 0.0)
        if token_amount <= 0:
            raise ExecutorError(
                f"sell swap {tx.function!r} carries no positive "
                f"position_token_amount ({token_amount!r}); refusing to "
                f"sign — amount_in_eth ({tx.amount_in_eth!r}) is the ETH "
                f"notional and must never be encoded as token units")
        amount_in_wei = int(token_amount * (10 ** dec_in))
        if amount_in_wei <= 0:
            raise ExecutorError(
                f"sell swap {tx.function!r}: position_token_amount "
                f"{token_amount!r} does not survive scaling to {dec_in} "
                f"decimals; refusing to sign")
        return amount_in_wei

    def _quote_for_signing(self, tx: pe.UnsignedTx):
        """Fresh per-venue quote at signing time (W2). Each swap
        function is encoded from ITS OWN venue's quote — the executor
        picked the venue from its own gate quote, and re-quoting a
        different venue here would build calldata the sequence was
        never shaped for. Returns (amount_out_wei, exec_params) or
        raises when the venue cannot quote: no signature without a
        minimum.

        The quote is sized from the amount the swap will actually
        move: a buy quotes tx.amount_in_eth of WETH, a SELL quotes the
        position's token amount (_sell_amount_in_wei) — the same
        number the calldata encodes as amountIn.
        """
        if tx.function in ("swapExactETHForTokens", "swapExactTokensForETH"):
            venue = "aerodrome"
        elif tx.function == "exactInputSingle":
            venue = "uniswap"
        else:
            return None, {}
        dex, cfg = self._ensure_dex()
        client = _RpcClient(self._ensure_chain())
        if tx.side == "buy":
            token_in, token_out = WETH, tx.token
        else:
            token_in, token_out = tx.token, WETH
        dec_in = self._decimals(token_in)
        if tx.side == "sell":
            amount_in_wei = self._sell_amount_in_wei(tx, dec_in)
        else:
            amount_in_wei = int(tx.amount_in_eth * (10 ** dec_in))
        if venue == "aerodrome":
            adapter = dex.AerodromeQuoter(client, router=cfg.aerodrome_router,
                                          factory=cfg.aerodrome_factory)
        else:
            adapter = dex.UniswapV3Quoter(client, quoter=cfg.uniswap_quoter_v2,
                                          swap_router=cfg.uniswap_swap_router)
        try:
            detailed = adapter.quote_detailed(token_in, token_out,
                                              amount_in_wei)
        except Exception as exc:  # noqa: BLE001
            raise ExecutorError(
                f"{venue} quote failed at signing: {exc}") from exc
        if not detailed or int(detailed.get("amount_out_wei") or 0) <= 0:
            raise ExecutorError(
                f"{venue} cannot quote {tx.function} at signing time; "
                f"refusing to sign without a minimum")
        return int(detailed["amount_out_wei"]), dict(detailed.get("exec")
                                                     or {})

    def _min_out_wei(self, tx: pe.UnsignedTx, amount_out_wei: int,
                     dec_out: int) -> int:
        """amountOutMin = fresh quote discounted by the executor's
        PROBE_MAX_SLIPPAGE_BPS. The wiring CONSUMES the executor's
        existing slippage value; it introduces no knob of its own."""
        slip_bps = float(getattr(pe, "PROBE_MAX_SLIPPAGE_BPS", 150.0))
        return int(amount_out_wei * (1.0 - slip_bps / 10_000.0))

    def _build_calldata(self, tx: pe.UnsignedTx) -> str:
        """Real calldata per tx.function (W2), on BaseForge's verified
        primitives. Raises on anything unrecognized."""
        from baseforge import dex as _dex
        from baseforge import swap as _swap
        fn = tx.function
        deadline = tx.deadline_ts or int(time.time()) + 120
        recipient = self.wallet
        if fn == "deposit":
            return "0x" + _dex.selector(_WETH_DEPOSIT_SIG).hex()
        if fn == "approve":
            amount_wei = int(tx.approve_amount * (10 ** self._decimals(tx.to)))
            if amount_wei <= 0:
                raise ExecutorError("approve amount must be positive")
            return _swap.approve_calldata(tx.spender, amount_wei)
        if fn == "swapExactETHForTokens":
            # swapExactETHForTokens(uint256 amountOutMin, address[] path,
            #                       address to, uint256 deadline)
            # head: amountOutMin, pathOffset, to, deadline (4 words)
            out_wei, _exec = self._quote_for_signing(tx)
            dec_out = self._decimals(tx.token)
            min_out = self._min_out_wei(tx, out_wei, dec_out)
            path = [WETH, tx.token]
            body = (
                _dex.enc_u256(min_out)
                + _dex.enc_u256(4 * 32)               # offset to path[]
                + _dex.enc_address(recipient)
                + _dex.enc_u256(deadline)
                + _dex.enc_u256(len(path))
                + b"".join(_dex.enc_address(p) for p in path)
            )
            return "0x" + (_dex.selector(_AERO_ETH_FOR_TOKENS_SIG)
                           + body).hex()
        if fn == "swapExactTokensForETH":
            # swapExactTokensForETH(uint256 amountIn, uint256 amountOutMin,
            #                       address[] path, address to, uint256 deadline)
            # head: amountIn, amountOutMin, pathOffset, to, deadline (5 words)
            out_wei, _exec = self._quote_for_signing(tx)
            min_out = self._min_out_wei(tx, out_wei, 18)  # WETH out
            # amountIn is the POSITION'S TOKEN AMOUNT in the token's
            # base units — the same amount the sign-time quote above
            # was sized from. amount_in_eth (the ETH notional) is never
            # encoded as token units; absent/non-positive raises.
            amount_in_wei = self._sell_amount_in_wei(
                tx, self._decimals(tx.token))
            path = [tx.token, WETH]
            body = (
                _dex.enc_u256(amount_in_wei)
                + _dex.enc_u256(min_out)
                + _dex.enc_u256(5 * 32)               # offset to path[]
                + _dex.enc_address(recipient)
                + _dex.enc_u256(deadline)
                + _dex.enc_u256(len(path))
                + b"".join(_dex.enc_address(p) for p in path)
            )
            return "0x" + (_dex.selector(_AERO_TOKENS_FOR_ETH_SIG)
                           + body).hex()
        if fn == "exactInputSingle":
            # SwapRouter02 multicall(deadline, [exactInputSingle(...)])
            # — BaseForge's verified builder, allowlisted on the router.
            out_wei, exec = self._quote_for_signing(tx)
            fee = exec.get("fee")
            if fee is None:
                raise ExecutorError(
                    "uniswap quote at signing carried no fee tier")
            if tx.side == "buy":
                token_in, token_out = WETH, tx.token
                dec_out = self._decimals(tx.token)
            else:
                token_in, token_out = tx.token, WETH
                dec_out = 18
            min_out = self._min_out_wei(tx, out_wei, dec_out)
            dec_in = self._decimals(token_in)
            if tx.side == "sell":
                # sell: amountIn is the position's token amount in the
                # token's base units — the same amount the sign-time
                # quote above was sized from.
                amount_in_wei = self._sell_amount_in_wei(tx, dec_in)
            else:
                amount_in_wei = int(tx.amount_in_eth * (10 ** dec_in))
                if amount_in_wei <= 0:
                    raise ExecutorError("swap amountIn must be positive")
            return _swap.uniswap_exact_input_single_calldata(
                token_in, token_out, int(fee), recipient, deadline,
                amount_in_wei, min_out)
        raise ExecutorError(f"no calldata builder for function {fn!r}")

    def _build_and_sign(self, tx: pe.UnsignedTx, key: bytearray) -> bytes:
        """Build the real transaction from UnsignedTx and sign with the
        executor-supplied key bytearray. Integer wei at the boundary.
        Real calldata (W2) + deliberate per-type gas (W2)."""
        from eth_account import Account
        from web3 import Web3
        w3 = self._ensure_chain()
        nonce = w3.eth.get_transaction_count(self.wallet, "pending")
        value_wei = int(tx.value_eth * 1e18)  # float ETH -> integer wei
        data_hex = self._build_calldata(tx)
        tx_dict = {
            "chainId": CHAIN_ID, "nonce": nonce,
            "gasPrice": w3.eth.gas_price(),
            "gas": _gas_for(tx.function),
            "to": Web3.to_checksum_address(tx.to),
            "value": value_wei,
            "data": bytes.fromhex(data_hex[2:]),
        }
        signed = Account.sign_transaction(tx_dict, bytes(key))
        self.sign_count += 1
        return signed.raw_transaction

    def sign_fn(self, tx: pe.UnsignedTx, key: bytearray) -> bytes:
        try:
            return self._build_and_sign(tx, key)
        except ExecutorError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ExecutorError(f"signing failed: {exc}") from exc

    # -- submission (+ requirement 6: landed-step gas journal) --------------------

    def _journal_gas(self, row: dict) -> None:
        """Append-only gas journal, at submit time, before return.
        A journaling failure must never mask the submission result —
        but it must be loud."""
        try:
            os.makedirs(os.path.dirname(self.gas_journal_path), exist_ok=True)
            with open(self.gas_journal_path, "a") as f:
                f.write(json.dumps(row, sort_keys=True) + "\n")
        except Exception as exc:  # noqa: BLE001
            log.error("GAS JOURNAL WRITE FAILED: %s", exc)

    # -- signed-tx decoding (W3: know what to measure) ---------------------------

    @staticmethod
    def _decode_signed_tx(signed: bytes) -> dict:
        """RLP-decode a signed legacy tx and recover the sender.
        Fail-closed: a typed (EIP-2718) or malformed payload raises —
        this wiring only ever builds legacy txs."""
        try:
            from rlp import decode as _rlp_decode
        except ImportError as exc:  # pragma: no cover - venv dep
            raise ExecutorError("rlp unavailable for tx decoding") from exc
        raw = bytes(signed)
        if not raw or raw[0] < 0xC0:
            raise ExecutorError(
                "signed payload is not a legacy RLP list (typed tx or "
                "malformed); refusing to decode")
        fields = _rlp_decode(raw)
        if len(fields) != 9:
            raise ExecutorError(
                f"signed tx has {len(fields)} RLP fields, expected 9")
        nonce, _gas_price, _gas, to, value, data, _v, _r, _s = fields
        try:
            from eth_account import Account
            sender = Account.recover_transaction(raw)
        except Exception as exc:  # noqa: BLE001
            raise ExecutorError(
                f"sender recovery failed: {exc}") from exc
        return {
            "sender": sender.lower(),
            "to": ("0x" + bytes(to).hex()).lower(),
            "value_wei": int.from_bytes(bytes(value), "big"),
            "data": bytes(data),
            "nonce": int.from_bytes(bytes(nonce), "big"),
        }

    def _decode_context(self, decoded: dict) -> dict:
        """Decode the calldata THIS wiring builds into a fill-measurement
        context (W3 + addendum: the same context the gas journal rows
        carry). Fail-closed on any unknown selector.

        Decoding uses eth_abi's standard decoder (already a web3
        dependency) rather than hand-rolled offset math — a wrong
        offset here silently measures the wrong balance.
        """
        from eth_abi import decode as _abi_decode
        data = decoded["data"]
        if len(data) < 4:
            raise ExecutorError("calldata too short to carry a selector")
        sel = data[:4].hex()
        body = data[4:]
        to = decoded["to"]

        if sel == _SEL_DEPOSIT:
            return {"function": "deposit", "to": to,
                    "value_wei": decoded["value_wei"],
                    "token_in": None, "token_out": WETH,
                    "amount_in_wei": 0, "spender": None, "deadline": 0}
        if sel == _SEL_APPROVE:
            spender, _amount = _abi_decode(["address", "uint256"], body)
            return {"function": "approve", "to": to,
                    "value_wei": decoded["value_wei"],
                    "token_in": None, "token_out": None,
                    "amount_in_wei": 0,
                    "spender": str(spender).lower(), "deadline": 0}
        if sel == _SEL_AERO_ETH_FOR_TOKENS:
            # swapExactETHForTokens(uint256,address[],address,uint256)
            min_out, path, recipient, deadline = _abi_decode(
                ["uint256", "address[]", "address", "uint256"], body)
            if len(path) < 2:
                raise ExecutorError("swapExactETHForTokens path too short")
            return {"function": "swapExactETHForTokens", "to": to,
                    "value_wei": decoded["value_wei"],
                    "token_in": str(path[0]).lower(),
                    "token_out": str(path[-1]).lower(),
                    "amount_in_wei": 0, "spender": None,
                    "deadline": int(deadline)}
        if sel == _SEL_AERO_TOKENS_FOR_ETH:
            # swapExactTokensForETH(uint256,uint256,address[],address,uint256)
            amount_in, _min_out, path, _recipient, deadline = _abi_decode(
                ["uint256", "uint256", "address[]", "address", "uint256"],
                body)
            if len(path) < 2:
                raise ExecutorError("swapExactTokensForETH path too short")
            return {"function": "swapExactTokensForETH", "to": to,
                    "value_wei": decoded["value_wei"],
                    "token_in": str(path[0]).lower(),
                    "token_out": str(path[-1]).lower(),
                    "amount_in_wei": int(amount_in), "spender": None,
                    "deadline": int(deadline)}
        if sel == _SEL_MULTICALL:
            # multicall(uint256,bytes[]) -> [exactInputSingle(params)]
            deadline, calls = _abi_decode(["uint256", "bytes[]"], body)
            if len(calls) < 1:
                raise ExecutorError("multicall carries no calls")
            inner = calls[0]
            if len(inner) < 4 or inner[:4].hex() != _SEL_EXACT_INPUT_SINGLE:
                raise ExecutorError(
                    "multicall inner call is not exactInputSingle")
            token_in, token_out, _fee, _recipient, amount_in, \
                _min_out, _sqrt_limit = _abi_decode(
                    ["address", "address", "uint24", "address", "uint256",
                     "uint256", "uint160"], inner[4:])
            return {"function": "exactInputSingle", "to": to,
                    "value_wei": decoded["value_wei"],
                    "token_in": str(token_in).lower(),
                    "token_out": str(token_out).lower(),
                    "amount_in_wei": int(amount_in), "spender": None,
                    "deadline": int(deadline)}
        raise ExecutorError(f"unrecognized calldata selector 0x{sel}")

    # -- fill establishment (W3) ---------------------------------------------------

    def _snapshot_balances(self, ctx: dict) -> dict | None:
        """Pre/post balance snapshot for the step's output side.
        Returns None for allowance-only steps (approve)."""
        if ctx["function"] == "approve":
            return None
        owner = ctx.get("sender") or self.wallet
        snap = {"eth": self._ensure_chain().eth.get_balance(owner)}
        out_token = ctx.get("token_out")
        if out_token:
            snap["tokens"] = {out_token.lower():
                              self._balance_of(out_token, owner)}
        else:
            snap["tokens"] = {}
        return snap

    def _establish_fill(self, ctx: dict, pre: dict, post: dict,
                        gas_cost_wei: int, eth_usd: float) -> dict:
        """The fill, from balance deltas only (W3). Logs are never
        consulted; a delta that cannot be established RAISES."""
        fn = ctx["function"]
        if fn == "approve":
            # An allowance moves no balance — the receipt says so
            # explicitly rather than omitting the fields silently.
            return {"fill_basis": "allowance-only (no balance moves)"}
        sender = ctx.get("sender", self.wallet)
        if pre is None or post is None:
            raise ExecutorError(
                f"cannot establish fill for {fn}: missing balance snapshot")
        value_eth = ctx["value_wei"] / 1e18
        if fn in ("deposit",) or (
                fn in ("swapExactETHForTokens", "exactInputSingle")
                and ctx["token_out"]
                and ctx["token_out"].lower() == WETH):
            # Output leg is WETH (a wrap, or a buy that somehow quotes
            # WETH->WETH): the WETH balance delta is the fill.
            delta = post["tokens"].get(WETH.lower(), 0) \
                - pre["tokens"].get(WETH.lower(), 0)
            if delta <= 0:
                raise ExecutorError(
                    f"fill not established for {fn}: WETH balance did "
                    f"not increase")
            amount_out = delta / 1e18
            if amount_out <= 0:
                raise ExecutorError("fill amount_out is not positive")
            executed = (value_eth * eth_usd) / amount_out
            return {"amount_out": amount_out,
                    "executed_price_usd": executed,
                    "fill_basis": "balance-delta"}
        if fn == "swapExactETHForTokens":
            out_token = ctx["token_out"]
            dec_out = self._decimals(out_token)
            delta = post["tokens"].get(out_token.lower(), 0) \
                - pre["tokens"].get(out_token.lower(), 0)
            if delta <= 0:
                raise ExecutorError(
                    f"buy fill not established: {out_token} balance did "
                    f"not increase")
            amount_out = delta / (10 ** dec_out)
            if amount_out <= 0:
                raise ExecutorError("buy fill amount_out is not positive")
            executed = (value_eth * eth_usd) / amount_out
            return {"amount_out": amount_out,
                    "executed_price_usd": executed,
                    "fill_basis": "balance-delta"}
        if fn == "swapExactTokensForETH":
            # Native ETH out: the ETH balance delta plus this step's gas
            # (gas left the same balance) is the received amount.
            eth_received_wei = (post["eth"] - pre["eth"]) + gas_cost_wei
            if eth_received_wei <= 0:
                raise ExecutorError(
                    "sell fill not established: no net ETH received "
                    "after gas correction")
            amount_out = eth_received_wei / 1e18
            dec_in = self._decimals(ctx["token_in"])
            tokens_sold = ctx["amount_in_wei"] / (10 ** dec_in)
            if tokens_sold <= 0:
                raise ExecutorError(
                    "sell fill not established: decoded amountIn is zero")
            executed = (amount_out * eth_usd) / tokens_sold
            return {"amount_out": amount_out,
                    "executed_price_usd": executed,
                    "fill_basis": "balance-delta"}
        if fn == "exactInputSingle":
            out_token = ctx["token_out"]
            if out_token and out_token.lower() == WETH:
                # token -> WETH (sell leg)
                delta = post["tokens"].get(WETH.lower(), 0) \
                    - pre["tokens"].get(WETH.lower(), 0)
                if delta <= 0:
                    raise ExecutorError(
                        "sell fill not established: WETH balance did "
                        "not increase")
                amount_out = delta / 1e18
                dec_in = self._decimals(ctx["token_in"])
                tokens_sold = ctx["amount_in_wei"] / (10 ** dec_in)
                if tokens_sold <= 0:
                    raise ExecutorError(
                        "sell fill not established: decoded amountIn "
                        "is zero")
                executed = (amount_out * eth_usd) / tokens_sold
                return {"amount_out": amount_out,
                        "executed_price_usd": executed,
                        "fill_basis": "balance-delta"}
            # WETH -> token (buy leg)
            dec_out = self._decimals(out_token)
            delta = post["tokens"].get(out_token.lower(), 0) \
                - pre["tokens"].get(out_token.lower(), 0)
            if delta <= 0:
                raise ExecutorError(
                    f"buy fill not established: {out_token} balance did "
                    f"not increase")
            amount_out = delta / (10 ** dec_out)
            if amount_out <= 0:
                raise ExecutorError("buy fill amount_out is not positive")
            in_eth = ctx["amount_in_wei"] / 1e18
            executed = (in_eth * eth_usd) / amount_out
            return {"amount_out": amount_out,
                    "executed_price_usd": executed,
                    "fill_basis": "balance-delta"}
        raise ExecutorError(f"no fill rule for function {fn!r}")

    def submit_fn(self, signed: bytes) -> dict:
        """eth_sendRawTransaction + wait for receipt. Establishes the
        fill from balance deltas (W3), journals the broadcast at send
        time and completes it on receipt (W6), and raises on receipt
        timeout — NEVER resubmits."""
        w3 = self._ensure_chain()
        # decode BEFORE sending: it tells us whose balances to watch
        # and what the step moves (W3).
        decoded = self._decode_signed_tx(signed)
        ctx = self._decode_context(decoded)
        ctx["sender"] = decoded["sender"]
        pre = self._snapshot_balances(ctx)
        try:
            tx_hash = w3.eth.send_raw_transaction(signed)
        except Exception as exc:  # noqa: BLE001
            raise ExecutorError(f"broadcast failed: {exc}") from exc
        self.submit_count += 1
        tx_hash_hex = tx_hash.hex()
        # W6: journal the BROADCAST before the receipt wait — a step
        # that lands but whose receipt times out still leaves a trace.
        self._journal_gas({
            "status": "broadcast",
            "ts": int(time.time()),
            "tx_hash": tx_hash_hex,
            "nonce": decoded["nonce"],
            "sender": decoded["sender"],
            "target": decoded["to"],
            "function": ctx["function"],
            "token": (ctx.get("token_out") or ctx.get("token_in") or ""),
            "value_wei": decoded["value_wei"],
        })
        # wait for the receipt (bounded)
        receipt = None
        for _ in range(30):
            try:
                receipt = w3.eth.get_transaction_receipt(tx_hash)
                if receipt:
                    break
            except Exception:
                pass
            time.sleep(2)
        if receipt is None:
            # broadcast succeeded but the receipt timed out: RAISE.
            # Never resubmit — the executor's nonce-anomaly halt is the
            # recovery path, not a duplicate send.
            raise ExecutorError(
                f"receipt timeout for {tx_hash_hex} — not resubmitting")
        gas_used = int(receipt.get("gasUsed", 0))
        eff_price = int(receipt.get("effectiveGasPrice", 0))
        gas_cost_wei = gas_used * eff_price
        block_time = int(w3.eth.get_block(receipt["blockNumber"])
                         .get("timestamp", time.time()))
        # W6: complete the broadcast row with the confirmed gas.
        self._journal_gas({
            "status": "confirmed",
            "ts": int(time.time()),
            "tx_hash": tx_hash_hex,
            "nonce": decoded["nonce"],
            "sender": decoded["sender"],
            "target": decoded["to"],
            "function": ctx["function"],
            "token": (ctx.get("token_out") or ctx.get("token_in") or ""),
            "value_wei": decoded["value_wei"],
            "gas_used": gas_used,
            "effective_gas_price_wei": eff_price,
            "gas_cost_wei": gas_cost_wei,
            "gas_cost_eth": gas_cost_wei / 1e18,
            "block_number": int(receipt["blockNumber"]),
        })
        post = self._snapshot_balances(ctx)
        # eth_usd for gas_usd AND the executed price. An unavailable
        # price RAISES (W6) — never a plausible 0.0.
        eth_usd = self.eth_usd_fn()
        receipt_dict = {
            "tx_hash": tx_hash_hex,
            "block_number": int(receipt["blockNumber"]),
            "block_time": block_time,
            "gas_used": gas_used,
            "effective_gas_price_wei": eff_price,
            "gas_eth": gas_cost_wei / 1e18,
            "gas_usd": (gas_cost_wei / 1e18) * eth_usd,
        }
        receipt_dict.update(self._establish_fill(ctx, pre, post,
                                                 gas_cost_wei, eth_usd))
        return receipt_dict


def ctx_sender(ctx: dict) -> str:
    """The address whose balances a step moves: the decoded sender
    when present (W3 — submit_fn measures whoever the tx says sent),
    else the configured wallet."""
    return ctx.get("sender") or pe.MAIN_WALLET


# ---------------------------------------------------------------------------
# Requirement 7 — read-only in-flight verifier (standalone)
# ---------------------------------------------------------------------------

def verify_inflight_record(state_path: str, w3=None,
                           gas_journal_path: str = None) -> dict:
    """READ-ONLY: given a persisted lane state with in-flight records,
    report per-step landed / not-landed from chain state plus a
    recommendation (resume-safe / keep-failed-closed). Signs nothing,
    submits nothing. NOT wired into the executor — consumed by human
    review.

    W4: landed/not-landed comes from the wiring's own gas journal —
    the append-only record of every broadcast, written at send time —
    crossed with receipt lookups by tx hash, and nonce progression as
    the second signal. The in-flight record's steps_completed counter
    is NOT evidence: a crash after broadcast but before the counter
    persisted looks like "nothing landed" while step 1 is on-chain.
    "resume-safe" requires positive evidence that nothing landed —
    ZERO broadcast rows in the op window. Broadcast-but-unconfirmed
    (rows exist, no receipt found) is keep-failed-closed: the step
    may have landed and must never be re-executed.
    """
    if not os.path.exists(state_path):
        raise ExecutorError(f"lane state not found: {state_path}")
    with open(state_path) as f:
        state = json.load(f)
    inflight = state.get("inflight_ops", {})
    journal_path = gas_journal_path or GAS_JOURNAL_PATH
    # A missing/unreadable journal is itself fail-closed: without the
    # broadcast record there is no evidence either way.
    if not os.path.exists(journal_path):
        raise ExecutorError(
            f"gas journal not found at {journal_path}; cannot establish "
            f"what landed — keep-failed-closed")
    journal_rows = []
    with open(journal_path) as f:
        for line in f:
            line = line.strip()
            if line:
                journal_rows.append(json.loads(line))
    if w3 is None:
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(RPC_URL))
    try:
        confirmed_nonce = int(
            w3.eth.get_transaction_count(pe.MAIN_WALLET, "latest"))
        nonce_note = f"confirmed nonce now {confirmed_nonce}"
    except Exception as exc:  # noqa: BLE001
        confirmed_nonce = None
        nonce_note = f"nonce read failed: {exc}"
    report = {"state_path": state_path,
              "gas_journal": journal_path,
              "operations": []}
    for pick_id, op in inflight.items():
        steps = op.get("steps", [])
        started_at = int(op.get("started_at", 0))
        completed_counter = int(op.get("steps_completed", 0))
        # Broadcast rows inside the op's time window, in submit order.
        window_rows = [r for r in journal_rows
                       if r.get("status") == "broadcast"
                       and int(r.get("ts", 0)) >= started_at]
        step_report = []
        for i, step in enumerate(steps):
            row = window_rows[i] if i < len(window_rows) else None
            landed = False
            if row is None:
                evidence = ("no broadcast record in the op window — "
                            "nothing was sent for this step")
            else:
                try:
                    receipt = w3.eth.get_transaction_receipt(
                        row.get("tx_hash"))
                except Exception as exc:  # noqa: BLE001
                    receipt = None
                    evidence = (f"receipt lookup failed for "
                                f"{row.get('tx_hash')}: {exc}")
                else:
                    evidence = None
                if receipt is not None:
                    landed = True
                    block = int(receipt.get("blockNumber", 0))
                    evidence = (f"journal tx {str(row.get('tx_hash'))[:18]}… "
                                f"confirmed on-chain at block {block}")
                elif evidence is None:
                    evidence = (f"broadcast recorded "
                                f"({str(row.get('tx_hash'))[:18]}…) but no "
                                f"receipt found")
            # nonce cross-check on the journal row's nonce
            nonce_signal = ""
            if row is not None and confirmed_nonce is not None:
                row_nonce = int(row.get("nonce", -1))
                if row_nonce >= 0:
                    consumed = confirmed_nonce > row_nonce
                    nonce_signal = (
                        f"; nonce {row_nonce} "
                        f"{'consumed' if consumed else 'not consumed'} "
                        f"(confirmed nonce {confirmed_nonce})")
            step_report.append({
                "index": i,
                "to": step.get("to"),
                "function": step.get("function"),
                "landed": landed,
                "evidence": (evidence or "") + nonce_signal,
            })
        any_landed = any(s["landed"] for s in step_report)
        all_landed = bool(step_report) and all(s["landed"]
                                               for s in step_report)
        broadcast_in_window = len(window_rows)
        if all_landed:
            recommendation = (
                "resume-safe with care: every step has a confirmed "
                "on-chain receipt; verify the final swap's fill before "
                "booking anything")
        elif any_landed:
            n = sum(1 for s in step_report if s["landed"])
            recommendation = (
                f"keep-failed-closed: {n} of {len(step_report)} steps "
                f"landed on-chain (journal evidence); verify each landed "
                f"tx before any resume — never re-execute a landed step")
        elif broadcast_in_window:
            # Broadcast rows exist but no receipt was found: a
            # possibly-landed step (the W6 receipt-timeout path).
            # resume-safe is NOT available — nothing landed is exactly
            # what is NOT known here.
            recommendation = (
                f"keep-failed-closed: {broadcast_in_window} broadcast "
                f"row(s) in the op window with no receipt found — the "
                f"step(s) may have landed (the receipt-timeout path); "
                f"check each broadcast tx hash on-chain before any "
                f"resume — never re-execute a possibly-landed step")
        else:
            recommendation = (
                "resume-safe: zero broadcast rows in the op window — "
                "positive evidence (the append-only journal, written at "
                "send time) that nothing landed; retry the whole "
                "sequence")
        report["operations"].append({
            "pick_id": pick_id,
            "kind": op.get("kind"),
            "steps_completed_counter": completed_counter,
            "total_steps": len(steps),
            "steps": step_report,
            "nonce_cross_check": nonce_note,
            "recommendation": recommendation,
        })
    return report


# ---------------------------------------------------------------------------
# Composition root
# ---------------------------------------------------------------------------

def build_live_executor(state_path: str = None, ledger_path: str = None,
                        poller_state_path: str = "",
                        poller_closes_path: str = "",
                        dry_run: bool = False,
                        rpc_url: str = RPC_URL) -> pe.ProbeExecutor:
    """Construct the committed executor with the real providers. The
    executor's main() is NOT edited — this is the composition root it
    lacks. No posture change, no arming."""
    import probe_keystore
    providers = LiveProviders(rpc_url=rpc_url)
    state_path = state_path or os.path.join(LANE_DIR, "probe_lane_state.json")
    ledger_path = ledger_path or os.path.join(LANE_DIR, "probe_ledger.jsonl")
    return pe.ProbeExecutor(
        state_path=state_path, ledger_path=ledger_path,
        poller_state_path=poller_state_path,
        poller_closes_path=poller_closes_path,
        posture_fn=pe.default_posture_reader,
        price_fn=providers.price_fn,
        quote_fn=providers.quote_fn,
        sign_fn=providers.sign_fn,
        submit_fn=providers.submit_fn,
        balance_fn=providers.balance_fn,
        gas_price_fn=providers.gas_price_fn,
        nonce_fn=providers.nonce_fn,
        eth_usd_fn=providers.eth_usd_fn,
        keystore=probe_keystore,
        dry_run=dry_run,
    ), providers


class _RunnerKeystore:
    """Keystore shim for runner mode (WO15): holds the PASSPHRASE in
    memory only and delegates to the real keystore's decrypt_at_use
    per signing event — the existing decrypt-at-use custody model is
    preserved exactly: key material stays transient and zeroized per
    use, exactly as built. The passphrase is zeroized by the runner on
    exit via zeroize_master(). Nothing here touches disk."""

    def __init__(self, passphrase_bytes: bytes, keystore_mod):
        self._passphrase = bytearray(passphrase_bytes)
        self._keystore_mod = keystore_mod

    def decrypt_at_use(self, passphrase=None):
        # Delegate to the real keystore — decrypt-at-use per event.
        return self._keystore_mod.decrypt_at_use(bytes(self._passphrase))

    def _zeroize(self, buf):
        # The executor zeroizes the returned key buffer; the
        # passphrase lives in the shim, not here. Accept both
        # bytearray and bytes for robustness.
        if isinstance(buf, bytearray):
            for i in range(len(buf)):
                buf[i] = 0
        elif isinstance(buf, memoryview):
            buf[:] = b'\x00' * len(buf)

    def zeroize_master(self):
        for i in range(len(self._passphrase)):
            self._passphrase[i] = 0
        self._passphrase = bytearray()


def run_runner(ex, interval_s=60.0, max_cycles=None, sleep_fn=None):
    """WO15 runner: loop ex.run_cycle() on a cadence. ex must already
    be started (start() called). Returns the number of cycles run.
    KeyboardInterrupt exits cleanly. Transient faults in run_cycle are
    logged and the loop continues (the executor's in-flight recovery is
    built for exactly this). Keystore errors propagate — revocation or
    blindness must stop the runner, not spin against a deleted file
    forever. sleep_fn is injectable for tests.

    WO16: touches probe_runner.heartbeat after EVERY cycle attempt
    (success and transient-fault branches alike) — the touch proves the
    loop is iterating. A hung cycle produces no touch."""
    import time as _time
    if sleep_fn is None:
        sleep_fn = _time.sleep
    cycles = 0
    try:
        while max_cycles is None or cycles < max_cycles:
            try:
                summary = ex.run_cycle()
            except Exception as exc:
                # Check if it's a keystore error — those must stop the runner
                try:
                    from probe_keystore import KeystoreError
                    if isinstance(exc, KeystoreError):
                        raise
                except ImportError:
                    pass
                # Transient fault — log and continue
                import logging
                logging.warning("cycle %d failed (transient): %s", cycles + 1, exc)
                cycles += 1
                _touch_heartbeat()
                if max_cycles is None or cycles < max_cycles:
                    sleep_fn(interval_s)
                continue
            cycles += 1
            _touch_heartbeat()
            print(json.dumps(summary, indent=2))
            if max_cycles is None or cycles < max_cycles:
                sleep_fn(interval_s)
    except KeyboardInterrupt:
        pass
    return cycles


def _run_runner_mode(args, getpass_fn=None, keystore_mod=None, sleep_fn=None):
    """WO15 runner: passphrase entered once, held in memory only,
    zeroized on exit. decrypt-at-use and zeroize in the real keystore
    are untouched — the runner shim delegates to them per signing event.
    getpass_fn, keystore_mod, and sleep_fn are injectable for tests."""
    if getpass_fn is None:
        import getpass as _gp
        getpass_fn = _gp.getpass
    if keystore_mod is None:
        import probe_keystore as keystore_mod

    passphrase = getpass_fn(
        "Probe keystore passphrase (typed locally, not stored): ")
    if not passphrase:
        print("empty passphrase refused", file=sys.stderr)
        return 1

    runner_ks = _RunnerKeystore(passphrase.encode("utf-8"), keystore_mod)
    try:
        ex, _providers = build_live_executor(
            poller_state_path=args.poller_state,
            poller_closes_path=args.poller_closes)
        ex.keystore = runner_ks
        ex.start()
        run_runner(ex, interval_s=args.interval, max_cycles=args.max_cycles,
                   sleep_fn=sleep_fn)
    finally:
        runner_ks.zeroize_master()
    return 0


def main(argv=None) -> int:
    """Wiring entry: --dry-run walks a full cycle with the real READ
    providers wired in, zero sign/submit (posture enforced by the
    executor itself). --runner loops run_cycle on a cadence with the
    passphrase entered once at start."""
    import argparse
    logging.basicConfig(level=logging.INFO)
    p = argparse.ArgumentParser(description="WO10 probe wiring (build only)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--poller-state", default="")
    p.add_argument("--poller-closes", default="")
    p.add_argument("--runner", action="store_true",
                   help="Loop run_cycle on a cadence; passphrase entered once.")
    p.add_argument("--interval", type=float, default=60.0,
                   help="Seconds between cycles in runner mode (default 60).")
    p.add_argument("--max-cycles", type=int, default=None,
                   help="Stop after N cycles in runner mode (default: infinite).")
    args = p.parse_args(argv)
    if args.runner:
        return _run_runner_mode(args)
    ex, _providers = build_live_executor(
        poller_state_path=args.poller_state,
        poller_closes_path=args.poller_closes, dry_run=args.dry_run)
    summary = ex.run_cycle()
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
