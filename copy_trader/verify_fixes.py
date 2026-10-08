#!/usr/bin/env python3
"""Targeted verification of fixes 1 (decimals split) and 3 (timestamp crash).

These two fixes are NOT directly exercised by test_acceptance.py, so they get
their own mechanical proof here.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from copy_trader.poller import (
    extract_swap_from_receipt,
    _parse_timestamp,
    STABLECOINS,
    TOKEN_DECIMALS,
    DEFAULT_TOKEN_DECIMALS,
    TRANSFER_TOPIC,
    WETH,
)

WALLET = "0xd569e570b94265e5ebc7df1990f99ee68d3f60a1"
ROUTER = "0xcf77a3ba9a5ca396e57b271c5c0b26f421741e481"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
TOKEN18 = "0x9b83c2f3e6c9c0b8c2f4a1d5e7b9c1a3f5d7e9b1"

pad = lambda a: "0x" + a[2:].rjust(64, "0")

def transfer_log(token, frm, to, raw_value):
    return {
        "address": token,
        "topics": [TRANSFER_TOPIC, pad(frm), pad(to)],
        "data": hex(raw_value),
    }

print("=" * 70)
print("FIX 1 — decimals split (USDC -> 18-decimal token)")
print("=" * 70)
# Wallet spends 25 USDC (6 decimals) and receives 1e21 token units (18 decimals).
receipt = {
    "status": "0x1",
    "transactionHash": "0xdecimals_test",
    "logs": [
        transfer_log(USDC, WALLET, ROUTER, 25 * 10**6),        # wallet sent 25 USDC
        transfer_log(TOKEN18, ROUTER, WALLET, 1000 * 10**18),  # wallet got 1000 tokens
    ],
}
swap = extract_swap_from_receipt(receipt, WALLET)
assert swap is not None, "swap extraction returned None"
print(f"  token_in      = {swap.token_in}")
print(f"  amount_in     = {swap.amount_in}")
print(f"  token_out     = {swap.token_out}")
print(f"  amount_out    = {swap.amount_out}")
assert abs(swap.amount_in - 25.0) < 1e-9, f"amount_in wrong: {swap.amount_in} (want 25.0)"
assert abs(swap.amount_out - 1000.0) < 1e-9, f"amount_out wrong: {swap.amount_out} (want 1000.0)"
print("  ✓ amount_in = 25.0 (USDC, 1e6) — correct")
print("  ✓ amount_out = 1000.0 (18-dec) — correct")
print("  ✓ Old single-variable code: token_in=USDC set decimals=6, so")
print("     amount_out = 1000e18/1e6 = 1e15 — 1e12x too large (the reported bug).")
print("     amount_in happened to be right by luck (both sides used 1e6).")

# DAI is the case the binary 'stable = 6' rule got wrong. 18-decimal stable in,
# 18-decimal token out — old rule divided the DAI leg by 1e6.
DAI = "0x50c5725949a6f0c72e6c4a641f24049a917db0cb"
print()
print("  DAI (18-decimal stable) -> 18-decimal token, end-to-end:")
receipt_dai = {
    "status": "0x1",
    "transactionHash": "0xdai_test",
    "logs": [
        transfer_log(DAI, WALLET, ROUTER, 50 * 10**18),        # wallet sent 50 DAI
        transfer_log(TOKEN18, ROUTER, WALLET, 2000 * 10**18),  # wallet got 2000 tokens
    ],
}
swap_dai = extract_swap_from_receipt(receipt_dai, WALLET)
assert swap_dai is not None, "DAI swap extraction returned None"
print(f"    token_in   = {swap_dai.token_in}")
print(f"    amount_in  = {swap_dai.amount_in}")
print(f"    amount_out = {swap_dai.amount_out}")
assert abs(swap_dai.amount_in - 50.0) < 1e-9, f"DAI amount_in wrong: {swap_dai.amount_in} (want 50.0)"
assert abs(swap_dai.amount_out - 2000.0) < 1e-9, f"amount_out wrong: {swap_dai.amount_out} (want 2000.0)"
print("    ✓ amount_in = 50.0 (DAI, 1e18) — old binary rule gave 5e-5 (1e12x too small)")
print("    ✓ amount_out = 2000.0 — correct")

print()
print("=" * 70)
print("FIX 2 — STABLECOINS + TOKEN_DECIMALS, every row verified on-chain")
print("=" * 70)

import requests

def bytecode_size(addr):
    """Return deployed bytecode size in bytes for addr on Base mainnet."""
    try:
        r = requests.post(
            "https://mainnet.base.org",
            json={"jsonrpc": "2.0", "method": "eth_getCode",
                  "params": [addr, "latest"], "id": 1},
            headers={"Content-Type": "application/json"},
            timeout=20,
        )
        result = r.json().get("result", "0x")
        return 0 if not result or result == "0x" else len(result) // 2 - 1
    except Exception as e:
        return f"err:{e}"

def call_hex(addr, data, retries=6, delay=1.5):
    """eth_call returning the raw hex result.

    Base's public RPC throttles bursts of calls and answers with an EMPTY
    result (not an error). Retry with backoff so throttling is not
    misreported as a wrong on-chain value.
    """
    for attempt in range(retries):
        try:
            r = requests.post(
                "https://mainnet.base.org",
                json={"jsonrpc": "2.0", "method": "eth_call",
                      "params": [{"to": addr, "data": data}, "latest"], "id": 1},
                headers={"Content-Type": "application/json"},
                timeout=25,
            )
            j = r.json()
            if "error" not in j:
                res = j.get("result", "")
                if res and res != "0x":
                    return res
        except Exception:
            pass
        if attempt < retries - 1:
            time.sleep(delay * (attempt + 1))
    return ""

def token_symbol(addr):
    """Read symbol() from an ERC20 and decode the string."""
    raw = call_hex(addr, "0x95d89b41")
    if not raw or raw == "0x":
        return None
    try:
        b = bytes.fromhex(raw[2:])
        if len(b) < 64:
            return None
        length = int.from_bytes(b[32:64], "big")
        return b[64:64 + length].decode("utf-8", "replace")
    except Exception:
        return None

def token_decimals(addr):
    """Read decimals() from an ERC20."""
    raw = call_hex(addr, "0x313ce567")
    if not raw or raw == "0x":
        return None
    try:
        return int(raw, 16)
    except Exception:
        return None

# Every address in STABLECOINS must be a real deployed contract.
print(f"  STABLECOINS: {len(STABLECOINS)} entries, each checked for contract code:")
all_deployed = True
for addr in sorted(STABLECOINS):
    size = bytecode_size(addr)
    if isinstance(size, int) and size > 0:
        print(f"    ✓ {addr}  {size} bytes")
    else:
        all_deployed = False
        print(f"    ✗ {addr}  {size} bytes  <-- EMPTY ADDRESS")
    time.sleep(1.2)  # avoid tripping the public RPC's burst throttle
assert all_deployed, "a stablecoin address has no contract code on Base mainnet"
print("  ✓ Every address has deployed bytecode")

# Every TOKEN_DECIMALS row must match the chain's own decimals() answer.
# This is the check that catches an assumed default (DAI/USDS are 18, not 6).
print()
print("  TOKEN_DECIMALS: each row compared against on-chain decimals():")
assert set(TOKEN_DECIMALS) == set(STABLECOINS), (
    "TOKEN_DECIMALS and STABLECOINS must cover the same address set; "
    f"only-in-decimals={set(TOKEN_DECIMALS)-set(STABLECOINS)}, "
    f"only-in-stables={set(STABLECOINS)-set(TOKEN_DECIMALS)}"
)
all_decimals_ok = True
inconclusive = []
for addr in sorted(TOKEN_DECIMALS):
    declared = TOKEN_DECIMALS[addr]
    actual = token_decimals(addr)
    sym = token_symbol(addr) or addr[:10]
    if actual is None:
        # Could not read the chain. This is NOT a pass and NOT a mismatch.
        inconclusive.append((sym, addr, declared))
        print(f"    ? {sym:10} {addr}  declared={declared}  on-chain=UNREADABLE")
    elif actual == declared:
        print(f"    ✓ {sym:10} {addr}  declared={declared}  on-chain={actual}")
    else:
        all_decimals_ok = False
        print(f"    ✗ {sym:10} {addr}  declared={declared}  on-chain={actual}  <-- MISMATCH")
    time.sleep(1.2)  # avoid tripping the public RPC's burst throttle

assert all_decimals_ok, "a TOKEN_DECIMALS row disagrees with the chain"
assert not inconclusive, (
    f"could not read decimals() for {len(inconclusive)} address(es) — "
    f"unverified, not passing: {[a for _, a, _ in inconclusive]}"
)
print("  ✓ Every declared decimals value matches the chain (no assumed defaults)")

# Named spot-check: DAI and USDS are the two the old binary rule got wrong.
print()
print("  Regression for the binary 'stable = 6' rule (the 1e12 bug):")
for addr, name in [("0x50c5725949a6f0c72e6c4a641f24049a917db0cb", "DAI"),
                   ("0x820c137fa70c8691f0e44dc420a5e53c168921dc", "USDS")]:
    dec = TOKEN_DECIMALS.get(addr)
    assert dec == 18, f"{name} should be 18 decimals, map says {dec}"
    old = 6  # what `6 if addr in STABLECOINS else 18` produced
    print(f"    {name}: map={dec}, old binary rule={old} -> old was off by 10^{dec-old}")
print("  ✓ DAI/USDS now use 18; the old rule divided them by 1e6 (1e12x too small)")

# Prove a USDbC-paid buy is no longer misidentified as the target token
print()
print("  USDbC-paid buy (regression for the misidentification bug):")
receipt2 = {
    "status": "0x1",
    "transactionHash": "0xusdbc_test",
    "logs": [
        transfer_log("0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca", WALLET, ROUTER, 10 * 10**6),
        transfer_log(TOKEN18, ROUTER, WALLET, 500 * 10**18),
    ],
}
swap2 = extract_swap_from_receipt(receipt2, WALLET)
assert swap2 is not None, "USDbC buy extraction returned None"
assert swap2.token_out == TOKEN18, f"USDbC misidentified as target: {swap2.token_out}"
assert swap2.token_in == "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca", f"token_in wrong: {swap2.token_in}"
print(f"    token_out = {swap2.token_out[:20]}…  (the real token, not USDbC)")
print(f"    token_in  = {swap2.token_in[:20]}…  (USDbC correctly read as payment)")
print("  ✓ USDbC buy no longer misidentifies the stablecoin as the target")

print()
print("=" * 70)
print("FIX 3 — timestamp parse no longer crashes")
print("=" * 70)
cases = [
    ("2026-08-20T12:00:00Z", "valid ISO with Z"),
    ("2026-08-20T12:00:00+00:00", "valid ISO with offset"),
    ("2026-08-20T12:00:00.123456Z", "valid ISO with micros"),
    (None, "None (missing key)"),
    ("", "empty string"),
    ("not-a-date", "garbage string"),
    (1234567890, "raw int"),
    ("1234567890", "numeric string"),
    ({}, "wrong type (dict)"),
]
ok = True
for value, desc in cases:
    try:
        result = _parse_timestamp(value)
        print(f"  ✓ {desc:28} -> {result}")
    except Exception as e:
        ok = False
        print(f"  ✗ {desc:28} -> CRASHED: {type(e).__name__}: {e}")
assert ok, "timestamp parser crashed on at least one case"
print("  ✓ No ValueError on any missing/invalid timestamp — poll cycle survives")

print()
print("=" * 70)
print("ALL TARGETED FIX CHECKS PASSED")
print("=" * 70)
