#!/usr/bin/env python3
"""
Paper copy-trader poller.

Watches approved wallets on Base, detects DEX swaps, mirrors them as paper
positions under a fixed cost model. Paper-only: no keys, no signing, no live
trading. Reads chain data, writes paper records.

Usage:
    python -m copy_trader.poller --config watched-wallets.yaml --once
    python -m copy_trader.poller --config watched-wallets.yaml --daemon
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import requests
import yaml

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Known DEX routers on Base (extendable — not hard-coded to only these)
KNOWN_ROUTERS = {
    "0xcf77a3ba9a5ca399b7c97c74d54e5b1beb874e43",  # Aerodrome Router
    "0x2626664c2603336e57b271c5c0b26f421741e481",  # Uniswap SwapRouter02
    "0x1111111254eeb25477b68fb85ed929f73a960582",  # 1inch Router
    "0xdef1c0ded9bec7f1a1670819833240f027b25eff",  # 0x Exchange Proxy
    "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad",  # Uniswap Universal Router
    "0xd8ba9d1a99fc21f0eca24e9b85737c28a194a4e2",  # GMGN Fee Adapter Proxy
}

# Stablecoins — stable-to-stable moves are ignored.
# Addresses verified on-chain (eth_getCode non-empty, symbol() matches).
STABLECOINS = {
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
    "0x50c5725949a6f0c72e6c4a641f24049a917db0cb",  # DAI
    "0x820c137fa70c8691f0e44dc420a5e53c168921dc",  # USDS
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",  # USDbC
    "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2",  # USDT
}

# Per-token decimals, read from chain (decimals() on Base mainnet).
# Do NOT infer these from set membership: DAI and USDS are 18, the other
# three are 6. A single assumed default misprices the odd ones out by 1e12.
TOKEN_DECIMALS = {
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": 6,   # USDC
    "0x50c5725949a6f0c72e6c4a641f24049a917db0cb": 18,  # DAI
    "0x820c137fa70c8691f0e44dc420a5e53c168921dc": 18,  # USDS
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca": 6,   # USDbC
    "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2": 6,   # USDT
}
DEFAULT_TOKEN_DECIMALS = 18  # non-stable ERC20 default

# WETH on Base
WETH = "0x4200000000000000000000000000000000000006"

# Cost model: 900bps round-trip (matches Copy Path Ledger bar)
ROUNDTRIP_COST_BPS = 900.0

# ---------------------------------------------------------------------------
# Price-fetch failure semantics
# ---------------------------------------------------------------------------
# Every failure mode gets its own reason so callers can tell "retryable
# transport fault" from "this token has no priced pool". A bare None cannot
# express that difference, and a fallback chain keyed on None is decorative:
# it either never fires or fires pointlessly.
PRICE_OK = "ok"
PRICE_HTTP_429 = "http_429"
PRICE_HTTP_5XX = "http_5xx"
PRICE_NO_POOL = "no_pool"          # 404, or price_usd empty — genuinely unpriced
PRICE_PARSE_ERROR = "parse_error"
PRICE_NETWORK = "network"          # timeout / connection error

# Reasons where trying a second source is meaningful. no_pool is NOT here:
# if GeckoTerminal has no pool, GMGN has no pool either.
PRICE_RETRYABLE = frozenset({PRICE_HTTP_429, PRICE_HTTP_5XX, PRICE_NETWORK,
                             PRICE_PARSE_ERROR})

# Bounded retry: converts most of an observed 56% first-pass 429 rate into
# success, and caps total stall so a throttled window cannot hang a poll cycle.
PRICE_FETCH_MAX_ATTEMPTS = 3
PRICE_FETCH_BACKOFF_BASE_S = 1.0


@dataclass
class PriceResult:
    """A price fetch outcome. `price is None` means exactly one thing:
    there is no usable price, and `reason` says why."""
    price: float | None = None
    reason: str = PRICE_OK
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.price is not None and self.reason == PRICE_OK

    @property
    def retryable(self) -> bool:
        """True when a second source might plausibly succeed."""
        return self.reason in PRICE_RETRYABLE

# Default paper notional per position
DEFAULT_PAPER_NOTIONAL_USD = 25.0

# Max hold time
MAX_HOLD_DAYS = 7

# Blockscout API
BLOCKSCOUT_API = "https://base.blockscout.com/api/v2/addresses"
BLOCKSCOUT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# Base RPC for receipts
BASE_RPC = "https://mainnet.base.org"

# Transfer event keccak256 signature
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# How long a seen tx hash is retained before pruning.
#
# This must exceed the OLDEST tx the API still returns, or a pruned hash is
# returned again by the next fetch and re-ingested as if it were new. That
# window is not a fixed constant: the same API served 15.6 days for one wallet
# and 49.7 days for another in a single cycle. A retention shorter than the
# real window silently resurrects history (observed: 19 txs pruned at 30d were
# still fetchable, and were re-detected on the next cycle).
#
# So retention is the max of a floor and (observed window * margin), computed
# from the txs actually seen. The floor guards the first run, before any
# window has been measured.
SEEN_TX_RETENTION_FLOOR_DAYS = 120
SEEN_TX_RETENTION_MARGIN = 1.5
# Kept for backwards-compatible imports.
SEEN_TX_TTL_DAYS = SEEN_TX_RETENTION_FLOOR_DAYS
SEEN_TX_RETENTION_SECONDS = SEEN_TX_RETENTION_FLOOR_DAYS * 86400
SEEN_TX_TTL_SECONDS = SEEN_TX_RETENTION_SECONDS
# The API's advertised history depth; the observed window is measured at runtime.
BLOCKSCOUT_HISTORY_DAYS = 15

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class SwapEvent:
    """A detected DEX swap from a watched wallet."""
    tx_hash: str
    wallet_address: str
    wallet_label: str
    token_in: str
    token_out: str
    amount_in: float
    amount_out: float
    timestamp: int  # unix seconds
    is_buy: bool  # True = wallet spent ETH/stable, received token

@dataclass
class PaperPosition:
    """An open paper position mirroring a wallet's buy."""
    pick_id: str  # wallet + tx_hash
    wallet_address: str
    wallet_label: str
    token: str  # the token being held
    entry_price: float  # USD per token at entry
    paper_notional_usd: float
    quantity: float  # token quantity
    cost_bps: float
    open_time: str  # ISO
    open_tx_hash: str
    source_amount_in: float  # wallet's own trade size
    source_token_in: str

