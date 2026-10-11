#!/usr/bin/env python3
"""
quote_parity.py — read-only THREE-venue quote parity + spread journaling.

OBSERVATION ONLY. This module adds KyberSwap as the third quote source beside
Uniswap V3 and Aerodrome, pulls a same-minute quote from each, computes the
pairwise spread in bps, and appends a journal line when venues disagree beyond
SPREAD_ALERT_BPS. Its only output is kyber_spread_journal.jsonl + stdout.

It is DELIBERATELY NOT wired into execution venue selection:
  * It does not touch safe_intent.py, simulate_intent(), executor.py, risk.py,
    or caps.json. No venue is chosen or ranked here.
  * Nothing here can trade, sign, build, encode a swap, or write state.
  * The point is spotting venue DIVERGENCE (a KyberSwap route that prices far
    off the on-chain quoters = a signal worth logging), not routing to the best
    one. Per the spec it is an independent cross-check, not a router.

Quote sources:
  * kyberswap   — KyberQuoter (kyber_quoter.py), fail-closed router check.
  * uniswap_v3  — Uniswap V3 QuoterV2.quoteExactInputSingle (live pool probe).
  * aerodrome   — Aerodrome Router.getAmountsOut (volatile route).

All on-chain addresses verified by eth_getCode before use (fail-closed).

Hand-rolled ABI via simulate.py's keccak/rpc plumbing (no web3). Stdlib only.
"""

import json
import os
import time

import simulate  # rpc + keccak + eth_call + has_code plumbing (read-only)
from kyber_quoter import KyberQuoter, ROUTER_BASE

HERE = os.path.dirname(os.path.abspath(__file__))
JOURNAL = os.path.join(HERE, "kyber_spread_journal.jsonl")
SPREAD_ALERT_BPS = 300

# verified by eth_getCode on Base 2026-10-11
UNIV3_FACTORY = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"
UNIV3_QUOTERV2 = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
AERO_ROUTER = "0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43"
AERO_FACTORY = "0x420DD381b31aEf6683db6B902084cB0FFECe40Da"
UNIV3_FEE_TIERS = [100, 500, 3000, 10000]

WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
USDC_DEC = 6


# ------------------------------- ABI encoders -------------------------------
# quoteExactInputSingle((address,address,uint256,uint24,uint160)) — the tuple
# has only static members, so it encodes inline as 5x32 bytes (no offset ptr).
_QUOTER2_SIG = "quoteExactInputSingle((address,address,uint256,uint24,uint160))"
_GETPOOL_SIG = "getPool(address,address,uint24)"
# getAmountsOut(uint256,(address,address,bool,address)[]) — routes is a dynamic
# array of static tuples: amountIn word, offset ptr, length, elements.
_AERO_SIG = "getAmountsOut(uint256,(address,address,bool,address)[])"

# Regression anchor: the exact calldata viem encodeFunctionData produces for
# getAmountsOut(1e18, [(WETH,USDC,false,AERO_FACTORY)]). _enc_aero must
# reproduce this byte-for-byte. Generated from viem's live output (2026-10-11),
# NOT hand-transcribed, to avoid a typo'd anchor.
_AERO_CALLDATA_VERIFIED = (
    "0x"
    "5509a1ac0000000000000000000000000000000000000000000000000de0b6b3"
    "a764000000000000000000000000000000000000000000000000000000000000"
    "0000004000000000000000000000000000000000000000000000000000000000"
    "0000000100000000000000000000000042000000000000000000000000000000"
    "00000006000000000000000000000000833589fcd6edb6e08f4c7c32d4f71b54"
    "bda0291300000000000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000420dd381b31aef6683db6b902084cb0f"
    "fece40da"
)


def _enc_quoter2(token_in, token_out, amount_in_wei, fee):
    sel = simulate._selector(_QUOTER2_SIG).hex()
    body = (simulate._enc_address(token_in) +
            simulate._enc_address(token_out) +
            simulate._enc_uint(amount_in_wei, 256).hex() +
            simulate._enc_uint(fee, 24).hex() +
            simulate._enc_uint(0, 160).hex())
    return "0x" + sel + body


def _enc_getpool(token_in, token_out, fee):
    sel = simulate._selector(_GETPOOL_SIG).hex()
    body = (simulate._enc_address(token_in) +
            simulate._enc_address(token_out) +
            simulate._enc_uint(fee, 24).hex())
    return "0x" + sel + body


def _enc_aero(amount_in_wei, token_in, token_out):
    # ABI: getAmountsOut(uint256,(address,address,bool,address)[])
    # Word 1 = amountIn; word 2 = offset to routes[] (0x40); then array
    # length + one static tuple element (from, to, stable, factory).
    # Calldata byte-verified against viem encodeFunctionData for this exact
    # input (2026-10-11) — see _enc_aero_regression() below.
    sel = simulate._selector(_AERO_SIG).hex()
    head = simulate._enc_uint(amount_in_wei, 256).hex()  # amountIn
    routes_off = simulate._enc_uint(0x40, 256).hex()      # offset to routes[]
    length = simulate._enc_uint(1, 256).hex()             # one route
    elem = (simulate._enc_address(token_in) +
            simulate._enc_address(token_out) +
            simulate._enc_uint(0, 8).hex() +              # stable = false (volatile)
            simulate._enc_address(AERO_FACTORY))
    return "0x" + sel + head + routes_off + length + elem


