#!/usr/bin/env python3
"""Stdlib-only on-chain verification for discovered Base pools.
Verifies pool contract exists (eth_getCode) and token0/token1 match expected pair.
"""
import json
import time
import urllib.request
from _atomic_write import atomic_jsonl_append

RPC = "https://mainnet.base.org"
WETH = "0x4200000000000000000000000000000000000006"

# Pools from the latest discovery scan (line 23) with score > 50
POOLS = [
    {
        "name": "ElonRWA / WETH 0.01%",
        "address": "0xd56f086e7b796b313d49f2bc926fac4bdd2a2b0b",
        "token_addr": "0xaa6cccdce193698d33deb9ffd4be74eaa74c4898",
        "quote": "WETH",
        "score": 64,
        "source": "gecko_trending",
    },
    {
        "name": "DRB / WETH 1%",
        "address": "0x5116773e18a9c7bb03ebb961b38678e45e238923",
        "token_addr": "0x3ec2156d4c0a9cbdab4a016633b7bcf6a8d68ea2",
        "quote": "WETH",
        "score": 61,
        "source": "gecko_trending",
    },
]

TOKEN0_SEL = "0x0dfe1681"
TOKEN1_SEL = "0xd21220a7"


def rpc(method, params):
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode()
    req = urllib.request.Request(
        RPC,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (base-trend-bot/rpc-verify)",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read().decode())
    if "error" in resp:
        raise RuntimeError(resp["error"])
    return resp["result"]


def addr_from_hex(hexval):
    # strip 0x, take last 40 hex chars (20 bytes)
    h = hexval[2:] if hexval.startswith("0x") else hexval
    return "0x" + h[-40:].lower()


def verify(pool):
    out = {"verified": False, "rpc": RPC}
    code = rpc("eth_getCode", [pool["address"], "latest"])
    out["pool_exists"] = code not in ("0x", "0x0", None)
    out["pool_codesize"] = max(0, (len(code) - 2) // 2)
    if not out["pool_exists"]:
        return out

    t0 = rpc("eth_call", [{"to": pool["address"], "data": TOKEN0_SEL}, "latest"])
    t1 = rpc("eth_call", [{"to": pool["address"], "data": TOKEN1_SEL}, "latest"])
    out["token0"] = addr_from_hex(t0)
    out["token1"] = addr_from_hex(t1)
    expected = {WETH.lower(), pool["token_addr"].lower()}
    actual = {out["token0"], out["token1"]}
    out["pair_match"] = expected == actual
    out["verified"] = out["pool_exists"] and out["pair_match"]
    return out


# Ordered best-first. Verified live 2026-08-28: publicnode + 1rpc handle bursts
# without 429; mainnet.base.org + meowrpc rate-limit (~5/12 calls). ankr now
# requires an API key (-32000 Unauthorized) and llamarpc returns 521, so both
# are dropped. RpcClient.call retries 429s before failing over.
RPC_ENDPOINTS = [
    "https://base-rpc.publicnode.com",
    "https://1rpc.io/base",
    "https://mainnet.base.org",
    "https://base.meowrpc.com",
]


class RpcClient:
    """Minimal stdlib JSON-RPC client with multi-endpoint failover.

    Public Base RPCs rate-limit (HTTP 429) under burst load. A single 429 must
    NOT kill the endpoint: we retry it with backoff before failing over, and we
    space calls slightly so the gate survives verifying several pools in a loop.
    """
    # 429 retry config
    RPC_429_RETRIES = 4
    RPC_429_BACKOFF = 1.5   # seconds, multiplied by attempt
    RPC_CALL_DELAY = 0.4    # spacing between sequential calls to avoid 429

    def __init__(self, endpoints=None, timeout=30):
        self.endpoints = list(endpoints) if endpoints else list(RPC_ENDPOINTS)
        self.timeout = timeout
        self.last_used = None

    @staticmethod
    def _is_429(e):
        return isinstance(e, urllib.error.HTTPError) and e.code == 429

    def call(self, method, params):
        last_err = None
        for url in self.endpoints:
            try:
                payload = json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
                ).encode()
                # Retry 429s against this same endpoint before failing over.
                for attempt in range(self.RPC_429_RETRIES + 1):
                    try:
                        req = urllib.request.Request(
                            url,
                            data=payload,
                            headers={
                                "Content-Type": "application/json",
                                "User-Agent": "Mozilla/5.0 (base-trend-bot/rpc-verify)",
                                "Accept": "application/json",
                            },
                        )
                        with urllib.request.urlopen(req, timeout=self.timeout) as r:
                            resp = json.loads(r.read().decode())
                        if resp.get("error"):
                            raise RuntimeError(resp["error"])
                        self.last_used = url
                        return resp["result"]
                    except Exception as e:
                        last_err = e
                        if self._is_429(e) and attempt < self.RPC_429_RETRIES:
                            time.sleep(self.RPC_429_BACKOFF * (attempt + 1))
                            continue
                        break
            except Exception as e:
                last_err = e
            # small spacing so the next endpoint / next call isn't 429'd instantly
            time.sleep(self.RPC_CALL_DELAY)
        raise RuntimeError(f"all RPC endpoints failed: {last_err}")

    def get_code(self, addr):
        return self.call("eth_getCode", [addr, "latest"])

    def call_selector(self, addr, selector):
        return self.call("eth_call", [{"to": addr, "data": selector}, "latest"])


def verify_pool(rpc_client, pool_addr, weth, token_addr):
    """Verify a pool exists and holds the expected WETH+token pair.

    Returns dict: verified(bool), reason(str), codesize(int),
    token0(str), token1(str).
    """
    out = {"verified": False, "reason": None, "codesize": 0,
           "token0": None, "token1": None}
    try:
        code = rpc_client.get_code(pool_addr)
    except Exception as e:
        out["reason"] = f"eth_getCode failed: {e}"
        return out
    if code in ("0x", "0x0", None, ""):
        out["reason"] = "pool contract has no code (eth_getCode empty)"
        return out
    out["codesize"] = max(0, (len(code) - 2) // 2)
    try:
        t0 = rpc_client.call_selector(pool_addr, TOKEN0_SEL)
        t1 = rpc_client.call_selector(pool_addr, TOKEN1_SEL)
    except Exception as e:
        out["reason"] = f"token0/token1 read failed: {e}"
        return out
    token0 = addr_from_hex(t0)
    token1 = addr_from_hex(t1)
    out["token0"] = token0
    out["token1"] = token1
    expected = {weth.lower(), token_addr.lower()}
    actual = {token0.lower(), token1.lower()}
    if expected == actual:
        out["verified"] = True
        out["reason"] = "pool exists; token0/token1 match WETH+token pair"
    else:
        out["reason"] = f"pair mismatch: expected {sorted(expected)} got {sorted(actual)}"
    return out


def main():
    results = []
    for i, p in enumerate(POOLS):
        if i > 0:
            time.sleep(1.5)
        v = verify(p)
        p["onchain"] = v
        results.append(p)
        print(
            f"{p['name']:24s} exists={v.get('pool_exists')} "
            f"codesize={v.get('pool_codesize')} pair_match={v.get('pair_match')} "
            f"verified={v.get('verified')}"
        )
        print(f"    token0={v.get('token0')}")
        print(f"    token1={v.get('token1')}")
    # Append verified records to discovered.jsonl
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(time.time()*1000)%1000:03d}Z",
        "verified_by": "rpc_verify",
        "coins": [
            {
                "name": p["name"],
                "address": p["address"],
                "token_addr": p["token_addr"],
                "quote": p["quote"],
                "score": p["score"],
                "source": p["source"],
                "onchain": p["onchain"],
            }
            for p in results
        ],
    }
    atomic_jsonl_append("discovered.jsonl", rec)
    print("Wrote verified records to discovered.jsonl")


if __name__ == "__main__":
    main()
