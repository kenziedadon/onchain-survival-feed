import urllib.request
import json
import time

# Latest discovery from line 113
pool_address = "0xc211e1f853a898bd1302385ccde55f33a8c4b3f3"
token_address = "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf"
name = "cbBTC / WETH 0.01%"
score = 93

# Base RPC endpoints
RPC_URLS = [
    "https://mainnet.base.org",
    "https://base-rpc.publicnode.com",
    "https://base.drpc.org",
]

# WETH on Base
WETH = "0x4200000000000000000000000000000000000006"

def rpc_call(method, params, rpc_url):
    payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
    data = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    req = urllib.request.Request(rpc_url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}

# Try each RPC until one works
result = None
for rpc in RPC_URLS:
    # 1. Check pool contract exists (eth_getCode)
    code_resp = rpc_call("eth_getCode", [pool_address, "latest"], rpc)
    if "error" not in code_resp and code_resp.get("result") and code_resp["result"] != "0x":
        codesize = len(code_resp["result"]) // 2 - 1
        print(f"Pool contract exists: codesize={codesize} bytes")
        
        # 2. Check token0 and token1
        # Uniswap V3 pool: token0() and token1() are view functions
        # token0() selector: 0x0dfe1681
        # token1() selector: 0xd21220a7
        token0_resp = rpc_call("eth_call", [{"to": pool_address, "data": "0x0dfe1681"}, "latest"], rpc)
        token1_resp = rpc_call("eth_call", [{"to": pool_address, "data": "0xd21220a7"}, "latest"], rpc)
        
        if "error" not in token0_resp and "result" in token0_resp:
            token0 = "0x" + token0_resp["result"][-40:]
        else:
            token0 = "ERROR"
            
        if "error" not in token1_resp and "result" in token1_resp:
            token1 = "0x" + token1_resp["result"][-40:]
        else:
            token1 = "ERROR"
        
        print(f"token0: {token0}")
        print(f"token1: {token1}")
        print(f"Expected token0 (WETH): {WETH}")
        print(f"Expected token1 (cbBTC): {token_address}")
        
        pair_match = (token0.lower() == WETH.lower() and token1.lower() == token_address.lower()) or \
                     (token1.lower() == WETH.lower() and token0.lower() == token_address.lower())
        
        print(f"Pair match: {pair_match}")
        
        result = {
            "verified": True,
            "rpc": rpc,
            "pool_exists": True,
            "pool_codesize": codesize,
            "token0": token0,
            "token1": token1,
            "pair_match": pair_match,
            "expected_token0": WETH,
            "expected_token1": token_address
        }
        break
    else:
        print(f"RPC {rpc} failed: {code_resp.get('error', 'no code')}")

if not result:
    result = {"verified": False, "error": "All RPCs failed"}

print("\nVerification result:")
print(json.dumps(result, indent=2))

# Append to discovered.jsonl
verify_entry = {
    "ts": time.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    "verified_by": "rpc_verify_cron",
    "rpc": result.get("rpc", "unknown"),
    "coins": [{
        "name": name,
        "address": pool_address,
        "token_addr": token_address,
        "quote": "WETH",
        "score": score,
        "source": "gecko_trending",
        "onchain": result
    }]
}

with open("/home/kenzodadon/base-trend-bot/discovered.jsonl", "a") as f:
    f.write(json.dumps(verify_entry) + "\n")

print("\nAppended verification to discovered.jsonl")