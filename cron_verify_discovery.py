#!/usr/bin/env python3
"""Base-research cron: verify this run's discovered coins (score>50) on-chain.

Reads the most recent discovery record in discovered.jsonl, filters coins with
score > 50, and verifies each via Base RPC:
  - pool contract exists (eth_getCode)
  - token0()/token1() match the expected {WETH, token_addr} pair
Writes a single verification record back to discovered.jsonl.

Stdlib only. 1.5s rate limit between RPC calls.
"""
import json
import time
import urllib.request

RPCS = [
    "https://mainnet.base.org",
    "https://base-rpc.publicnode.com",
    "https://base.llamarpc.com",
]
WETH = "0x4200000000000000000000000000000000000006"
SCORE_MIN = 50
DISCOVERY_FILE = "discovered.jsonl"


def rpc_call(rpc, method, params, timeout=15):
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode()
    req = urllib.request.Request(
        rpc,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "base-trend-bot/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.loads(r.read().decode())
    if "error" in resp:
        raise RuntimeError(str(resp["error"]))
    return resp["result"]


def get_code_size(rpc, addr):
    code = rpc_call(rpc, "eth_getCode", [addr, "latest"])
    if not code or code == "0x":
        return 0
    return len(code) // 2 - 1  # hex chars / 2, minus 0x prefix


def get_token(rpc, pool, selector):
    res = rpc_call(rpc, "eth_call", [{"to": pool, "data": selector}, "latest"])
    if not res or res == "0x":
        return None
    return "0x" + res[26:]  # last 20 bytes


def verify(coin):
    last_err = None
    for rpc in RPCS:
        try:
            cs = get_code_size(rpc, coin["address"])
            if cs == 0:
                continue  # pool not deployed on this RPC
            t0 = get_token(rpc, coin["address"], "0x0dfe1681")  # token0()
            t1 = get_token(rpc, coin["address"], "0xd21220a7")  # token1()
            toks = {t0.lower(), t1.lower()}
            expected = {WETH.lower(), coin["token_addr"].lower()}
            pair_match = toks == expected
            return {
                "verified": cs > 0,
                "rpc": rpc,
                "pool_exists": cs > 0,
                "pool_codesize": cs,
                "token0": t0,
                "token1": t1,
                "pair_match": pair_match,
                "expected_token0": WETH,
                "expected_token1": coin["token_addr"],
            }
        except Exception as e:
            last_err = str(e)
            continue
    return {"verified": False, "error": last_err, "pair_match": False}


def main():
    # Find the latest discovery record (has "coins" and a "ts", no "verified_by")
    last_disc = None
    with open(DISCOVERY_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if "coins" in rec and "verified_by" not in rec:
                last_disc = rec
    if not last_disc:
        print("No discovery record found.")
        return

    coins = last_disc.get("coins", [])
    pending = [c for c in coins if c.get("score", 0) > SCORE_MIN]
    print(
        f"Latest discovery ts={last_disc.get('ts')} -> "
        f"{len(coins)} coins, {len(pending)} with score>{SCORE_MIN}"
    )

    out = []
    for c in pending:
        time.sleep(1.5)  # rate limit between RPC calls
        oc = verify(c)
        rec = {
            "name": c.get("name"),
            "address": c.get("address"),
            "token_addr": c.get("token_addr"),
            "quote": "WETH",
            "score": c.get("score"),
            "source": c.get("source"),
            "onchain": oc,
        }
        out.append(rec)
        print(
            f"  {c.get('name'):22} score={c.get('score'):3} "
            f"verified={oc.get('verified')} pair_match={oc.get('pair_match')} "
            f"codesize={oc.get('pool_codesize')} rpc={oc.get('rpc','')}"
        )

    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "verified_by": "cron_verify_discovery",
        "source_ts": last_disc.get("ts"),
        "coins": out,
    }
    with open(DISCOVERY_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")
    print(f"Wrote verification record ({len(out)} coins) to {DISCOVERY_FILE}")


if __name__ == "__main__":
    main()
