#!/usr/bin/env python3
"""swap_1inch.py — swap using 1inch aggregator on Base"""
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

    # 1inch API endpoint
    oneinch_url = f'https://api.1inch.io/v5.0/{CHAIN_ID}/swap?fromTokenAddress={WETH}&toTokenAddress={USDC}&amount=100000000000000&fromAddress={account.address}&slippage=1'

    print('Requesting quote from 1inch...')

    try:
        req = urllib.request.Request(oneinch_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as r:
            quote = json.loads(r.read())
    
        print(f'Quote: {json.dumps(quote, indent=2)[:1000]}')
    
        # Get tx data
        tx_data = quote['tx']
    
        # Build transaction
        tx = {
            'to': w3.to_checksum_address(tx_data['to']),
            'from': account.address,
            'value': int(tx_data['value'], 16) if tx_data['value'].startswith('0x') else int(tx_data['value']),
            'data': tx_data['data'],
            'gas': int(tx_data['gas'], 16) if tx_data['gas'].startswith('0x') else int(tx_data['gas']),
            'gasPrice': w3.eth.gas_price,
            'nonce': w3.eth.get_transaction_count(account.address),
            'chainId': CHAIN_ID,
        }
    
        print(f'\nGas: {tx["gas"]}')
    
        # Sign
        signed = account.sign_transaction(tx)
    
        # Broadcast
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        print(f'Tx: {tx_hash.hex()}')
    
        # Wait
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        print(f'Status: {receipt.status}')
        print(f'Block: {receipt.blockNumber}')
    
        if receipt.status == 1:
            print('\n✅ SWAP SUCCESS!')
        else:
            print('\n❌ Reverted')

    except Exception as e:
        print(f'Failed: {e}')
