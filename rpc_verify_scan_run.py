#!/usr/bin/env python3
"""Independent on-chain verification of this scan run's new Base pools (score > 50).
Stdlib only (urllib, json). Verifies eth_getCode + token0()/token1() pair match,
then appends a verification record to discovered.jsonl.

New coins from this run (trend_discovery.py @ 2026-08-29T06:14Z):
  - KTA / WETH   score=76  pool=0xd9edc75...2edee token=0xc06340...98973
  - DRB / WETH 1% score=68 pool=0x511677...b3f3 token=0x3ec215...8ea2
"""
import json
import time
import urllib.request

RPC = "https://mainnet.base.org"
WETH = "0x4200000000000000000000000000000000000006"

# (name, pool, token_addr, score)
NEW_COINS = [
    ("KTA / WETH", "0xd9edc75a3a797ec92ca370f19051babebfb2edee",
     "0xc0634090f2fe6c6d75e61be2b949464abb498973", 76),
    ("DRB / WETH 1%", "0x5116773e18a9c7bb03ebb961b38678e45e238923",
     "0x3ec2156d4c0a9cbdab4a016633b7bcf6a8d68ea2", 68),
]

TOKEN0_SEL = "0x0dfe1681"
TOKEN1_SEL = "0xd21220a7"


def rpc(method, params, retries=4):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
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
                time.sleep(2 ** (attempt + 1))
                continue
            raise
    raise last_err


def addr_from_hex(hx):
    if not hx or hx == "0x":
        return None
    return "0x" + hx[-40:].lower()


def verify(name, pool, token_addr, score):
    out = {"name": name, "pool": pool, "token_addr": token_addr, "score": score,
           "source": "gecko_trending"}
    code = rpc("eth_getCode", [pool, "latest"])
    exists = code is not None and code not in ("0x", "0x0")
    out["pool_exists"] = exists
    out["pool_codesize"] = (len(code) - 2) // 2 if exists else 0
    t0 = addr_from_hex(rpc("eth_call", [{"to": pool, "data": TOKEN0_SEL}, "latest"]))
    t1 = addr_from_hex(rpc("eth_call", [{"to": pool, "data": TOKEN1_SEL}, "latest"]))
    out["token0"] = t0
    out["token1"] = t1
    out["expected_token0"] = WETH
    out["expected_token1"] = token_addr
    pair_match = (
        t0 in (WETH, token_addr) and t1 in (WETH, token_addr) and t0 != t1
    )
    out["pair_match"] = pair_match
    out["verified"] = bool(exists and pair_match)
    return out


def main():
    # rate-limit: 1.5s between GeckoTerminal calls is for the discovery side;
    # here we pace RPC calls modestly to avoid 429.
    results = []
    for i, (name, pool, token_addr, score) in enumerate(NEW_COINS):
        v = verify(name, pool, token_addr, score)
        results.append(v)
        print(f"  {name:18s} exists={v['pool_exists']} code={v['pool_codesize']:>6}B "
              f"t0={v['token0']} t1={v['token1']} pair_match={v['pair_match']} "
              f"verified={v['verified']}")
        if i < len(NEW_COINS) - 1:
            time.sleep(1.5)
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + ".000Z",
        "verified_by": "rpc_verify_scan_run_cron",
        "rpc": RPC,
        "coins": results,
    }
    with open("discovered.jsonl", "a") as f:
        f.write(json.dumps(record) + "\n")
    print(f"\nAppended verification record for {len(results)} coins to discovered.jsonl")


if __name__ == "__main__":
    main()
