#!/usr/bin/env python3
"""Verify the new high-score coin(s) from this scan independently via Base RPC.
Reuses the multi-endpoint RpcClient + verify_pool from rpc_verify.py.
"""
import json, time, sys, datetime
from rpc_verify import RpcClient, verify_pool, WETH

# New coins from this scan run with score > 50 (DRB/WETH).
NEW_COINS = [
    {
        "name": "DRB / WETH 1%",
        "pool": "0x5116773e18a9c7bb03ebb961b38678e45e238923",
        "token_addr": "0x3ec2156d4c0a9cbdab4a016633b7bcf6a8d68ea2",
        "score": 61,
        "source": "gecko_trending",
    },
]

def main():
    rc = RpcClient()
    results = []
    for i, c in enumerate(NEW_COINS):
        if i > 0:
            time.sleep(1.5)
        v = verify_pool(rc, c["pool"], WETH, c["token_addr"])
        rec = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "name": c["name"],
            "pool": c["pool"],
            "token_addr": c["token_addr"],
            "score": c["score"],
            "source": c["source"],
            "onchain": {
                "verified": v["verified"],
                "reason": v["reason"],
                "codesize": v["codesize"],
                "token0": v["token0"],
                "token1": v["token1"],
            },
        }
        results.append(rec)
        print(f"  {c['name']:22s} verified={v['verified']} code={v['codesize']}B")
        print(f"      token0={v['token0']}")
        print(f"      token1={v['token1']}")
        print(f"      reason={v['reason']}")
    # Append to discovered.jsonl as a verification record
    with open("discovered.jsonl", "a") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")
    print(f"\nWrote {len(results)} verification record(s) to discovered.jsonl")
    return results

if __name__ == "__main__":
    main()
