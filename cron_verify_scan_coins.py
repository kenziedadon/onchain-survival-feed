#!/usr/bin/env python3
"""Verify the just-discovered high-score coins on-chain via Base RPC (stdlib only).

Reads the 2 newest coins from the latest scan, re-checks pool code + token pair.
Rate limit: 1.5s between RPC calls.
"""
import json
import time
import urllib.request

RPC_ENDPOINTS = [
    "https://mainnet.base.org",
    "https://base-rpc.publicnode.com",
    "https://base.llamarpc.com",
]
WETH = "0x4200000000000000000000000000000000000006"

# newest scan coins (from trend_discovery run at 2026-08-29T03:50Z)
COINS = [
    ("DRB / WETH 1%", "0x5116773e18a9c7bb03ebb961b38678e45e238923",
     "0x3ec2156d4c0a9cbdab4a016633b7bcf6a8d68ea2", 73),
    ("KTA / WETH", "0xd9edc75a3a797ec92ca370f19051babebfb2edee",
     "0xc0634090f2fe6c6d75e61be2b949464abb498973", 61),
]

USED_RPC = [None]

def rpc_call(method, params):
    last_err = None
    for rpc in RPC_ENDPOINTS:
        try:
            payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                                   "params": params}).encode()
            req = urllib.request.Request(
                rpc, data=payload,
                headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                USED_RPC[0] = rpc
                return json.loads(r.read().decode())
        except Exception as e:
            last_err = e
            continue
    raise last_err

def get_code(addr):
    res = rpc_call("eth_getCode", [addr, "latest"])
    code = res.get("result", "0x")
    size = len(code) // 2 if code and code != "0x" else 0
    return size

def get_token(fn, pool):
    sel = "0x0dfe1681" if fn == "token0" else "0xd21220a7"
    res = rpc_call("eth_call", [{"to": pool, "data": sel}, "latest"])
    rr = res.get("result", "0x")
    if not rr or rr == "0x":
        return None
    return "0x" + rr[-40:]

results = []
for name, pool, token, score in COINS:
    size = get_code(pool)
    pool_exists = size > 0
    time.sleep(1.5)  # rate limit between RPC calls
    t0 = get_token("token0", pool)
    time.sleep(1.5)
    t1 = get_token("token1", pool)
    expected = {WETH.lower(), token.lower()}
    actual = {str(t0).lower(), str(t1).lower()} if t0 and t1 else set()
    pair_match = actual == expected
    entry = {
        "name": name, "address": pool, "token_addr": token, "quote": "WETH",
        "score": score, "source": "gecko_trending",
        "onchain": {
            "verified": pool_exists and pair_match,
            "rpc": USED_RPC[0],
            "pool_exists": pool_exists,
            "pool_codesize": size,
            "token0": t0, "token1": t1,
            "pair_match": pair_match,
            "expected_token0": WETH, "expected_token1": token,
        },
    }
    results.append(entry)
    print(f"{name:14s} exists={pool_exists} codesize={size} "
          f"t0={t0} t1={t1} pair_match={pair_match}")

verified = [r for r in results if r["onchain"]["verified"]]
print(f"\nVERIFIED {len(verified)}/{len(results)} coins via {USED_RPC[0]}")

out = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + ".000Z",
       "verified_by": "rpc_verify_scan", "coins": results}
with open("discovered.jsonl", "a") as f:
    f.write(json.dumps(out) + "\n")
print("Appended fresh RPC verification to discovered.jsonl")
