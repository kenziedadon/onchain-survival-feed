#!/usr/bin/env python3
"""swap_via_1inch_api.py — use 1inch API to get correct swap calldata"""
import json, urllib.request
from eth_account import Account
from web3 import Web3

RPC = 'https://mainnet.base.org'
CHAIN_ID = 8453

KEY_FILE = '/home/kenzodadon/onchain-survival/.payer_key_tmp'

if __name__ == "__main__":
    with open(KEY_FILE) as f:
        key = f.read().strip()
    account = Account.from_key(key)

    w3 = Web3(Web3.HTTPProvider(RPC))

    print('=== 1inch Swap on Base ===\n')

    WETH = '0x4200000000000000000000000000000000000006'
    USDC = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'

    # 1inch API endpoint for Base
    # Get quote first
    oneinch_quote_url = f'https://api.1inch.dev/swap/v5.2/{CHAIN_ID}/quote?fromTokenAddress={WETH}&toTokenAddress={USDC}&amount=100000000000000'

    # Get swap data
    oneinch_swap_url = f'https://api.1inch.dev/swap/v5.2/{CHAIN_ID}/swap?fromTokenAddress={WETH}&toTokenAddress={USDC}&amount=100000000000000&fromAddress={account.address}&slippage=1&disableEstimate=true'

    print('Requesting 1inch quote...')

    try:
        # Need API key
        req = urllib.request.Request(oneinch_quote_url, headers={
            'User-Agent': 'Mozilla/5.0',
            'Authorization': 'Bearer YOUR_API_KEY',  # Need API key
        })
    
        with urllib.request.urlopen(req, timeout=15) as r:
            quote = json.loads(r.read())
    
        print(f'Quote received: {json.dumps(quote, indent=2)[:500]}')
    
    except Exception as e:
        print(f'Quote failed (expected - need API key): {e}')

    # Try without API key
    print('\nTrying without API key...')
    try:
        req = urllib.request.Request(oneinch_quote_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=15) as r:
            quote = json.loads(r.read())
        print(f'Quote: {json.dumps(quote, indent=2)[:500]}')
    except Exception as e:
        print(f'Also failed: {e}')

    # Alternative: use 0x API
    print('\n=== Trying 0x API ===')
    zeroex_url = f'https://base.api.0x.org/swap/v1/quote?sellToken={WETH}&buyToken={USDC}&sellAmount=100000000000000'

    try:
        req = urllib.request.Request(zeroex_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=15) as r:
            quote = json.loads(r.read())
    
        print(f'Quote: {json.dumps(quote, indent=2)[:1000]}')
    
    except Exception as e:
        print(f'0x API failed: {e}')
