#!/usr/bin/env python3
"""probe_executor.py — WO9 live executor for the session-key probe (BUILD ONLY).

Consumes the keystore built in WO7/WO8/WO8b and, once posture gates allow
it, mirrors the paper poller's leaders with real fills at probe size — so
the fill-gap scorecard can be judged on real executions instead of paper.

Per spec §11 (2026-10-13 redesign): the probe operates from the MAIN wallet
0xe7a16Be2aF1eCE8922d9d3aD55C03e29004e540F. C = 0.0100 ETH is a VIRTUAL pot
enforced by this executor's accounting, not by wallet balance.

This module changes NO posture. It arms nothing. In the current disarmed
posture (kill_switch=true, live_trading=false) a live-mode run refuses to
sign anything — fail-closed at every junction. `--dry-run` is the pre-arm
rehearsal instrument and works while disarmed.

Design for testability: every external dependency (posture reader, price
chain, quoters, signer, submitter, balance, gas, nonce, keystore module,
clock) is injectable. Tests pass mocks; nothing live is touched. The only
real key path is `decrypt_at_use()` called immediately before each signing
event, with the key held in a bytearray and zeroized after — including on
exception paths.

Dependencies: eth_keys / eth_account (venv), probe_keystore (same repo),
copy_trader.price_chain (same repo, reused — never a second price path).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

log = logging.getLogger("probe_executor")

# ---------------------------------------------------------------------------
# Constants (WO9 §5 / spec §2/§3/§4)
# ---------------------------------------------------------------------------

MAIN_WALLET = "0xe7a16Be2aF1eCE8922d9d3aD55C03e29004e540F"

C_POT_ETH = 0.0100            # virtual pot (spec §3)
TRADE_SIZE_ETH = C_POT_ETH / 50   # 0.0002 — exactly, never variable
DAILY_LOSS_HALT_ETH = C_POT_ETH / 10  # 0.001 (McKenzie ruling 2026-10-12)
PROBE_MAX_SLIPPAGE_BPS = float(
    os.environ.get("PROBE_MAX_SLIPPAGE_BPS", "150"))  # OPEN DECISION #1
PROBE_MAX_TRADES = int(os.environ.get("PROBE_MAX_TRADES", "50"))  # #3
PROBE_EXPIRY_DAYS = 30        # spec §4
MAX_HOLD_DAYS = 7             # poller's discipline, applied to follower entry
SWAP_DEADLINE_SECONDS = 120   # spec §8
GAS_SPIKE_FACTOR = 10         # spec §6 — >10x trailing 1h median = skip
GAS_LIMIT_ESTIMATE = 250_000  # rough per-swap gas for the wallet sanity check

# Allowlist (spec §2) — the key may sign ONLY these.
WETH = "0x4200000000000000000000000000000000000006"
AERODROME_ROUTER = "0xcf77a3ba9a5ca399b7c97c74d54e5b1beb874e43"
UNISWAP_ROUTER02 = "0x2626664c2603336e57b271c5c0b26f421741e481"

ALLOWED_FUNCTIONS = {
    WETH: {"deposit", "withdraw", "approve"},
    AERODROME_ROUTER: {"swapExactETHForTokens", "swapExactTokensForETH"},
    UNISWAP_ROUTER02: {"exactInputSingle", "exactInput"},
}
ROUTERS = {AERODROME_ROUTER, UNISWAP_ROUTER02}

# Project-root-absolute runtime files (WO1 absolute-path rule — no
# CWD-relative paths; a CWD-relative default silently writes state
# elsewhere when the executor is launched from a different directory).
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE_PATH = os.path.join(PROJECT_ROOT, "probe_lane_state.json")
DEFAULT_LEDGER_PATH = os.path.join(PROJECT_ROOT, "probe_ledger.jsonl")

# Posture source: the SAME file the paper stack's arm gate reads (caps.json),
# read-only here. (risk.load_caps() itself may REWRITE caps.json when it
# force-disarms; the executor must never write posture, so it reads the
# file directly instead of calling that loader.)
CAPS_FILE = os.path.join(PROJECT_ROOT, "caps.json")


class ExecutorError(Exception):
    """Fail-closed: any condition that prevents a signature."""


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class UnsignedTx:
    """A constructed, unsigned transaction pending every gate."""
    to: str                 # checksummed contract address
    function: str           # e.g. swapExactETHForTokens / approve / deposit
    value_eth: float        # ETH value carried by the tx (buys)
    amount_in_eth: float    # ETH-notional amount in (cap checks, ≤ C/50)
    token: str = ""         # token being traded ("" for wrap ops)
    side: str = "buy"       # buy | sell
    deadline_ts: int = 0    # unix seconds, ≤ build_time + 120
    spender: str = ""       # approve only; must be an allowlisted router
    approve_amount: float = 0.0  # approve only; must be ≤ position tokens
    position_token_amount: float = 0.0  # approve bound: the trade's tokens


@dataclass
class Quote:
    """Best executable quote from the allowlisted venues' quoters."""
    venue: str              # aerodrome | uniswap_v3
    amount_out: float       # token (buy) or ETH (sell) expected out
    implied_price_usd: float
    routable: bool = True


@dataclass
class LanePosition:
    """One open live position, keyed by pick_id."""
    pick_id: str
    leader_wallet: str
    token: str
    size_eth: float
    # entry
    entry_quoted_price_usd: float
    entry_executed_price_usd: float
    entry_tx_hash: str
    entry_block: int
    entry_time: int          # unix seconds (follower's own entry)
    leader_entry_price_usd: float
    leader_entry_time: int
    entry_gas_eth: float = 0.0
    entry_gas_usd: float = 0.0
    token_amount: float = 0.0  # tokens received (for the sell leg)
    # exit (filled at close)
    exit_quoted_price_usd: float = 0.0
    exit_executed_price_usd: float = 0.0
    exit_tx_hash: str = ""
    exit_block: int = 0
    exit_time: int = 0
    leader_exit_price_usd: float = 0.0
    leader_exit_time: int = 0
    exit_gas_eth: float = 0.0
    exit_gas_usd: float = 0.0
    close_reason: str = ""