@dataclass
class CloseRecord:
    """A completed paper position close."""
    pick_id: str
    wallet_address: str
    wallet_label: str
    token: str
    open_time: str
    close_time: str
    entry_price: float
    close_price: float
    cost_bps: float
    close_reason: str  # mirrored_exit | max_hold
    net_pnl_pct: float
    paper_notional_usd: float
    quantity: float
    gross_pnl_pct: float

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

@dataclass
class PollerState:
    """Persistent state — saved to JSON after each poll cycle."""
    # tx_hash -> unix timestamp of the tx (0 when unknown). A dict, not a set,
    # so entries can be pruned by age.
    seen_tx_hashes: dict = field(default_factory=dict)
    open_positions: dict = field(default_factory=dict)  # pick_id -> PaperPosition
    wallet_trade_counts: dict = field(default_factory=lambda: defaultdict(int))
    wallet_net_pnl: dict = field(default_factory=lambda: defaultdict(float))
    wallet_rotated_out: set = field(default_factory=set)
    last_poll_timestamp: int = 0

    def to_dict(self) -> dict:
        return {
            "seen_tx_hashes": dict(self.seen_tx_hashes),
            "open_positions": {k: asdict(v) for k, v in self.open_positions.items()},
            "wallet_trade_counts": dict(self.wallet_trade_counts),
            "wallet_net_pnl": dict(self.wallet_net_pnl),
            "wallet_rotated_out": list(self.wallet_rotated_out),
            "last_poll_timestamp": self.last_poll_timestamp,
        }

    @classmethod
    def from_dict(cls, d: dict) -> PollerState:
        state = cls()
        raw_seen = d.get("seen_tx_hashes", {})
        if isinstance(raw_seen, list):
            # Legacy schema (bare set of hashes) — timestamps unknown.
            state.seen_tx_hashes = {str(h): 0 for h in raw_seen}
        else:
            state.seen_tx_hashes = {str(h): int(ts) for h, ts in raw_seen.items()}
        state.open_positions = {
            k: PaperPosition(**v) for k, v in d.get("open_positions", {}).items()
        }
        state.wallet_trade_counts = defaultdict(int, d.get("wallet_trade_counts", {}))
        state.wallet_net_pnl = defaultdict(float, d.get("wallet_net_pnl", {}))
        state.wallet_rotated_out = set(d.get("wallet_rotated_out", []))
        state.last_poll_timestamp = d.get("last_poll_timestamp", 0)
        return state


