#!/usr/bin/env python3
"""Cron discovery follow-up: on-chain verify unverified high-score Base pools.

- Reads discovered.jsonl
- Finds unique pools with score>50 lacking a verified_by rpc_verify entry
- Verifies each via eth_getCode + token0/token1 (multi-endpoint failover, UA header)
- Appends a canonical verified_by entry to discovered.jsonl
Stdlib only.
"""
import json
import time
import urllib.request
import urllib.error
from _atomic_write import atomic_jsonl_append
from _tolerant_read import iter_jsonl_entries

WETH = "0x4200000000000000000000000000000000000006".lower()
TOKEN0_SEL = "0x0dfe1681"
TOKEN1_SEL = "0xd21220a7"

ENDPOINTS = [
    "https://mainnet.base.org",
    "https://base.meowrpc.com",
    "https://base.drpc.org",
    "https://1rpc.io/base",
    "https://rpc.ankr.com/base",
]

UA = {"Content-Type": "application/json",
      "User-Agent": "Mozilla/5.0 (base-trend-bot/rpc-verify)",
      "Accept": "application/json"}


def rpc_call(method, params, timeout=12):
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    last = None
    for url in ENDPOINTS:
        try:
            req = urllib.request.Request(url, data=payload, headers=UA, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode())
            if "error" in data and data["error"]:
                last = data["error"]
                continue
            return data.get("result")
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:80]}"
            continue
    raise RuntimeError(f"all RPC endpoints failed: {last}")


def addr_from_hex(hexval):
    h = hexval[2:] if hexval and hexval.startswith("0x") else (hexval or "")
    return "0x" + h[-40:].lower()


def verify(pool):
    out = {"verified": False, "rpc": "multi-endpoint-failover"}
    code = rpc_call("eth_getCode", [pool["address"], "latest"])
    out["pool_exists"] = code not in ("0x", "0x0", None, "")
    out["pool_codesize"] = max(0, (len(code) - 2) // 2) if code else 0
    if not out["pool_exists"]:
        return out
    t0 = rpc_call("eth_call", [{"to": pool["address"], "data": TOKEN0_SEL}, "latest"])
    t1 = rpc_call("eth_call", [{"to": pool["address"], "data": TOKEN1_SEL}, "latest"])
    out["token0"] = addr_from_hex(t0)
    out["token1"] = addr_from_hex(t1)
    expected = {WETH, pool["token_addr"].lower()}
    actual = {out["token0"], out["token1"]}
    out["pair_match"] = (expected == actual)
    out["verified"] = out["pool_exists"] and out["pair_match"]
    return out


def main():
    path = "discovered.jsonl"
    rows = []
    for rec in iter_jsonl_entries(path):
        rows.append(rec)

    # Build set of already-verified addresses from verified_by entries
    verified_addrs = set()
    for r in rows:
        if r.get("verified_by") == "rpc_verify":
            for c in r.get("coins", []):
                oc = c.get("onchain", {})
                if oc.get("verified") is True and oc.get("pair_match") is True:
                    verified_addrs.add(c.get("address", "").lower())

    # Collect unique high-score (score>50) pools needing verification
    pending = {}
    for r in rows:
        for c in r.get("coins", []):
            if not isinstance(c, dict):
                continue
            sc = c.get("score")
            if isinstance(sc, (int, float)) and sc > 50:
                addr = c.get("address")
                if not addr:
                    continue
                if addr.lower() in verified_addrs:
                    continue
                if addr.lower() in pending:
                    # keep highest score seen
                    if sc > pending[addr.lower()]["score"]:
                        pending[addr.lower()]["score"] = sc
                    continue
                pending[addr.lower()] = {
                    "name": c.get("name"),
                    "address": addr,
                    "token_addr": c.get("token_addr"),
                    "quote": "WETH",
                    "score": sc,
                    "source": c.get("source", "gecko_trending"),
                }

    print(f"Total discovered rows: {len(rows)}")
    print(f"Already verified_by rpc_verify: {len(verified_addrs)} pools")
    print(f"Pending high-score unverified pools: {len(pending)}")
    for p in pending.values():
        print(f"  - {p['name']} score={p['score']} addr={p['address']}")

    if not pending:
        print("Nothing to verify.")
        return

    results = []
    for p in pending.values():
        try:
            oc = verify(p)
            entry = {
                "name": p["name"],
                "address": p["address"],
                "token_addr": p["token_addr"],
                "quote": p["quote"],
                "score": p["score"],
                "source": p["source"],
                "onchain": oc,
            }
            results.append(entry)
            print(f"  VERIFY {p['name']}: exists={oc['pool_exists']} "
                  f"codesize={oc['pool_codesize']} pair_match={oc['pair_match']} "
                  f"verified={oc['verified']}")
        except Exception as e:
            print(f"  FAIL {p['name']}: {e}")
        time.sleep(0.4)  # small spacing between pools (RPC, not GeckoTerminal)

    # Append verified_by entries
    if results:
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S.%f+00:00", time.gmtime()),
            "verified_by": "rpc_verify",
            "coins": results,
        }
        atomic_jsonl_append(path, rec)
        print(f"\nAppended {len(results)} verified entries to discovered.jsonl")

    print("\n=== SUMMARY ===")
    print(f"New pools verified this run: {len(results)}")
    for e in results:
        oc = e["onchain"]
        status = "PASS" if oc["verified"] else "REJECT"
        print(f"  [{status}] {e['name']} (score {e['score']}): "
              f"pool_exists={oc['pool_exists']} pair_match={oc['pair_match']}")


if __name__ == "__main__":
    main()
