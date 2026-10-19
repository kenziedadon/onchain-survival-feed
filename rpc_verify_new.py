#!/usr/bin/env python3
"""Independent on-chain verification of newly discovered Base pools via Base RPC.
Stdlib only (urllib, json). Verifies:
  - pool contract exists (eth_getCode -> non-empty bytecode)
  - token0()/token1() match the expected WETH<->token pair
Appends a verification record to discovered.jsonl.
"""
import json
import time
import urllib.request
from _atomic_write import atomic_jsonl_append

RPC = "https://mainnet.base.org"
WETH = "0x4200000000000000000000000000000000000006"

# New coins from this scan run (score > 50)
NEW_COINS = [
    {
        "name": "VVV / WETH",
        "pool": "0x01784ef301d79e4b2df3a21ad9a536d4cf09a5ce",
        "token_addr": "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",
        "score": 103,
        "expected_token0": WETH,
        "expected_token1": "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",
    },
    {
        "name": "cbBTC / WETH 0.01%",
        "pool": "0xc211e1f853a898bd1302385ccde55f33a8c4b3f3",
        "token_addr": "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf",
        "score": 83,
        "expected_token0": WETH,
        "expected_token1": "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf",
    },
    {
        "name": "ElonRWA / WETH 0.01%",
        "pool": "0xd56f086e7b796b313d49f2bc926fac4bdd2a2b0b",
        "token_addr": "0xaa6cccdce193698d33deb9ffd4be74eaa74c4898",
        "score": 55,
        "expected_token0": WETH,
        "expected_token1": "0xaa6cccdce193698d33deb9ffd4be74eaa74c4898",
    },
]

TOKEN0_SEL = "0x0dfe1681"
TOKEN1_SEL = "0xd21220a7"


def rpc(method, params, retries=4):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    }
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                RPC,
                data=json.dumps(payload).encode(),
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Hermes-Base-Research/1.0",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                resp = json.loads(r.read().decode())
            if "error" in resp:
                raise RuntimeError(str(resp["error"]))
            return resp.get("result")
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429:
                wait = 2 ** (attempt + 1)
                time.sleep(wait)
                continue
            raise
    raise last_err


def addr_from_hex(hx):
    """Extract a checksum-agnostic address from a 32-byte eth_call result."""
    if not hx or hx == "0x":
        return None
    # take last 20 bytes
    return "0x" + hx[-40:].lower()


def verify(coin):
    pool = coin["pool"]
    out = {"name": coin["name"], "pool": pool, "score": coin["score"]}
    # 1. contract exists
    code = rpc("eth_getCode", [pool, "latest"])
    exists = code is not None and code not in ("0x", "0x0")
    out["pool_exists"] = exists
    out["pool_codesize"] = (len(code) - 2) // 2 if exists else 0
    # 2. token0/token1
    t0 = addr_from_hex(rpc("eth_call", [{"to": pool, "data": TOKEN0_SEL}, "latest"]))
    t1 = addr_from_hex(rpc("eth_call", [{"to": pool, "data": TOKEN1_SEL}, "latest"]))
    out["token0"] = t0
    out["token1"] = t1
    pair_match = (
        t0 in (coin["expected_token0"], coin["expected_token1"])
        and t1 in (coin["expected_token0"], coin["expected_token1"])
        and t0 != t1
    )
    out["pair_match"] = pair_match
    out["expected_token0"] = coin["expected_token0"]
    out["expected_token1"] = coin["expected_token1"]
    out["verified"] = bool(exists and pair_match)
    return out


def main():
    results = []
    for coin in NEW_COINS:
        v = verify(coin)
        # normalize hex case for comparison robustness
        results.append(v)
        print(
            f"  {v['name']:24s} exists={v['pool_exists']} code={v['pool_codesize']:>6}B "
            f"t0={v['token0']} t1={v['token1']} pair_match={v['pair_match']}"
        )
        time.sleep(1.5)
    # append verification record to discovered.jsonl
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + ".000Z",
        "verified_by": "rpc_verify_new_cron",
        "rpc": RPC,
        "coins": results,
    }
    atomic_jsonl_append("discovered.jsonl", record)
    print(f"\nAppended verification record for {len(results)} coins to discovered.jsonl")


if __name__ == "__main__":
    main()
