#!/usr/bin/env python3
"""Verify the new high-score coin (VVV/WETH, score 95) on-chain via Base RPC (stdlib only)."""
import json
import time
import urllib.request
from _atomic_write import atomic_jsonl_append

RPC_ENDPOINTS = [
    "https://mainnet.base.org",
    "https://base-rpc.publicnode.com",
    "https://base.llamarpc.com",
]
WETH = "0x4200000000000000000000000000000000000006"

# New coin from this scan (line 44) with score > 50
NAME, POOL, TOKEN, SCORE = (
    "VVV / WETH", "0x01784ef301d79e4b2df3a21ad9a536d4cf09a5ce",
    "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf", 95,
)

USED_RPC = [None]
def rpc_call(method, params):
    last_err = None
    for rpc in RPC_ENDPOINTS:
        try:
            payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
            req = urllib.request.Request(rpc, data=payload, headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
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
    return code if code else "0x", len(code) // 2 if code and code != "0x" else 0

def get_token(fn, pool):
    sel = "0x0dfe1681" if fn == "token0" else "0xd21220a7"
    res = rpc_call("eth_call", [{"to": pool, "data": sel}, "latest"])
    r = res.get("result", "0x")
    if not r or r == "0x":
        return None
    return "0x" + r[-40:]

code, size = get_code(POOL)
pool_exists = size > 0
t0 = get_token("token0", POOL)
t1 = get_token("token1", POOL)
expected = {WETH.lower(), TOKEN.lower()}
actual = {str(t0).lower(), str(t1).lower()} if t0 and t1 else set()
pair_match = actual == expected
entry = {
    "name": NAME, "address": POOL, "token_addr": TOKEN, "quote": "WETH",
    "score": SCORE, "source": "gecko_trending",
    "onchain": {
        "verified": pool_exists and pair_match,
        "rpc": USED_RPC[0],
        "pool_exists": pool_exists,
        "pool_codesize": size,
        "token0": t0, "token1": t1,
        "pair_match": pair_match,
        "expected_token0": WETH, "expected_token1": TOKEN,
    },
}
print(f"{NAME:14s} exists={pool_exists} codesize={size} t0={t0} t1={t1} pair_match={pair_match} rpc={USED_RPC[0]}")

out = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + ".000Z", "verified_by": "rpc_verify", "coins": [entry]}
atomic_jsonl_append("discovered.jsonl", out)
print("Appended VVV verification to discovered.jsonl" if entry["onchain"]["verified"] else "WARNING: VVV NOT verified")