def _parse_timestamp(ts: Any) -> int:
    """Safely parse a timestamp string into unix seconds, returning 0 on failure."""
    if not ts:
        return 0
    try:
        return int(datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        try:
            return int(float(ts))
        except (ValueError, TypeError):
            return 0

# ---------------------------------------------------------------------------
# Chain data fetchers
# ---------------------------------------------------------------------------

def fetch_wallet_transactions(address: str) -> list[dict]:
    """Fetch recent transactions for a wallet from Blockscout."""
    url = f"{BLOCKSCOUT_API}/{address}/transactions"
    headers = {"User-Agent": BLOCKSCOUT_UA, "Accept": "application/json"}
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data.get("items", [])

def fetch_tx_receipt(tx_hash: str) -> dict | None:
    """Fetch transaction receipt from Base RPC to get Transfer events."""
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_getTransactionReceipt",
        "params": [tx_hash],
        "id": 1,
    }
    headers = {"Content-Type": "application/json"}
    resp = requests.post(BASE_RPC, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    result = resp.json().get("result")
    if not result or result.get("status") != "0x1":
        return None
    return result

def fetch_token_price_result(token_address: str) -> PriceResult:
    """Fetch current token price in USD from GeckoTerminal, with a typed reason.

    Returns a PriceResult so callers can distinguish "no pool exists" from
    "the request failed" — the distinction a fallback chain depends on.

    NOTE: fetch_token_price() below is the legacy bare-None wrapper kept for
    call sites that have not been migrated. It collapses every reason to None
    and must not be used to decide whether to try another source.
    """
    url = f"https://api.geckoterminal.com/api/v2/networks/base/tokens/{token_address}"
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

    last_exc: str | None = None
    for attempt in range(PRICE_FETCH_MAX_ATTEMPTS):
        try:
            resp = requests.get(url, headers=headers, timeout=15)
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = f"{type(exc).__name__}: {exc}"
            reason = PRICE_NETWORK
        except Exception as exc:  # noqa: BLE001 - classify, never swallow
            last_exc = f"{type(exc).__name__}: {exc}"
            reason = PRICE_NETWORK
        else:
            code = getattr(resp, "status_code", None)
            if code == 429:
                last_exc = "HTTP 429"
                reason = PRICE_HTTP_429
            elif code is not None and 500 <= code < 600:
                last_exc = f"HTTP {code}"
                reason = PRICE_HTTP_5XX
            else:
                try:
                    resp.raise_for_status()
                except requests.HTTPError as exc:
                    code = getattr(resp, "status_code", None) or 0
                    # 404 is a genuine "no such token/pool", not a transport fault.
                    reason = PRICE_NO_POOL if code == 404 else PRICE_HTTP_5XX
                    return PriceResult(reason=reason, detail=str(exc)[:200])
                except Exception as exc:  # noqa: BLE001
                    reason = PRICE_HTTP_5XX
                    return PriceResult(reason=reason, detail=str(exc)[:200])
                else:
                    try:
                        data = resp.json()
                        price = data.get("data", {}).get("attributes", {}).get("price_usd")
                    except Exception as exc:  # noqa: BLE001
                        return PriceResult(reason=PRICE_PARSE_ERROR,
                                           detail=str(exc)[:200])
                    if not price:
                        # Token exists but carries no price — no priced pool.
                        return PriceResult(reason=PRICE_NO_POOL,
                                           detail="price_usd empty/absent")
                    try:
                        return PriceResult(price=float(price), reason=PRICE_OK)
                    except (TypeError, ValueError) as exc:
                        return PriceResult(reason=PRICE_PARSE_ERROR,
                                           detail=str(exc)[:200])

        # Retryable failure — back off before the next attempt.
        if attempt < PRICE_FETCH_MAX_ATTEMPTS - 1:
            time.sleep(PRICE_FETCH_BACKOFF_BASE_S * (2 ** attempt)
                        + (PRICE_FETCH_BACKOFF_BASE_S * 0.25))

    return PriceResult(reason=reason, detail=last_exc or "unknown")


def fetch_token_price(token_address: str) -> float | None:
    """Legacy wrapper: price in USD, or None for ANY failure.

    Deprecated for failure-handling decisions — use fetch_token_price_result().
    Retained so existing callers keep working unchanged.
    """
    return fetch_token_price_result(token_address).price

def fetch_eth_balance_wei(address: str, block_number: int) -> int | None:
    """ETH balance (wei) of an address at a given block, via Base RPC."""
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_getBalance",
        "params": [address, hex(block_number)],
        "id": 1,
    }
    try:
        resp = requests.post(BASE_RPC, json=payload,
                             headers={"Content-Type": "application/json"}, timeout=30)
        resp.raise_for_status()
        result = resp.json().get("result")
        return int(result, 16) if result and result != "0x" else None
    except Exception:
        return None

def eth_leg_wei(
    wallet_address: str,
    receipt: dict,
    sibling_count: int = 0,
) -> int | None:
    """
    Measure the wallet's net ETH movement in the tx's block, net of gas.

    The wallet's ETH leg of a native-quoted swap leaves no ERC20 Transfer log,
    so it is measured from the balance delta across the block. The delta is
    signed: positive = ETH received (a sell), negative = ETH spent (a buy).

    Gas is EXCLUDED from the measurement, because gas is not part of the trade.
    This tx's own gas is deducted from the delta, so it is added back:
    `trade_leg = delta + own_gas`. The trade leg itself carries the sign.

    FAILS CLOSED when the wallet has more than one tx in the block. The delta
    then contains BOTH trades' legs and cannot be attributed to either one —
    gas correction does not split it, it only removes fees. Attributing the
    whole delta to one swap would silently inflate amount_in, corrupting the
    exact field this measurement exists to make honest.

    `sibling_count` is the number of the wallet's OTHER txs in this block
    (0 when none). It is a COUNT, not a gas total: a count cannot be zeroed by
    a failed receipt fetch, whereas a gas sum can, which would silently re-open
    the multi-tx hole.

    Returns the trade leg in wei, or None when unattributable/zero/unreadable.
    """
    if sibling_count > 0:
        return None  # multiple wallet txs in block, delta unattributable

    block_hex = receipt.get("blockNumber")
    if not block_hex:
        return None
    block_number = int(block_hex, 16)

    before = fetch_eth_balance_wei(wallet_address, block_number - 1)
    after = fetch_eth_balance_wei(wallet_address, block_number)
    if before is None or after is None:
        return None

    delta = after - before

    gas_used = int(receipt.get("gasUsed", "0x0"), 16)
    gas_price = int(receipt.get("effectiveGasPrice", "0x0"), 16)
    own_gas = gas_used * gas_price

    # Add this tx's own gas back: trade_leg = delta + own_gas
    trade_leg = delta + own_gas

    if trade_leg == 0:
        return None
    return trade_leg

