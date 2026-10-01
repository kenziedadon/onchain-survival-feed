#!/usr/bin/env python3
"""On-chain verification of discovered coins (score>50) via Base RPC. Stdlib only."""
import json
import time
import urllib.request

RPCS = [
    "https://mainnet.base.org",
    "https://base-rpc.publicnode.com",
    "https://base.llamarpc.com",
]
WETH = "0x4200000000000000000000000000000000000006"

# coins from the latest scan needing verification (score > 50)
# line 37 of discovered.jsonl (this run): DRB score=61, BNKR score=28
PENDING = [
    {"name": "DRB / WETH 1%", "address": "0x5116773e18a9c7bb03ebb961b38678e45e238923",
     "token_addr": "0x3ec2156d4c0a9cbdab4a016633b7bcf6a8d68ea2", "quote": "WETH",
     "score": 61, "source": "gecko_trending"},
]

def rpc_call(rpc, method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(rpc, data=body, headers={"Content-Type": "application/json", "User-Agent": "base-trend-bot/1.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        resp = json.loads(r.read().decode())
    if "error" in resp:
        raise RuntimeError(resp["error"])
    return resp["result"]

def get_code_size(rpc, addr):
    code = rpc_call(rpc, "eth_getCode", [addr, "latest"])
    return len(code) // 2 - 1  # bytes = hexchars/2, minus 0x

def get_token(rpc, pool, selector):
    res = rpc_call(rpc, "eth_call", [{"to": pool, "data": selector}, "latest"])
    return "0x" + res[26:]  # last 20 bytes

def verify(coin):
    last_err = None
    for rpc in RPCS:
        try:
            cs = get_code_size(rpc, coin["address"])
            t0 = get_token(rpc, coin["address"], "0x0dfe1681")  # token0()
            t1 = get_token(rpc, coin["address"], "0xd21220a7")  # token1()
            toks = {t0.lower(), t1.lower()}
            expected = {WETH.lower(), coin["token_addr"].lower()}
            pair_match = toks == expected
            return {"verified": cs > 0, "rpc": rpc, "pool_exists": cs > 0,
                    "pool_codesize": cs, "token0": t0, "token1": t1, "pair_match": pair_match}
        except Exception as e:
            last_err = str(e)
            continue
    return {"verified": False, "error": last_err, "pair_match": False}

out = []
for c in PENDING:
    time.sleep(1.5)  # rate limit between RPC calls
    oc = verify(c)
    rec = {"name": c["name"], "address": c["address"], "token_addr": c["token_addr"],
           "quote": c["quote"], "score": c["score"], "source": c["source"], "onchain": oc}
    out.append(rec)
    print(f"{c['name']}: verified={oc.get('verified')} pair_match={oc.get('pair_match')} "
          f"codesize={oc.get('pool_codesize')} rpc={oc.get('rpc','')}")

record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
          "verified_by": "verify_scan", "coins": out}
with open("discovered.jsonl", "a") as f:
    f.write(json.dumps(record) + "\n")
print("Wrote verification record to discovered.jsonl")
