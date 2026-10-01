#!/usr/bin/env python3
"""Stdlib-only on-chain verification for new discovered coins (score>50).
Verifies: pool contract exists (eth_getCode) + token0/token1 match expected pair.
"""
import json
import time
import urllib.request

RPC = "https://mainnet.base.org"
WETH = "0x4200000000000000000000000000000000000006"

# Latest scan's new coin with score>50
NEW_COINS = [
    {
        "name": "VVV / WETH",
        "pool": "0x01784ef301d79e4b2df3a21ad9a536d4cf09a5ce",
        "token": "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",
        "score": 95,
    },
]

SELECTORS = {"token0": "0x0dfe1681", "token1": "0xd21220a7"}


def rpc(method, params):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    req = urllib.request.Request(
        RPC,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "base-trend-bot/1.0"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read().decode())
    if "error" in resp:
        raise RuntimeError(resp["error"])
    return resp["result"]


def get_code(addr):
    return rpc("eth_getCode", [addr, "latest"])


def call_selector(addr, selector):
    res = rpc("eth_call", [{"to": addr, "data": selector}, "latest"])
    # last 20 bytes = address
    return "0x" + res[-40:]


def main():
    results = []
    for c in NEW_COINS:
        pool_code = get_code(c["pool"])
        token_code = get_code(c["token"])
        t0 = call_selector(c["pool"], SELECTORS["token0"])
        t1 = call_selector(c["pool"], SELECTORS["token1"])
        pool_exists = pool_code not in ("0x", None, "")
        token_exists = token_code not in ("0x", None, "")
        # expected: pool holds WETH + target token (order may vary)
        pair_match = (
            token_exists
            and {t0.lower(), t1.lower()}
            == {WETH.lower(), c["token"].lower()}
        )
        rec = {
            "name": c["name"],
            "pool": c["pool"],
            "token": c["token"],
            "score": c["score"],
            "rpc": RPC,
            "pool_exists": pool_exists,
            "token_exists": token_exists,
            "pool_codesize": len(pool_code) // 2 if pool_code != "0x" else 0,
            "token0": t0,
            "token1": t1,
            "weth": WETH,
            "pair_match": pair_match,
            "verified": pool_exists and token_exists and pair_match,
        }
        results.append(rec)
        time.sleep(1.5)
    return results


if __name__ == "__main__":
    out = main()
    print(json.dumps(out, indent=2))
    with open("/tmp/verify_out.json", "w") as f:
        json.dump(out, f, indent=2)