# ---------------------------------------------------------------------------
# Swap detection
# ---------------------------------------------------------------------------

def is_dex_swap(tx: dict) -> bool:
    """Check if a transaction is a DEX swap by looking at the 'to' address
    and transaction type. A swap has token_transfer in transaction_types and
    the 'to' is a known router or contract."""
    to_field = tx.get("to") or {}
    if isinstance(to_field, dict):
        to_addr = (to_field.get("hash") or "").lower()
        is_contract = to_field.get("is_contract", False)
    else:
        to_addr = str(to_field).lower()
        is_contract = False
    if to_addr in KNOWN_ROUTERS:
        return True
    # Fallback: if tx has token_transfer type and 'to' is a contract, it's likely a swap
    tx_types = tx.get("transaction_types", [])
    if "token_transfer" in tx_types and is_contract:
        return True
    return False

def net_flow_by_token(receipt: dict, wallet_address: str) -> dict[str, int]:
    """
    Net flow per token for the wallet in this tx: received - sent.

    Only tokens the wallet actually moved appear, and only with non-zero net.
    A token the wallet both sent and received (net zero) drops out — that is
    pass-through, not a leg.
    """
    wallet_lower = wallet_address.lower()
    net: dict[str, int] = defaultdict(int)

    for log in receipt.get("logs", []):
        topics = log.get("topics", [])
        if not topics or topics[0].lower() != TRANSFER_TOPIC:
            continue
        if len(topics) < 3:
            continue
        from_addr = "0x" + topics[1][-40:].lower()
        to_addr = "0x" + topics[2][-40:].lower()
        if from_addr != wallet_lower and to_addr != wallet_lower:
            continue
        data = log.get("data", "0x0")
        try:
            value = int(data, 16) if data not in ("0x", "") else 0
        except ValueError:
            continue
        token_addr = (log.get("address") or "").lower()
        if not token_addr:
            continue
        if to_addr == wallet_lower:
            net[token_addr] += value
        if from_addr == wallet_lower:
            net[token_addr] -= value

    return {tok: v for tok, v in net.items() if v != 0}