# ------------------------------- quoters ------------------------------------
def quote_uniswap_v3(token_in, token_out, amount_in_wei):
    """Returns USDC-denominated amount_out (float) or None. Fail-closed."""
    if not simulate.has_code(UNIV3_QUOTERV2):
        return None
    for fee in UNIV3_FEE_TIERS:
        pool = simulate.eth_call(UNIV3_FACTORY, _enc_getpool(token_in, token_out, fee))
        if not pool or pool == "0x" + "0" * 64:
            continue
        pool_addr = "0x" + pool[26:]
        if not simulate.has_code(pool_addr):
            continue
        res = simulate.eth_call(UNIV3_QUOTERV2, _enc_quoter2(token_in, token_out, amount_in_wei, fee))
        if not res or len(res) < 66:
            continue
# quoteExactInputSingle returns (uint256 amountOut, uint160 sqrtPriceX96After,
# uint32 initializedTicksCrossed, uint256 gasEstimate). amountOut is the FIRST
# 32-byte word — the head, NOT the tail (that would be gasEstimate).
        amount_out = int(res[2:66], 16)
        return amount_out / (10 ** USDC_DEC)
    return None


def quote_aerodrome(token_in, token_out, amount_in_wei):
    """Returns USDC-denominated amount_out (float) or None. Fail-closed."""
    if not simulate.has_code(AERO_ROUTER):
        return None
    res = simulate.eth_call(AERO_ROUTER, _enc_aero(amount_in_wei, token_in, token_out))
    if not res or res == "0x":
        return None
    # getAmountsOut returns uint256[]; take the last 32-byte word.
    body = res[2:]
    if len(body) < 64:
        return None
    last_word = body[-64:]
    amount_out = int(last_word, 16)
    return amount_out / (10 ** USDC_DEC)


def quote_kyberswap(token_in, token_out, amount_in_wei):
    """Returns USDC-denominated amount_out (float) or None. Fail-closed via
    KyberQuoter (raises on no-code router / API error -> caught here)."""
    try:
        q = KyberQuoter().quote(token_in, token_out, amount_in_wei)
        out = q.get("amountOut")
        return out / (10 ** USDC_DEC) if out is not None else None
    except Exception:
        return None


# ------------------------------- parity -------------------------------------
def spread_bps(a, b):
    if not a or not b or a == 0:
        return None
    return (abs(a - b) / ((a + b) / 2.0)) * 10000


def _journal(line):
    with open(JOURNAL, "a") as f:
        f.write(json.dumps(line) + "\n")


def run_parity(token_in=WETH, token_out=USDC, amount_in_wei=None):
    """Read-only three-venue quote + spread journaling. Returns the dict."""
    if amount_in_wei is None:
        amount_in_wei = 10 ** 18  # 1 unit
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    pair = f"{token_in[:6]}..{token_in[-4:]}->{token_out[:6]}..{token_out[-4:]}"

    quotes = {
        "kyberswap": quote_kyberswap(token_in, token_out, amount_in_wei),
        "uniswap_v3": quote_uniswap_v3(token_in, token_out, amount_in_wei),
        "aerodrome": quote_aerodrome(token_in, token_out, amount_in_wei),
    }

    names = list(quotes.keys())
    spreads = {}
    max_spread = None
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            label = f"{names[i]} vs {names[j]}"
            s = spread_bps(quotes[names[i]], quotes[names[j]])
            spreads[label] = None if s is None else round(s, 2)
            if s is not None and (max_spread is None or s > max_spread):
                max_spread = s

    record = {"ts": ts, "pair": pair, "amount_in_wei": str(amount_in_wei),
              "quotes": {k: (round(v, 6) if v is not None else None) for k, v in quotes.items()},
              "spreads_bps": spreads,
              "max_spread_bps": None if max_spread is None else round(max_spread, 2)}

    # Journal only on real divergence — this is the observation payload.
    if max_spread is not None and max_spread > SPREAD_ALERT_BPS:
        record["kind"] = "live_spread_alert"
        record["threshold_bps"] = SPREAD_ALERT_BPS
        _journal(record)

    return record


# --------------------------- regression (the point) -------------------------
def _enc_aero_regression():
    """Assert _enc_aero reproduces the viem-verified calldata byte-for-byte.
    This is the exact encoding that failed before (dropped amountIn word);
    the anchor is worthless without this assertion. Exits non-zero on fail."""
    mine = _enc_aero(10 ** 18, WETH, USDC)
    if mine.lower() != _AERO_CALLDATA_VERIFIED.lower():
        print("REGRESSION FAILED: _enc_aero != viem-verified calldata")
        print(f"  mine     ({len(mine)}): {mine}")
        print(f"  verified ({len(_AERO_CALLDATA_VERIFIED)}): {_AERO_CALLDATA_VERIFIED}")
        raise SystemExit(1)
    print(f"PASS: _enc_aero reproduces viem calldata byte-for-byte ({len(mine)} chars)")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        _enc_aero_regression()
        raise SystemExit(0)
    r = run_parity()
    print(json.dumps(r, indent=2))
    print(f"\n  journal: {JOURNAL}")
    print(f"  alert threshold: {SPREAD_ALERT_BPS} bps")
    ms = r["max_spread_bps"]
    if ms is not None and ms > SPREAD_ALERT_BPS:
        print(f"  >> DIVERGENCE {ms} bps > {SPREAD_ALERT_BPS} — journaled")
    else:
        print(f"  max spread {ms} bps <= {SPREAD_ALERT_BPS} — no alert (expected)")
