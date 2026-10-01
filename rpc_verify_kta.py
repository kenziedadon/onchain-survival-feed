#!/usr/bin/env python3
"""stdlib-only on-chain verification for KTA/WETH pool (Base mainnet)."""
import json
import time
import urllib.request

RPC = "https://mainnet.base.org"

# KTA / WETH pool discovered this run
POOL = "0xd9edc75a3a797ec92ca370f19051babebfb2edee"
WETH = "0x4200000000000000000000000000000000000006"
KTA  = "0xc0634090f2fe6c6d75e61be2b949464abb498973"

def rpc_call(method, params):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    req = urllib.request.Request(
        RPC, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)

def get_code(addr):
    res = rpc_call("eth_getCode", [addr, "latest"])
    code = res.get("result", "0x")
    return code if code else "0x"

def call_addr(addr, selector):
    data = selector + "0" * 56  # padding to 32-byte arg
    res = rpc_call("eth_call", [{"to": addr, "data": data}, "latest"])
    out = res.get("result", "0x")
    if not out or out == "0x":
        return None
    return "0x" + out[-40:]

def main():
    # eth_getCode for pool contract existence
    code = get_code(POOL)
    pool_exists = code not in ("0x", None) and len(code) > 2
    codesize = (len(code) - 2) // 2 if pool_exists else 0

    # token0() / token1() selectors
    token0_sel = "0x0dfe1681"  # token0()
    token1_sel = "0xd21220a7"  # token1()

    t0 = call_addr(POOL, token0_sel)
    t1 = call_addr(POOL, token1_sel)

    # pair match: unordered set {WETH, KTA}
    expected = {WETH.lower(), KTA.lower()}
    actual = {t0.lower(), t1.lower()} if (t0 and t1) else set()
    pair_match = (actual == expected)

    result = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + ".000Z",
        "verified_by": "rpc_verify_discovery_cron",
        "rpc": RPC,
        "coins": [{
            "name": "KTA / WETH",
            "pool": POOL,
            "token_addr": KTA,
            "quote": "WETH",
            "score": 61,
            "source": "gecko_trending",
            "onchain": {
                "verified": pool_exists and pair_match,
                "pool_exists": pool_exists,
                "pool_codesize": codesize,
                "token0": t0,
                "token1": t1,
                "expected_token0": WETH,
                "expected_token1": KTA,
                "pair_match": pair_match
            }
        }]
    }
    # append verified result as a single JSONL line
    with open("discovered.jsonl", "a") as f:
        f.write(json.dumps(result) + "\n")
    print(json.dumps(result, indent=2))
    return result

if __name__ == "__main__":
    main()