def extract_swap_from_receipt(
    receipt: dict,
    wallet_address: str,
    sibling_count: int = 0,
) -> tuple[SwapEvent | None, str]:
    """
    Extract a swap from a tx receipt using net flow per token.

    Rule:
      * Net = received - sent per token, for tokens involving the wallet.
      * Input  = the token with the most negative net.
      * Output = the token with the most positive net.
      * is_buy = the output is not a stablecoin and not WETH.

    Fail closed. A swap is returned only when the flow is unambiguous. Anything
    else returns (None, reason) and opens no position.

    A native-ETH leg leaves no ERC20 Transfer log, so a one-legged flow is
    expected for native-quoted swaps. In that case the ETH side is MEASURED
    from the wallet's balance delta across the tx's block (see eth_leg_wei),
    never assumed. If that measurement is unavailable the swap is skipped —
    there is no fabricated "ETH" fallback.

    Returns (SwapEvent | None, reason_string).
    """
    net = net_flow_by_token(receipt, wallet_address)

    negatives = {t: v for t, v in net.items() if v < 0}
    positives = {t: v for t, v in net.items() if v > 0}

    if not net:
        return None, "no token flow involving wallet"

    # --- genuine ambiguity: 2+ tokens on a side. Fail closed.
    if len(negatives) > 1:
        return None, f"ambiguous input: {len(negatives)} tokens sent ({sorted(negatives)})"
    if len(positives) > 1:
        return None, f"ambiguous output: {len(positives)} tokens received ({sorted(positives)})"
    if len(negatives) == 1 and len(positives) == 1:
        # Both sides are ERC20 — normal wrapped-token swap.
        token_in = next(iter(negatives))
        token_out = next(iter(positives))
        amount_in_raw = -negatives[token_in]
        amount_out_raw = positives[token_out]
        is_buy = token_out not in STABLECOINS and token_out != WETH
        eth_note = ""
    elif len(negatives) == 1 and not positives:
        # Wallet only SENT an ERC20 → it received the ETH leg (a sell).
        token_in = next(iter(negatives))
        amount_in_raw = -negatives[token_in]
        token_out = "ETH"
        leg = eth_leg_wei(wallet_address, receipt, sibling_count)
        if leg is None:
            return None, "one ERC20 leg sent, but ETH delta unreadable or zero"
        if leg <= 0:
            return None, f"one ERC20 leg sent, but ETH delta sign wrong ({leg} wei)"
        amount_out_raw = leg
        is_buy = False
        eth_note = f"ETH leg measured from block delta: {leg} wei"
    elif len(positives) == 1 and not negatives:
        # Wallet only RECEIVED an ERC20 → it spent the ETH leg (a buy).
        token_out = next(iter(positives))
        amount_out_raw = positives[token_out]
        token_in = "ETH"
        leg = eth_leg_wei(wallet_address, receipt, sibling_count)
        if leg is None:
            return None, "one ERC20 leg received, but ETH delta unreadable or zero"
        if leg >= 0:
            return None, f"one ERC20 leg received, but ETH delta sign wrong ({leg} wei)"
        amount_in_raw = -leg
        is_buy = token_out not in STABLECOINS and token_out != WETH
        eth_note = f"ETH leg measured from block delta: {leg} wei"
    else:
        return None, f"unhandled flow shape (neg={len(negatives)}, pos={len(positives)})"

    # Sanity: a leg of zero is not a trade.
    if amount_in_raw <= 0 or amount_out_raw <= 0:
        return None, f"non-positive leg (in={amount_in_raw}, out={amount_out_raw})"

    # A stable-to-stable move is not a position-worthy swap.
    if token_in in STABLECOINS and token_out in STABLECOINS:
        return None, "stable-to-stable move"

    decimals_in = TOKEN_DECIMALS.get(token_in, DEFAULT_TOKEN_DECIMALS)
    decimals_out = TOKEN_DECIMALS.get(token_out, DEFAULT_TOKEN_DECIMALS)

    swap = SwapEvent(
        tx_hash=receipt.get("transactionHash", ""),
        wallet_address=wallet_address,
        wallet_label="",  # filled by caller
        token_in=token_in,
        token_out=token_out,
        amount_in=amount_in_raw / (10 ** decimals_in),
        amount_out=amount_out_raw / (10 ** decimals_out),
        timestamp=0,  # filled by caller from tx data
        is_buy=is_buy,
    )
    return swap, ("ok" if not eth_note else f"ok; {eth_note}")

# ---------------------------------------------------------------------------
# Paper position management
# ---------------------------------------------------------------------------

def open_paper_position(swap: SwapEvent, paper_notional_usd: float) -> PaperPosition | None:
    """Open a paper position mirroring a detected buy."""
    if not swap.is_buy:
        return None

    # Get current token price for entry
    price = fetch_token_price(swap.token_out)
    if not price or price <= 0:
        return None

    quantity = paper_notional_usd / price
    cost_bps = ROUNDTRIP_COST_BPS

    return PaperPosition(
        pick_id=f"{swap.wallet_address}:{swap.tx_hash}",
        wallet_address=swap.wallet_address,
        wallet_label=swap.wallet_label,
        token=swap.token_out,
        entry_price=price,
        paper_notional_usd=paper_notional_usd,
        quantity=quantity,
        cost_bps=cost_bps,
        # open_time is the TX timestamp, never wall-clock. Wall-clock here made
        # a historical buy look brand new and let it be closed seconds later by
        # an unrelated sell that was actually older.
        open_time=datetime.fromtimestamp(swap.timestamp, tz=timezone.utc).isoformat(),
        open_tx_hash=swap.tx_hash,
        source_amount_in=swap.amount_in,
        source_token_in=swap.token_in,
    )

def close_paper_position(
    position: PaperPosition,
    close_price: float,
    close_reason: str,
    close_timestamp: int | None = None,
) -> CloseRecord:
    """Close a paper position and compute P&L."""
    gross_pnl_pct = (close_price / position.entry_price - 1.0) * 100.0
    cost_pct = position.cost_bps / 100.0
    net_pnl_pct = gross_pnl_pct - cost_pct

    # close_time is the closing tx's timestamp when known, else wall-clock.
    if close_timestamp:
        close_time = datetime.fromtimestamp(close_timestamp, tz=timezone.utc).isoformat()
    else:
        close_time = datetime.now(timezone.utc).isoformat()

    return CloseRecord(
        pick_id=position.pick_id,
        wallet_address=position.wallet_address,
        wallet_label=position.wallet_label,
        token=position.token,
        open_time=position.open_time,
        close_time=close_time,
        entry_price=position.entry_price,
        close_price=close_price,
        cost_bps=position.cost_bps,
        close_reason=close_reason,
        net_pnl_pct=net_pnl_pct,
        paper_notional_usd=position.paper_notional_usd,
        quantity=position.quantity,
        gross_pnl_pct=gross_pnl_pct,
    )

# ---------------------------------------------------------------------------
# Poller
# ---------------------------------------------------------------------------

