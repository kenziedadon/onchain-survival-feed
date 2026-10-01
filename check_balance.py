#!/usr/bin/env python3
import json, urllib.request

rpc = 'https://mainnet.base.org'
payload = json.dumps({
    'jsonrpc':'2.0',
    'method':'eth_getBalance',
    'params':['0xe7a16Be2aF1eCE8922d9d3aD55C03e29004e540F','latest'],
    'id':1
}).encode()

req = urllib.request.Request(rpc, data=payload, headers={'Content-Type':'application/json'})
try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read())
        bal_hex = result.get('result','0x0')
        bal_eth = int(bal_hex, 16) / 1e18
        print(f'ONCHAIN_BALANCE_ETH={bal_eth:.6f}')
except Exception as e:
    print(f'BALANCE_CHECK_ERROR={e}')
