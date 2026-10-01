#!/usr/bin/env python3
import json, urllib.request

RPC_FALLBACKS = [
    "https://mainnet.base.org",
    "https://base.llamarpc.com",
    "https://base.meowrpc.com",
    "https://base.drpc.org",
    "https://1rpc.io/base",
    "https://base-pokt.nodies.app",
    "https://rpc.ankr.com/base",
]

UA = {"Content-Type": "application/json"}
payload = json.dumps({
    "jsonrpc": "2.0",
    "method": "eth_getBalance",
    "params": ["0xe7a16Be2aF1eCE8922d9d3aD55C03e29004e540F", "latest"],
    "id": 1,
}).encode()

for url in RPC_FALLBACKS:
    try:
        req = urllib.request.Request(url, data=payload, headers=UA)
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read()).get("result", "0x0")
            bal_eth = int(result, 16) / 1e18
            print(f"RPC_OK={url}")
            print(f"ONCHAIN_BALANCE_ETH={bal_eth:.6f}")
            break
    except Exception as e:
        print(f"RPC_FAIL={url}: {type(e).__name__}")
else:
    print("ALL_RPCS_FAILED")