class CopyTraderPoller:
    def __init__(self, config_path: str, state_path: str, journal_path: str):
        self.config_path = config_path
        self.state_path = state_path
        self.journal_path = journal_path
        self.config = self._load_config()
        self.state = self._load_state()
        self.paper_notional = self.config.get("paper_notional_usd", DEFAULT_PAPER_NOTIONAL_USD)
        self.poll_interval = self.config.get("poll_interval_sec", 60)
        self.rotation_enabled = self.config.get("rotation", {}).get("enabled", True)
        self.min_trades_before_cut = self.config.get("rotation", {}).get("min_trades_before_cut", 20)
        self.max_hold_seconds = MAX_HOLD_DAYS * 86400
        # Per-poll index of the wallet's txs by block, for sibling-gas lookups.
        self._block_cache: dict[int, list] = {}
        # Largest API history window observed this cycle (seconds).
        self._observed_window_seconds: int = 0

        # Setup logging
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
        )
        self.logger = logging.getLogger("copy_trader")

    def _load_config(self) -> dict:
        with open(self.config_path) as f:
            return yaml.safe_load(f)

    def _load_state(self) -> PollerState:
        if os.path.exists(self.state_path):
            with open(self.state_path) as f:
                return PollerState.from_dict(json.load(f))
        return PollerState()

    def _save_state(self):
        with open(self.state_path, "w") as f:
            json.dump(self.state.to_dict(), f, indent=2)

    def _append_journal(self, record: CloseRecord):
        """Append a close record to the journal (JSONL, append-only)."""
        os.makedirs(os.path.dirname(self.journal_path), exist_ok=True)
        with open(self.journal_path, "a") as f:
            f.write(json.dumps(asdict(record), sort_keys=True) + "\n")

    def _get_wallet_label(self, address: str) -> str:
        for w in self.config.get("wallets", []):
            if str(w["address"]).lower() == address.lower():
                return w.get("label", address)
        return address

    def _normalize_address(self, addr: Any) -> str:
        """Normalize an address that YAML may have parsed as an integer."""
        if isinstance(addr, int):
            return f"0x{addr:040x}"
        return str(addr).lower()

    def _prune_seen(self) -> int:
        """
        Drop seen tx hashes older than the retention window.

        Retention is max(floor, observed_window * margin). The observed window
        is the age of the OLDEST tx still returned by the API for any wallet
        this cycle — the retention must exceed that, or pruned hashes come back
        on the next fetch and get re-ingested as new.

        A fixed constant is not enough: the same API served a 15.6-day window
        for one wallet and 49.7 days for another in one cycle. Pruning at a
        hardcoded 30 days resurrected 19 txs that were then re-detected.
        """
        now = int(time.time())
        observed = getattr(self, "_observed_window_seconds", 0)
        retention = max(
            SEEN_TX_RETENTION_SECONDS,
            int(observed * SEEN_TX_RETENTION_MARGIN),
        )
        cutoff = now - retention
        before = len(self.state.seen_tx_hashes)
        self.state.seen_tx_hashes = {
            h: ts for h, ts in self.state.seen_tx_hashes.items()
            if ts == 0 or ts >= cutoff
        }
        dropped = before - len(self.state.seen_tx_hashes)
        if dropped:
            self.logger.info(
                f"  (retention window {retention / 86400:.1f}d "
                f"= max({SEEN_TX_RETENTION_SECONDS / 86400:.0f}d floor, "
                f"{observed / 86400:.1f}d observed x{SEEN_TX_RETENTION_MARGIN}))"
            )
        return dropped

    def _sibling_count(self, block_number: int, exclude_hash: str) -> int:
        """
        Number of the wallet's OTHER txs in the same block.

        Used to decide whether the block-level ETH delta is attributable at all.
        A COUNT (not a gas sum) so that a failed receipt fetch cannot silently
        report 0 and re-open the multi-tx attribution hole.
        """
        return sum(
            1 for tx in self._block_cache.get(block_number, [])
            if tx.get("hash") != exclude_hash
        )

    def poll_wallet(self, wallet: dict, baseline: bool = False) -> tuple[list[SwapEvent], int]:
        """
        Poll one wallet for new swaps.

        Returns (swaps, newest_tx_timestamp). When `baseline` is True this is a
        first run for this wallet: everything currently visible is marked seen
        and no swaps are returned, so a fresh poller never ingests weeks of
        history as if it were live activity.

        `baseline` is passed in rather than read from state, because state is
        global while wallets are polled in a loop — a wallet that baselined
        first would otherwise set the watermark and let the remaining wallets
        be treated as normal cycles.
        """
        address = self._normalize_address(wallet["address"])
        label = wallet.get("label", address)
        self.logger.info(f"Polling {label} ({address})...")

        try:
            txs = fetch_wallet_transactions(address)
        except Exception as e:
            self.logger.error(f"Failed to fetch txs for {label}: {e}")
            return [], self.state.last_poll_timestamp

        # Index by block so sibling tx count can be computed without refetching.
        self._block_cache = defaultdict(list)
        for tx in txs:
            try:
                blk = int(tx.get("block_number", 0))
            except (TypeError, ValueError):
                continue
            self._block_cache[blk].append(tx)

        newest_ts = 0
        oldest_ts = 0
        for tx in txs:
            ts = _parse_timestamp(tx.get("timestamp"))
            if ts > newest_ts:
                newest_ts = ts
            if ts and (oldest_ts == 0 or ts < oldest_ts):
                oldest_ts = ts

        # Track how far back the API actually serves, so pruning never drops a
        # hash that is still fetchable (which would re-ingest it as new).
        if oldest_ts:
            window = int(time.time()) - oldest_ts
            if window > getattr(self, "_observed_window_seconds", 0):
                self._observed_window_seconds = window

        # --- baseline run: mark all seen, open nothing.
        if baseline:
            for tx in txs:
                h = tx.get("hash", "")
                if h:
                    self.state.seen_tx_hashes[h] = _parse_timestamp(tx.get("timestamp"))
            self.logger.info(
                f"  BASELINE: marked {len(txs)} txs seen, newest ts {newest_ts}; "
                f"opened 0 positions"
            )
            return [], newest_ts

        swaps = []
        skipped = 0
        for tx in txs:
            tx_hash = tx.get("hash", "")
            if not tx_hash or tx_hash in self.state.seen_tx_hashes:
                continue

            self.state.seen_tx_hashes[tx_hash] = _parse_timestamp(tx.get("timestamp"))

            if not is_dex_swap(tx):
                continue

            # Fetch receipt to extract swap details
            try:
                receipt = fetch_tx_receipt(tx_hash)
            except Exception as e:
                self.logger.warning(f"  Failed to fetch receipt for {tx_hash}: {e}")
                continue

            if not receipt:
                continue

            try:
                blk = int(tx.get("block_number", 0))
            except (TypeError, ValueError):
                blk = 0
            sib_count = self._sibling_count(blk, tx_hash)

            swap, reason = extract_swap_from_receipt(receipt, address, sib_count)
            if swap:
                swap.wallet_label = label
                swap.timestamp = _parse_timestamp(tx.get("timestamp"))
                swaps.append(swap)
                self.logger.info(
                    f"  Detected {'BUY' if swap.is_buy else 'SELL'}: "
                    f"{swap.amount_in:.6f} {swap.token_in[:12]} -> "
                    f"{swap.amount_out:.6f} {swap.token_out[:12]}  [{reason}]"
                )
            else:
                skipped += 1
                self.logger.info(f"  SKIPPED {tx_hash[:18]}…: {reason}")

        if skipped:
            self.logger.info(f"  {skipped} tx(s) skipped this wallet (fail-closed)")

        return swaps, newest_ts

    def process_swaps(self, swaps: list[SwapEvent]):
        """
        Process detected swaps — open/close paper positions.

        Swaps are processed in TX TIMESTAMP ORDER, and a close only matches a
        position opened by a strictly older tx. Without this, a batch of
        historical swaps closed positions the instant they opened, recording
        -9% (pure cost) for every one of them.
        """
        for swap in sorted(swaps, key=lambda s: s.timestamp):
            if swap.is_buy:
                # Check if wallet is rotated out
                if swap.wallet_address in self.state.wallet_rotated_out:
                    self.logger.info(f"  Skipping buy from rotated-out wallet {swap.wallet_label}")
                    continue

                # Open paper position
                position = open_paper_position(swap, self.paper_notional)
                if position:
                    self.state.open_positions[position.pick_id] = position
                    self.logger.info(
                        f"  Opened paper position: {position.pick_id} "
                        f"@ ${position.entry_price:.6f} (tx ts {swap.timestamp})"
                    )
            else:
                # Sell — close the matching open position, oldest first.
                # The token being SOLD is the ERC20 the wallet spent, i.e.
                # swap.token_in. (For a native-quoted sell, token_out is "ETH",
                # which is never a held position — matching on token_out here
                # silently failed to close anything.)
                sold_token = swap.token_in
                close_pick_id = None
                close_open_ts = None
                for pick_id, pos in self.state.open_positions.items():
                    if pos.wallet_address != swap.wallet_address:
                        continue
                    if pos.token != sold_token:
                        continue
                    try:
                        pos_ts = int(datetime.fromisoformat(pos.open_time).timestamp())
                    except (ValueError, TypeError):
                        continue
                    if pos_ts >= swap.timestamp:
                        # Not strictly older — cannot be closed by this sell.
                        continue
                    if close_open_ts is None or pos_ts < close_open_ts:
                        close_open_ts = pos_ts
                        close_pick_id = pick_id

                if close_pick_id:
                    # FETCH BEFORE POP. The position must stay in
                    # open_positions until we hold a close price: a retryable
                    # failure (429/5xx/network) leaves it open to close on the
                    # next cycle. Popping first erased the position outright —
                    # no journal record, no PnL, no rotation credit, no log line.
                    close_price = fetch_token_price(sold_token)
                    if close_price:
                        position = self.state.open_positions.pop(close_pick_id)
                        record = close_paper_position(
                            position, close_price, "mirrored_exit", swap.timestamp
                        )
                        self._append_journal(record)
                        self.state.wallet_trade_counts[swap.wallet_address] += 1
                        self.state.wallet_net_pnl[swap.wallet_address] += record.net_pnl_pct
                        self.logger.info(
                            f"  Closed {close_pick_id}: net {record.net_pnl_pct:+.2f}% "
                            f"(reason: mirrored_exit)"
                        )
                        self._check_rotation(swap.wallet_address)
                else:
                    self.logger.info(
                        f"  Sell with no strictly-older open position for "
                        f"{sold_token[:12]} — no close"
                    )

    def _check_rotation(self, wallet_address: str):
        """Check if a wallet should be rotated out."""
        if not self.rotation_enabled:
            return
        if wallet_address in self.state.wallet_rotated_out:
            return
        trades = self.state.wallet_trade_counts[wallet_address]
        net_pnl = self.state.wallet_net_pnl[wallet_address]
        if trades >= self.min_trades_before_cut and net_pnl < 0:
            self.state.wallet_rotated_out.add(wallet_address)
            self.logger.warning(f"  ROTATE_OUT: {wallet_address} — {trades} trades, net PnL {net_pnl:+.2f}%")

    def check_max_hold(self):
        """Close any positions past max hold time."""
        now = datetime.now(timezone.utc)
        to_close = []
        for pick_id, pos in self.state.open_positions.items():
            open_time = datetime.fromisoformat(pos.open_time)
            if (now - open_time).total_seconds() > self.max_hold_seconds:
                to_close.append(pick_id)

        for pick_id in to_close:
            # FETCH BEFORE POP — see the mirrored-exit path above. A throttled
            # max-hold close must not erase the position; it retries next cycle.
            position = self.state.open_positions[pick_id]
            close_price = fetch_token_price(position.token)
            if close_price:
                self.state.open_positions.pop(pick_id)
                record = close_paper_position(position, close_price, "max_hold")
                self._append_journal(record)
                self.state.wallet_trade_counts[position.wallet_address] += 1
                self.state.wallet_net_pnl[position.wallet_address] += record.net_pnl_pct
                self.logger.info(f"  Closed {pick_id}: net {record.net_pnl_pct:+.2f}% (reason: max_hold)")
                self._check_rotation(position.wallet_address)

    def run_once(self):
        """Run one poll cycle."""
        self.logger.info("=== Poll cycle start ===")
        # Decide baseline ONCE, before polling any wallet. Reading the global
        # watermark inside the wallet loop let the first wallet's baseline
        # switch the remaining wallets into normal-cycle mode.
        is_baseline = not self.state.last_poll_timestamp
        if is_baseline:
            self.logger.info("  No watermark — this cycle is a BASELINE run "
                             "(all wallets: mark seen, open nothing)")

        newest_overall = self.state.last_poll_timestamp
        for wallet in self.config.get("wallets", []):
            swaps, newest_ts = self.poll_wallet(wallet, baseline=is_baseline)
            if newest_ts > newest_overall:
                newest_overall = newest_ts
            self.process_swaps(swaps)

        # Advance the watermark to the newest tx actually observed.
        self.state.last_poll_timestamp = newest_overall

        pruned = self._prune_seen()
        if pruned:
            self.logger.info(
                f"  Pruned {pruned} seen tx hash(es) older than {SEEN_TX_TTL_DAYS}d"
            )

        self.check_max_hold()
        self._save_state()
        self.logger.info(
            f"=== Poll cycle complete === "
            f"(seen={len(self.state.seen_tx_hashes)}, "
            f"open={len(self.state.open_positions)}, "
            f"watermark={self.state.last_poll_timestamp})"
        )

    def run_daemon(self):
        """Run continuously."""
        self.logger.info(f"Starting daemon (poll interval: {self.poll_interval}s)")
        while True:
            try:
                self.run_once()
            except Exception as e:
                self.logger.error(f"Poll cycle error: {e}", exc_info=True)
            time.sleep(self.poll_interval)

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Paper copy-trader poller")
    parser.add_argument("--config", required=True, help="Path to watched-wallets.yaml")
    parser.add_argument("--state", default="copy_trader/state.json", help="Path to state file")
    parser.add_argument("--journal", default="copy_trader/closes.jsonl", help="Path to journal file")
    parser.add_argument("--once", action="store_true", help="Run one poll cycle and exit")
    parser.add_argument("--daemon", action="store_true", help="Run continuously")
    args = parser.parse_args()

    poller = CopyTraderPoller(args.config, args.state, args.journal)

    if args.once:
        poller.run_once()
    elif args.daemon:
        poller.run_daemon()
    else:
        print("Specify --once or --daemon")
        sys.exit(1)

if __name__ == "__main__":
    main()