@dataclass
class LaneState:
    """Persistent virtual-pot accounting + lane bookkeeping (spec §6)."""
    cumulative_entry_notional_eth: float = 0.0
    cumulative_gas_eth: float = 0.0
    cumulative_gas_usd: float = 0.0
    realized_pnl_eth: float = 0.0
    day_bucket: dict = field(default_factory=dict)      # UTC day -> realized
    executed_trades: int = 0                            # entries executed
    arm_timestamp: int = 0                              # spec §4 expiry anchor
    open_positions: dict = field(default_factory=dict)  # pick_id -> LanePosition
    processed_pick_ids: list = field(default_factory=list)
    # WO9-F1: in-flight multi-step operations. pick_id -> {
    #   "kind": "entry"|"exit", "steps": [step summaries],
    #   "steps_completed": int, "started_at": int}
    # Persisted before every step so a crash between steps leaves the
    # sequence recoverable (or fail-closed) on restart — never double-booked.
    inflight_ops: dict = field(default_factory=dict)
    # F3b: handled close-row keys (pick_id + close_ts, or pick_id +
    # close_price when close_ts is absent). The poller's closes journal
    # is re-read in full every cycle; without this set a matched close
    # would emit a false `unmatched` row on every subsequent cycle,
    # forever, and a genuine unmatched would re-emit forever.
    handled_close_keys: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "cumulative_entry_notional_eth": self.cumulative_entry_notional_eth,
            "cumulative_gas_eth": self.cumulative_gas_eth,
            "cumulative_gas_usd": self.cumulative_gas_usd,
            "realized_pnl_eth": self.realized_pnl_eth,
            "day_bucket": dict(self.day_bucket),
            "executed_trades": self.executed_trades,
            "arm_timestamp": self.arm_timestamp,
            "open_positions": {k: asdict(v)
                               for k, v in self.open_positions.items()},
            "processed_pick_ids": list(self.processed_pick_ids),
            "inflight_ops": dict(self.inflight_ops),
            "handled_close_keys": list(self.handled_close_keys),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LaneState":
        s = cls()
        s.cumulative_entry_notional_eth = float(
            d.get("cumulative_entry_notional_eth", 0.0))
        s.cumulative_gas_eth = float(d.get("cumulative_gas_eth", 0.0))
        s.cumulative_gas_usd = float(d.get("cumulative_gas_usd", 0.0))
        s.realized_pnl_eth = float(d.get("realized_pnl_eth", 0.0))
        s.day_bucket = {str(k): float(v)
                        for k, v in d.get("day_bucket", {}).items()}
        s.executed_trades = int(d.get("executed_trades", 0))
        s.arm_timestamp = int(d.get("arm_timestamp", 0))
        s.open_positions = {k: LanePosition(**v)
                            for k, v in d.get("open_positions", {}).items()}
        s.processed_pick_ids = [str(p)
                                for p in d.get("processed_pick_ids", [])]
        s.inflight_ops = {str(k): dict(v)
                          for k, v in d.get("inflight_ops", {}).items()}
        s.handled_close_keys = [str(k)
                                for k in d.get("handled_close_keys", [])]
        return s


# ---------------------------------------------------------------------------
# Default (injectable) dependency implementations
# ---------------------------------------------------------------------------

def default_posture_reader() -> tuple:
    """Read (kill_switch, live_trading) from caps.json — read-only."""
    with open(CAPS_FILE) as f:
        c = json.load(f)
    return bool(c.get("kill_switch")), bool(c.get("live_trading"))


def _utc_day(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# The executor
# ---------------------------------------------------------------------------

class ProbeExecutor:
    """The WO9 executor. All external touchpoints are injected."""

    def __init__(self, state_path: str = DEFAULT_STATE_PATH,
                 ledger_path: str = DEFAULT_LEDGER_PATH,
                 poller_state_path: str = "",
                 poller_closes_path: str = "",
                 *,
                 posture_fn=default_posture_reader,
                 price_fn=None, quote_fn=None, sign_fn=None,
                 submit_fn=None, balance_fn=None, gas_price_fn=None,
                 nonce_fn=None, eth_usd_fn=None,
                 keystore=None, now_fn=time.time, dry_run: bool = False):
        self.state_path = os.path.abspath(state_path)
        self.ledger_path = os.path.abspath(ledger_path)
        self.poller_state_path = (os.path.abspath(poller_state_path)
                                  if poller_state_path else "")
        self.poller_closes_path = (os.path.abspath(poller_closes_path)
                                   if poller_closes_path else "")
        self.posture_fn = posture_fn
        self.price_fn = price_fn          # (token) -> (price_usd|None, reason)
        self.quote_fn = quote_fn          # (token, side, size_eth) -> Quote|None
        self.sign_fn = sign_fn            # (UnsignedTx, key_bytearray) -> bytes
        self.submit_fn = submit_fn        # (signed) -> receipt dict
        self.balance_fn = balance_fn      # () -> eth balance float
        self.gas_price_fn = gas_price_fn  # () -> (current_gwei, median1h_gwei)
        self.nonce_fn = nonce_fn          # () -> (confirmed, pending)
        self.eth_usd_fn = eth_usd_fn      # () -> eth price usd
        # keystore: module exposing decrypt_at_use() + _zeroize().
        if keystore is None:
            import probe_keystore as keystore  # noqa: F811
        self.keystore = keystore
        self.now_fn = now_fn
        self.dry_run = dry_run
        self.state = self._load_state()
        self._verified_key = False

    # -- state persistence (atomic: write-temp + rename) --------------------

    def _load_state(self) -> LaneState:
        if os.path.exists(self.state_path):
            with open(self.state_path) as f:
                return LaneState.from_dict(json.load(f))
        return LaneState()

    def save_state(self) -> None:
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state.to_dict(), f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.state_path)

    def _append_ledger(self, row: dict) -> None:
        os.makedirs(os.path.dirname(self.ledger_path), exist_ok=True)
        with open(self.ledger_path, "a") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")

    # -- poller inputs (read-only) -------------------------------------------

    def _read_poller_open_positions(self) -> dict:
        """pick_id -> position dict from the paper poller's state file."""
        if not self.poller_state_path or not os.path.exists(
                self.poller_state_path):
            return {}
        with open(self.poller_state_path) as f:
            data = json.load(f)
        return data.get("open_positions", {}) or {}

    def _read_poller_closes(self) -> list:
        """Close rows from the paper poller's journal (JSONL)."""
        if not self.poller_closes_path or not os.path.exists(
                self.poller_closes_path):
            return []
        rows = []
        with open(self.poller_closes_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
        return rows

    # -- gates ---------------------------------------------------------------

    def check_posture(self) -> tuple:
        """(ok, reason). In live mode kill_switch OR not live_trading refuses.
        In dry-run the posture is REPORTED, not enforced (WO9 §7)."""
        kill, live = self.posture_fn()
        if kill or not live:
            return False, (f"posture disarmed (kill_switch={kill}, "
                           f"live_trading={live})")
        return True, ""

    def check_allowlist(self, tx: UnsignedTx) -> tuple:
        """(ok, reason). The key may sign ONLY allowlisted calls, bounded.

        WO9-F1 ALLOWLIST CORRECTION: `approve` is permitted on (a) WETH
        and (b) the contract of a token held in an open lane position —
        in both cases spender ∈ allowlisted routers and amount ≤ the
        position's token amount. Every other approve target is rejected
        pre-sign.
        """
        to = str(tx.to or "").lower()
        # F1: `approve` is permitted on WETH AND on the contract of a
        # token held in an open lane position. Every other function is
        # still confined to the original allowlist set.
        held_tokens = {p.token.lower() for p in
                       self.state.open_positions.values()}
        if tx.function == "approve":
            if to != WETH and to not in held_tokens:
                return False, (f"approve target {tx.to} is neither WETH nor "
                               f"a lane-held token")
            if to != WETH and tx.token.lower() != to:
                return False, (f"approve to {tx.to} does not match the "
                               f"lane-held token {tx.token}")
        else:
            if to not in ALLOWED_FUNCTIONS:
                return False, f"to {tx.to} not in allowlist"
            if tx.function not in ALLOWED_FUNCTIONS[to]:
                return False, f"function {tx.function} not allowed on {tx.to}"
        # per-trade ceiling on value/amountIn
        if tx.value_eth > TRADE_SIZE_ETH + 1e-18:
            return False, (f"value {tx.value_eth} exceeds per-trade cap "
                           f"{TRADE_SIZE_ETH}")
        if tx.amount_in_eth > TRADE_SIZE_ETH + 1e-18:
            return False, (f"amountIn {tx.amount_in_eth} exceeds per-trade "
                           f"cap {TRADE_SIZE_ETH}")
        if tx.deadline_ts:
            if tx.deadline_ts - int(self.now_fn()) > SWAP_DEADLINE_SECONDS:
                return False, "deadline beyond 120s"
        if tx.function == "approve":
            if tx.spender.lower() not in ROUTERS:
                return False, f"approve spender {tx.spender} not a router"
            if tx.approve_amount <= 0:
                return False, "approve amount must be positive (never unlimited)"
            # amount ≤ the trade's token amount — bounded per trade, never
            # unlimited. position_token_amount is the bound in TOKEN units;
            # amount_in_eth is the ETH-notional (separate fields, separate
            # units — comparing them was the bug the exit path tripped).
            if (tx.position_token_amount > 0
                    and tx.approve_amount > tx.position_token_amount + 1e-12):
                return False, ("approve amount exceeds the trade's token "
                               "amount (never unlimited)")
        return True, ""

    def check_pot_cap(self) -> tuple:
        """Cumulative entry notional + trade must not cross C."""
        projected = (self.state.cumulative_entry_notional_eth
                     + TRADE_SIZE_ETH)
        if projected > C_POT_ETH + 1e-12:
            return False, (f"pot cap: {projected} > C={C_POT_ETH} "
                           f"(spent {self.state.cumulative_entry_notional_eth})")
        return True, ""

    def check_daily_loss_halt(self, now: int) -> tuple:
        """Day-bucket realized PnL ≤ -0.001 → halt ENTRIES (exits allowed)."""
        day = _utc_day(now)
        bucket = self.state.day_bucket.get(day, 0.0)
        if bucket <= -DAILY_LOSS_HALT_ETH + 1e-18:
            return False, (f"daily-loss halt: {day} realized {bucket} ≤ "
                           f"-{DAILY_LOSS_HALT_ETH}; entries halted "
                           f"(exits still permitted)")
        return True, ""

    def check_expiry(self, now: int) -> tuple:
        """Refuse NEW ENTRIES once 30 days from arm or trade cap reached."""
        if self.state.arm_timestamp:
            age_days = (now - self.state.arm_timestamp) / 86400.0
            if age_days >= PROBE_EXPIRY_DAYS:
                return False, (f"expired: {age_days:.1f}d ≥ "
                               f"{PROBE_EXPIRY_DAYS}d from arm; new entries "
                               f"refused (wind-down exits permitted)")
        if self.state.executed_trades >= PROBE_MAX_TRADES:
            return False, (f"expired: executed_trades "
                           f"{self.state.executed_trades} ≥ "
                           f"{PROBE_MAX_TRADES}; new entries refused "
                           f"(wind-down exits permitted)")
        return True, ""

    def check_wallet_sanity(self, size_eth: float) -> tuple:
        """Physical fence: balance ≥ trade size + estimated gas."""
        if self.balance_fn is None:
            return True, ""  # not injectable in this run; pot cap is the fence
        bal = float(self.balance_fn())
        need = size_eth
        if self.gas_price_fn is not None:
            cur, _med = self.gas_price_fn()
            need += (cur * 1e9 * GAS_LIMIT_ESTIMATE) / 1e18
        if bal < need:
            return False, (f"wallet sanity: balance {bal} < size+gas "
                           f"{need}; refusing")
        return True, ""

    def check_gas_spike(self) -> tuple:
        """Effective gas price > 10x trailing 1h median → skip the trade."""
        if self.gas_price_fn is None:
            return True, ""
        cur, med = self.gas_price_fn()
        if med > 0 and cur > GAS_SPIKE_FACTOR * med:
            return False, (f"gas spike: {cur} gwei > {GAS_SPIKE_FACTOR}x "
                           f"median {med} gwei; skipping")
        return True, ""

    def check_nonce_anomaly(self) -> tuple:
        """Unexpected pending tx / nonce gap → halt signing."""
        if self.nonce_fn is None:
            return True, ""
        confirmed, pending = self.nonce_fn()
        if pending != confirmed:
            return False, (f"nonce/pending anomaly: confirmed {confirmed} "
                           f"!= pending {pending}; signing halted")
        return True, ""

    # -- key handling ----------------------------------------------------------

    def verify_startup_key(self) -> str:
        """One decrypt + address derivation at startup. Fail-closed on any
        mismatch. Returns the derived checksum address."""
        raw = self.keystore.decrypt_at_use()
        buf = bytearray(raw)
        try:
            from eth_keys import keys as _eth_keys
            address = (_eth_keys.PrivateKey(bytes(buf))
                       .public_key.to_checksum_address())
        finally:
            self.keystore._zeroize(buf)
        if address.lower() != MAIN_WALLET.lower():
            raise ExecutorError(
                f"startup key derivation {address} != main wallet "
                f"{MAIN_WALLET}; refusing to run")
        self._verified_key = True
        log.info("startup key verified against %s", address)
        return address

    def _sign(self, tx: UnsignedTx) -> bytes:
        """Decrypt immediately before signing; zeroize after — always."""
        key = self.keystore.decrypt_at_use()
        buf = bytearray(key)
        try:
            if self.sign_fn is not None:
                return self.sign_fn(tx, buf)
            # Live path: sign with eth_account (never exercised in tests).
            import eth_account
            from eth_account import Account
            tx_dict = {
                "to": tx.to, "value": int(tx.value_eth * 1e18),
                "gas": GAS_LIMIT_ESTIMATE, "nonce": 0, "chainId": 8453,
            }
            signed = Account.sign_transaction(tx_dict, bytes(buf))
            return signed.rawTransaction
        finally:
            self.keystore._zeroize(buf)

    # -- price / quote helpers -------------------------------------------------

    def _price_or_none(self, token: str):
        """(price_usd, reason). Fail-closed: no price → None."""
        if self.price_fn is None:
            from copy_trader.price_chain import fetch_price_consensus
            res, _diag = fetch_price_consensus(token)
            return (res.price if res.ok else None), res.reason
        price, reason = self.price_fn(token)
        return (price if price else None), reason

    def _quote(self, token: str, side: str, size_eth: float):
        """(Quote|None, reason). Best executable quote or None."""
        if self.quote_fn is None:
            return None, "no quoter configured"
        return self.quote_fn(token, side, size_eth), ""

    def _slippage_bps(self, quoted: float, executed: float) -> float:
        """Positive = adverse vs the quote."""
        if not quoted:
            return 0.0
        return (executed - quoted) / quoted * 10_000.0

    def _quote_vs_median_bps(self, quote_price: float, median: float) -> float:
        """Adverse slippage of the QUOTE itself vs the price-chain median."""
        if not median:
            return 0.0
        return (quote_price - median) / median * 10_000.0

    # -- entry / exit ------------------------------------------------------------

    def process_entry(self, signal: dict, now: int) -> dict:
        """One leader BUY signal → (maybe) a real entry. Fail-closed order."""
        pick_id = str(signal.get("pick_id", ""))
        result = {"pick_id": pick_id, "action": "skip", "reason": ""}

        # Dedup: a pick_id is acted on once, across restarts.
        if pick_id in self.state.processed_pick_ids:
            result["reason"] = "dedup: already processed"
            return result
        if pick_id in self.state.open_positions:
            result["reason"] = "dedup: position already open"
            return result

        token = str(signal.get("token", ""))
        leader_wallet = str(signal.get("wallet_address", ""))
        leader_entry_price = float(signal.get("entry_price") or 0.0)
        leader_entry_time = int(signal.get("open_ts") or 0)

        # Gates (entries): posture / expiry / pot / daily-loss.
        ok, why = self.check_posture()
        if not ok and not self.dry_run:
            result["reason"] = f"refused: {why}"
            log.warning("entry refused (%s): %s", pick_id, why)
            return result
        ok, why = self.check_expiry(now)
        if not ok:
            result["reason"] = f"refused: {why}"
            self._append_ledger({"ts": now, "pick_id": pick_id,
                                 "row": "expiry_skip", "reason": why})
            return result
        ok, why = self.check_pot_cap()
        if not ok:
            result["reason"] = f"refused: {why}"
            self._append_ledger({"ts": now, "pick_id": pick_id,
                                 "row": "pot_cap_skip", "reason": why})
            return result
        ok, why = self.check_daily_loss_halt(now)
        if not ok:
            result["reason"] = f"halted: {why}"
            log.warning("lane halt (%s): %s", pick_id, why)
            return result
        ok, why = self.check_gas_spike()
        if not ok:
            result["reason"] = f"skipped: {why}"
            self._append_ledger({"ts": now, "pick_id": pick_id,
                                 "row": "gas_spike_skip", "reason": why})
            return result
        ok, why = self.check_nonce_anomaly()
        if not ok:
            result["reason"] = f"halted: {why}"
            log.warning("signing halt (%s): %s", pick_id, why)
            return result

        # Price first. A trade that can't be priced doesn't fire.
        price, reason = self._price_or_none(token)
        if not price:
            result["reason"] = "price_failure_skip"
            log.warning("price failure (%s): %s", pick_id, reason)
            self._append_ledger({"ts": now, "pick_id": pick_id,
                                 "row": "price_failure_skip",
                                 "reason": reason})
            self.state.processed_pick_ids.append(pick_id)
            self.save_state()
            return result

        # Quotability gate.
        quote, qreason = self._quote(token, "buy", TRADE_SIZE_ETH)
        if quote is None or not quote.routable or quote.amount_out <= 0:
            result["reason"] = "unroutable_skip"
            log.warning("unroutable (%s): %s", pick_id, qreason)
            self._append_ledger({"ts": now, "pick_id": pick_id,
                                 "row": "unroutable_skip",
                                 "reason": qreason})
            self.state.processed_pick_ids.append(pick_id)
            self.save_state()
            return result
        # slippage of the best quote vs the median must be within tolerance
        quote_vs_median = self._quote_vs_median_bps(quote.implied_price_usd,
                                                    price)
        if abs(quote_vs_median) > PROBE_MAX_SLIPPAGE_BPS:
            result["reason"] = "unroutable_skip"
            log.warning("unroutable (%s): quote %s bps vs median %s bps",
                        pick_id, quote_vs_median, PROBE_MAX_SLIPPAGE_BPS)
            self._append_ledger({"ts": now, "pick_id": pick_id,
                                 "row": "unroutable_skip",
                                 "reason": f"quote_vs_median_bps="
                                           f"{quote_vs_median:.1f} > "
                                           f"{PROBE_MAX_SLIPPAGE_BPS}"})
            self.state.processed_pick_ids.append(pick_id)
            self.save_state()
            return result

        # Build the ordered STEP SEQUENCE (venue-dependent shape, F1).
        steps = self._build_entry_steps(token, quote)
        for step in steps:
            ok, why = self.check_allowlist(step)
            if not ok:
                result["reason"] = f"allowlist: {why}"
                log.warning("allowlist rejection (%s): %s", pick_id, why)
                self._append_ledger({"ts": now, "pick_id": pick_id,
                                     "row": "allowlist_reject", "reason": why})
                return result
        ok, why = self.check_wallet_sanity(TRADE_SIZE_ETH)
        if not ok:
            result["reason"] = f"refused: {why}"
            log.warning("wallet sanity refusal (%s): %s", pick_id, why)
            return result

        if self.dry_run:
            result["action"] = "dry_run"
            result["reason"] = (f"unsigned {len(steps)}-step sequence built; "
                                f"no signing")
            log.info("DRY-RUN entry (%s): %d steps: %s", pick_id, len(steps),
                     " -> ".join(f"{s.function}@{s.to[:10]}"
                                 for s in steps))
            return result

        # Execute the sequence. Unhandled exception → NO signature,
        # observe-only. Position is booked ONLY from the FINAL swap step's
        # receipt — a lone deposit/approve is never a fill.
        receipts, failure = self._execute_steps(pick_id, "entry", steps, now)
        if failure is not None:
            self.state.inflight_ops.pop(pick_id, None)
            self.save_state()
            result["reason"] = (f"sequence failed at step "
                                f"{failure['step']}: {failure['reason']}")
            log.error("entry sequence failed (%s): %s", pick_id,
                      result["reason"])
            return result
        self.state.inflight_ops.pop(pick_id, None)

        # Book from the FINAL swap step's receipt only.
        receipt = receipts[-1]
        executed_price = float(receipt.get("executed_price_usd") or 0.0)
        # gas = sum across all steps of the sequence
        gas_eth = sum(float(r.get("gas_eth", 0.0)) for r in receipts)
        gas_usd = sum(float(r.get("gas_usd", 0.0)) for r in receipts)
        pos = LanePosition(
            pick_id=pick_id,
            leader_wallet=leader_wallet,
            token=token,
            size_eth=TRADE_SIZE_ETH,
            entry_quoted_price_usd=quote.implied_price_usd,
            entry_executed_price_usd=executed_price,
            entry_tx_hash=str(receipt.get("tx_hash", "")),
            entry_block=int(receipt.get("block_number", 0)),
            entry_time=int(receipt.get("block_time", now)),
            leader_entry_price_usd=leader_entry_price,
            leader_entry_time=leader_entry_time,
            entry_gas_eth=gas_eth,
            entry_gas_usd=gas_usd,
            token_amount=float(receipt.get("amount_out", 0.0)),
        )
        self.state.open_positions[pick_id] = pos
        self.state.processed_pick_ids.append(pick_id)
        self.state.cumulative_entry_notional_eth += TRADE_SIZE_ETH
        self.state.cumulative_gas_eth += pos.entry_gas_eth
        self.state.cumulative_gas_usd += pos.entry_gas_usd
        self.state.executed_trades += 1
        self.save_state()
        result["action"] = "entered"
        result["reason"] = f"filled at {executed_price} USD"
        return result

    def process_exit(self, pick_id: str, close_reason: str,
                     now: int, leader_exit_price: float = 0.0,
                     leader_exit_time: int = 0) -> dict:
        """Close the full live position. Exits permitted in wind-down.

        F2: leader_exit_price/time are threaded from the poller close row
        by run_cycle; production receipts carry real leader data.
        """
        result = {"pick_id": pick_id, "action": "skip", "reason": ""}
        pos = self.state.open_positions.get(pick_id)
        if pos is None:
            result["reason"] = "no open position for pick_id"
            return result
        if close_reason not in ("mirrored_exit", "max_hold"):
            result["reason"] = f"unknown close_reason {close_reason}"
            return result

        # Exits run in wind-down (expiry / daily-loss) — the posture gate
        # still binds, and flat+expired refuses everything.
        ok, why = self.check_posture()
        if not ok and not self.dry_run:
            result["reason"] = f"refused: {why}"
            log.warning("exit refused (%s): %s", pick_id, why)
            return result

        # F4: the nonce/pending anomaly halts ALL signing — exits included.
        ok, why = self.check_nonce_anomaly()
        if not ok:
            result["reason"] = f"halted: {why}"
            log.warning("exit signing halt (%s): %s", pick_id, why)
            return result

        # Price first (fail → retry next cycle, never force).
        price, reason = self._price_or_none(pos.token)
        if not price:
            result["reason"] = "price_failure_skip (retry next cycle)"
            log.warning("exit price failure (%s): %s", pick_id, reason)
            return result

        quote, qreason = self._quote(pos.token, "sell", pos.size_eth)
        if quote is None or not quote.routable or quote.amount_out <= 0:
            result["reason"] = "unroutable_skip (retry next cycle)"
            log.warning("exit unroutable (%s): %s", pick_id, qreason)
            return result
        quote_vs_median = self._quote_vs_median_bps(quote.implied_price_usd,
                                                    price)
        if abs(quote_vs_median) > PROBE_MAX_SLIPPAGE_BPS:
            result["reason"] = "unroutable_skip (retry next cycle)"
            log.warning("exit unroutable (%s): %s bps vs median", pick_id,
                        quote_vs_median)
            return result

        # Build the ordered exit STEP SEQUENCE (F1): approve on the
        # TOKEN'S contract → swap on the better venue.
        steps = self._build_exit_steps(pos, quote)
        for step in steps:
            ok, why = self.check_allowlist(step)
            if not ok:
                result["reason"] = f"allowlist: {why}"
                log.warning("exit allowlist rejection (%s): %s", pick_id, why)
                self._append_ledger({"ts": now, "pick_id": pick_id,
                                     "row": "allowlist_reject", "reason": why})
                return result
        ok, why = self.check_wallet_sanity(0.0)
        if not ok:
            result["reason"] = f"refused: {why}"
            return result

        if self.dry_run:
            result["action"] = "dry_run"
            result["reason"] = (f"unsigned {len(steps)}-step exit sequence "
                                f"built; no signing")
            return result

        # Execute the sequence; book receipt + PnL ONLY from the final
        # swap step's receipt.
        receipts, failure = self._execute_steps(pick_id, "exit", steps, now)
        if failure is not None:
            self.state.inflight_ops.pop(pick_id, None)
            self.save_state()
            result["reason"] = (f"sequence failed at step "
                                f"{failure['step']}: {failure['reason']}")
            log.error("exit sequence failed (%s): %s", pick_id,
                      result["reason"])
            return result
        self.state.inflight_ops.pop(pick_id, None)

        receipt = receipts[-1]
        gas_eth = sum(float(r.get("gas_eth", 0.0)) for r in receipts)
        gas_usd = sum(float(r.get("gas_usd", 0.0)) for r in receipts)

        # Record the close and emit the complete scorecard §6 receipt.
        pos.exit_quoted_price_usd = quote.implied_price_usd
        pos.exit_executed_price_usd = float(
            receipt.get("executed_price_usd") or 0.0)
        pos.exit_tx_hash = str(receipt.get("tx_hash", ""))
        pos.exit_block = int(receipt.get("block_number", 0))
        pos.exit_time = int(receipt.get("block_time", now))
        pos.exit_gas_eth = gas_eth
        pos.exit_gas_usd = gas_usd
        pos.close_reason = close_reason
        # F2: thread the leader's exit data (from the poller close row)
        pos.leader_exit_price_usd = float(leader_exit_price or 0.0)
        pos.leader_exit_time = int(leader_exit_time or 0)

        receipt_row = self._build_receipt(pos, now)
        self._append_ledger(receipt_row)

        # PnL accounting (realized only; open positions don't count).
        pnl_eth = receipt_row["follower_gross_eth"] - (
            pos.entry_gas_eth + pos.exit_gas_eth)
        self.state.realized_pnl_eth += pnl_eth
        day = _utc_day(now)
        self.state.day_bucket[day] = (self.state.day_bucket.get(day, 0.0)
                                      + pnl_eth)
        self.state.cumulative_gas_eth += pos.exit_gas_eth
        self.state.cumulative_gas_usd += pos.exit_gas_usd
        del self.state.open_positions[pick_id]
        self.save_state()
        result["action"] = "closed"
        result["reason"] = close_reason
        return result

    # -- multi-step operation sequencing (WO9-F1) -------------------------------

    def _build_entry_steps(self, token: str, quote: Quote) -> list:
        """The ordered step sequence for an entry. Every step is signed
        individually; the position is booked ONLY from the final swap."""
        now = int(self.now_fn())
        deadline = now + SWAP_DEADLINE_SECONDS
        if quote.venue == "uniswap_v3":
            # deposit → approve(router, ≤ wrapped) → exactInputSingle
            return [
                UnsignedTx(to=WETH, function="deposit",
                           value_eth=TRADE_SIZE_ETH,
                           amount_in_eth=TRADE_SIZE_ETH, token=token,
                           side="buy", deadline_ts=deadline),
                UnsignedTx(to=WETH, function="approve", value_eth=0.0,
                           amount_in_eth=TRADE_SIZE_ETH, token=token,
                           side="buy", deadline_ts=deadline,
                           spender=UNISWAP_ROUTER02,
                           approve_amount=TRADE_SIZE_ETH,
                           position_token_amount=TRADE_SIZE_ETH),
                UnsignedTx(to=UNISWAP_ROUTER02, function="exactInputSingle",
                           value_eth=0.0, amount_in_eth=TRADE_SIZE_ETH,
                           token=token, side="buy", deadline_ts=deadline),
            ]
        # Aerodrome: ETH in directly — single step.
        return [
            UnsignedTx(to=AERODROME_ROUTER,
                       function="swapExactETHForTokens",
                       value_eth=TRADE_SIZE_ETH,
                       amount_in_eth=TRADE_SIZE_ETH, token=token,
                       side="buy", deadline_ts=deadline),
        ]

    def _build_exit_steps(self, pos: LanePosition, quote: Quote) -> list:
        """The ordered step sequence for an exit: approve ON THE TOKEN'S
        CONTRACT → swap on the better exit venue. The receipt + PnL are
        booked ONLY from the final swap."""
        now = int(self.now_fn())
        deadline = now + SWAP_DEADLINE_SECONDS
        router = (AERODROME_ROUTER if quote.venue == "aerodrome"
                  else UNISWAP_ROUTER02)
        swap_fn = ("swapExactTokensForETH" if quote.venue == "aerodrome"
                   else "exactInputSingle")
        return [
            # approve ON THE TOKEN'S CONTRACT — not WETH. Bounded ≤ the
            # position's token amount. amount_in_eth is the ETH-notional
            # (≤ C/50); the approve bound is in TOKEN units.
            UnsignedTx(to=pos.token, function="approve", value_eth=0.0,
                       amount_in_eth=pos.size_eth, token=pos.token,
                       side="sell", deadline_ts=deadline, spender=router,
                       approve_amount=pos.token_amount,
                       position_token_amount=pos.token_amount),
            UnsignedTx(to=router, function=swap_fn, value_eth=0.0,
                       amount_in_eth=pos.size_eth, token=pos.token,
                       side="sell", deadline_ts=deadline,
                       position_token_amount=pos.token_amount),
        ]

    def _execute_steps(self, pick_id: str, kind: str, steps: list,
                       now: int) -> tuple:
        """Sign + submit each step in order. Returns
        (receipts, failure) where failure is None on success or a dict
        describing the failure with the step reached.

        Every step: allowlist-checked, cap-checked, decrypt-at-use,
        signed, zeroized — individually. State is persisted before each
        step so a crash mid-sequence is recoverable on restart. A failure
        at any step books NOTHING (no position, no receipt): the operation
        is ledger-logged as failed with the step reached, and the
        in-flight record is cleared only when the caller decides.
        """
        receipts = []
        op = {"kind": kind,
              "steps": [{"to": s.to, "function": s.function,
                         "value_eth": s.value_eth,
                         "amount_in_eth": s.amount_in_eth} for s in steps],
              "steps_completed": 0, "started_at": now}
        self.state.inflight_ops[pick_id] = op
        self.save_state()
        for i, step in enumerate(steps):
            ok, why = self.check_allowlist(step)
            if not ok:
                failure = {"step": i, "reason": f"allowlist: {why}",
                           "steps_completed": i}
                self._append_ledger({"ts": now, "pick_id": pick_id,
                                     "row": f"{kind}_sequence_failed",
                                     **failure})
                return receipts, failure
            ok, why = self.check_wallet_sanity(
                step.value_eth if step.value_eth else 0.0)
            if not ok:
                failure = {"step": i, "reason": f"wallet: {why}",
                           "steps_completed": i}
                self._append_ledger({"ts": now, "pick_id": pick_id,
                                     "row": f"{kind}_sequence_failed",
                                     **failure})
                return receipts, failure
            try:
                signed = self._sign(step)
                receipt = self.submit_fn(signed)
            except Exception as exc:  # noqa: BLE001 — never a blind sign
                failure = {"step": i, "reason": f"submit: {exc}",
                           "steps_completed": i}
                self._append_ledger({"ts": now, "pick_id": pick_id,
                                     "row": f"{kind}_sequence_failed",
                                     **failure})
                return receipts, failure
            receipts.append(receipt)
            op["steps_completed"] = i + 1
            self.save_state()
        return receipts, None

    def _recover_inflight(self, now: int) -> list:
        """On restart: any in-flight sequence FAILS CLOSED — it is
        ledger-logged as interrupted (with steps completed) and cleared.
        No automatic resume (resuming would need on-chain verification of
        which steps actually landed — pre-arm work, out of WO9's scope);
        a cleared record means the pick is retried cleanly next cycle and
        nothing is double-booked. Returns the interrupted pick_ids."""
        recovered = []
        for pick_id, op in list(self.state.inflight_ops.items()):
            self._append_ledger({
                "ts": now, "pick_id": pick_id,
                "row": "inflight_interrupted",
                "kind": op.get("kind"), "steps_completed":
                op.get("steps_completed", 0),
                "total_steps": len(op.get("steps", [])),
                "reason": "restart mid-sequence; failed closed, "
                          "pick will be retried cleanly",
            })
            # A pick interrupted mid-entry must be retryable: drop it from
            # processed so the next cycle re-runs the whole sequence.
            if pick_id in self.state.processed_pick_ids:
                self.state.processed_pick_ids.remove(pick_id)
            del self.state.inflight_ops[pick_id]
            recovered.append(pick_id)
        if recovered:
            self.save_state()
        return recovered

    # -- receipts (scorecard §6) -------------------------------------------------

    def _build_receipt(self, pos: LanePosition, now: int) -> dict:
        """The complete scorecard §6 receipt. Written once, never edited."""
        slip_in = self._slippage_bps(pos.entry_quoted_price_usd,
                                     pos.entry_executed_price_usd)
        # For sells, a LOWER executed price is adverse.
        slip_out = -self._slippage_bps(pos.exit_quoted_price_usd,
                                       pos.exit_executed_price_usd)
        slip_combined = slip_in + slip_out
        gas_usd_total = pos.entry_gas_usd + pos.exit_gas_usd
        gas_eth_total = pos.entry_gas_eth + pos.exit_gas_eth

        leader_gross = ((pos.leader_exit_price_usd
                         - pos.leader_entry_price_usd)
                        / pos.leader_entry_price_usd
                        if pos.leader_entry_price_usd else 0.0)
        # follower gross (pre-gas), in ETH terms
        follower_gross_eth = (pos.size_eth
                              * (pos.exit_executed_price_usd
                                 / pos.entry_executed_price_usd - 1.0)
                              if pos.entry_executed_price_usd else 0.0)
        follower_gross = (follower_gross_eth / pos.size_eth
                          if pos.size_eth else 0.0)
        follower_net = ((follower_gross_eth - gas_eth_total) / pos.size_eth
                        if pos.size_eth else 0.0)

        fill_ratio = (pos.entry_executed_price_usd
                      / pos.leader_entry_price_usd
                      if pos.leader_entry_price_usd else 0.0)
        trade_ratio = (follower_gross / leader_gross
                       if leader_gross else 0.0)

        return {
            "row": "receipt",
            "pick_id": pos.pick_id,
            "leader_wallet": pos.leader_wallet,
            "token": pos.token,
            "leader_entry_price": pos.leader_entry_price_usd,
            "leader_entry_time": pos.leader_entry_time,
            "leader_exit_price": pos.leader_exit_price_usd,
            "leader_exit_time": pos.leader_exit_time,
            "follower_entry_tx_hash": pos.entry_tx_hash,
            "follower_entry_block": pos.entry_block,
            "follower_entry_time": pos.entry_time,
            "follower_exit_tx_hash": pos.exit_tx_hash,
            "follower_exit_block": pos.exit_block,
            "follower_exit_time": pos.exit_time,
            "quoted_price_in": pos.entry_quoted_price_usd,
            "executed_price_in": pos.entry_executed_price_usd,
            "quoted_price_out": pos.exit_quoted_price_usd,
            "executed_price_out": pos.exit_executed_price_usd,
            "slippage_bps_in": round(slip_in, 3),
            "slippage_bps_out": round(slip_out, 3),
            "slippage_bps_combined": round(slip_combined, 3),
            "gas_usd_in": pos.entry_gas_usd,
            "gas_usd_out": pos.exit_gas_usd,
            "gas_usd_total": gas_usd_total,
            "delay_sec_in": pos.entry_time - pos.leader_entry_time,
            "delay_sec_out": pos.exit_time - pos.leader_exit_time,
            "size_eth": pos.size_eth,
            "leader_gross": round(leader_gross, 6),
            "follower_gross": round(follower_gross, 6),
            "follower_gross_eth": follower_gross_eth,
            "follower_net": round(follower_net, 6),
            "fill_replication_ratio": round(fill_ratio, 6),
            "trade_replication_ratio": round(trade_ratio, 6),
            "close_reason": pos.close_reason,
            "recorded_at": now,
        }

    # -- cycle driver -------------------------------------------------------------

    def run_cycle(self, now: int = None) -> dict:
        """One cycle: consume poller outputs read-only, act, persist."""
        now = int(now if now is not None else self.now_fn())
        summary = {"entries": [], "exits": [], "refusals": [],
                   "recovered": []}

        # Restart recovery (F1): any in-flight sequence from a prior run
        # is ledger-logged as interrupted and failed closed BEFORE any
        # new action — never double-booked, never orphaned silently.
        summary["recovered"] = self._recover_inflight(now)

        if not self.dry_run:
            ok, why = self.check_posture()
            if not ok:
                # Live mode + disarmed: refuse BEFORE any signing attempt.
                log.warning("cycle refused: %s", why)
                summary["refusals"].append(why)
                return summary

        # 1. New picks → entries.
        for pick_id, pos in self._read_poller_open_positions().items():
            if pick_id in self.state.processed_pick_ids:
                continue
            signal = dict(pos)
            signal["pick_id"] = pick_id
            r = self.process_entry(signal, now)
            (summary["entries"] if r["action"] in ("entered", "dry_run")
             else summary["refusals"]).append(r)

        # 2. mirrored_exit close rows → exits (F2: thread leader exit
        # price/time from the close row; F3: unmatched rows emitted here;
        # F3b: handled close-row keys deduped — the journal is re-read in
        # full every cycle, so a row already handled is skipped entirely:
        # no exit attempt, no ledger row, no false duplicate unmatched).
        lane_picks = set(self.state.open_positions)
        for row in self._read_poller_closes():
            pid = str(row.get("pick_id", ""))
            if row.get("close_reason") != "mirrored_exit":
                continue
            # F3b handled-key: pick_id + close_ts (fallback close_price).
            close_ts = row.get("close_ts")
            close_price = row.get("close_price")
            key = (f"{pid}|{close_ts}" if close_ts not in (None, "")
                   else f"{pid}|{close_price}")
            if key in self.state.handled_close_keys:
                continue  # already handled on a prior cycle
            if pid in lane_picks:
                r = self.process_exit(
                    pid, "mirrored_exit", now,
                    leader_exit_price=float(close_price or 0.0),
                    leader_exit_time=int(close_ts or 0))
                # F3b AMENDMENT: record the handled key ONLY when the exit
                # actually CLOSED. A transient skip (price failure,
                # unroutable, nonce halt — all "retry next cycle" by
                # design) must leave the row RETRYABLE: recording on
                # sight would silently drop the exit and ride the
                # position to max_hold — the WO6 silent-drop defect
                # reappearing. The unmatched branch records at emission
                # (emission IS the handling); the matched branch records
                # on success only.
                if r["action"] == "closed":
                    self.state.handled_close_keys.append(key)
                summary["exits"].append(r)
            else:
                # F3: leader exit with no matched buy → unmatched row.
                self._append_ledger({
                    "ts": now, "pick_id": pid, "row": "unmatched",
                    "reason": "leader mirrored_exit with no lane position",
                    "leader_exit_price": float(close_price or 0.0),
                })
                self.state.handled_close_keys.append(key)
                summary["exits"].append(
                    {"pick_id": pid, "action": "skip",
                     "reason": "unmatched (no lane position)"})

        # 3. max-hold: 7 days from the FOLLOWER's entry.
        for pid, pos in list(self.state.open_positions.items()):
            if pos.entry_time and (now - pos.entry_time) >= (
                    MAX_HOLD_DAYS * 86400):
                r = self.process_exit(pid, "max_hold", now)
                summary["exits"].append(r)

        self.save_state()
        return summary

    def start(self) -> None:
        """Live-mode startup: posture + key verification, or refuse."""
        ok, why = self.check_posture()
        if not ok:
            raise ExecutorError(
                f"posture disarmed at startup: {why} — refusing to run live")
        self.verify_startup_key()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO)
    p = argparse.ArgumentParser(description="WO9 probe executor (build only)")
    p.add_argument("--dry-run", action="store_true",
                   help="walk signals end to end without signing")
    p.add_argument("--state", default=DEFAULT_STATE_PATH)
    p.add_argument("--ledger", default=DEFAULT_LEDGER_PATH)
    p.add_argument("--poller-state", default="")
    p.add_argument("--poller-closes", default="")
    args = p.parse_args(argv)

    ex = ProbeExecutor(state_path=args.state, ledger_path=args.ledger,
                       poller_state_path=args.poller_state,
                       poller_closes_path=args.poller_closes,
                       dry_run=args.dry_run)
    if args.dry_run:
        log.info("DRY-RUN mode: posture reported, not enforced; no signing")
        summary = ex.run_cycle()
        print(json.dumps(summary, indent=2))
        return 0

    try:
        ex.start()
    except ExecutorError as e:
        print(f"FAIL-CLOSED: {e}", file=sys.stderr)
        return 1
    summary = ex.run_cycle()
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
