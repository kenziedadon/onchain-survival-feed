#!/usr/bin/env python3
"""swap_aerodrome_v3.py — try Aerodrome V3 router"""
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

    print('=== Aerodrome V3 (SlipStream) Swap ===\n')

    WETH = w3.to_checksum_address('0x4200000000000000000000000000000000000006')
    USDC = w3.to_checksum_address('0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913')
    AERO_V3_ROUTER = w3.to_checksum_address('0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43')

    # Aerodrome V3 Router ABI
    # swapExactETHForTokens(uint256 amountOutMin, Route[] routes, address to, uint256 deadline)
    # where Route = { address from, address to, bool stable }
    swap_abi = [
        {
            "inputs": [
                {"name": "amountOutMin", "type": "uint256"},
                {"name": "routes", "type": "tuple[]", "components": [
                    {"name": "from", "type": "address"},
                    {"name": "to", "type": "address"},
                    {"name": "stable", "type": "bool"}
                ]},
                {"name": "to", "type": "address"},
                {"name": "deadline", "type": "uint256"}
            ],
            "name": "swapExactETHForTokens",
            "outputs": [{"name": "amounts", "type": "uint256[]"}],
            "stateMutability": "payable",
            "type": "function"
        }
    ]

    deadline = w3.eth.get_block('latest')['timestamp'] + 300
    amount_in = w3.to_wei(0.0001, 'ether')

    router = w3.eth.contract(address=AERO_V3_ROUTER, abi=swap_abi)

    routes = [(WETH, USDC, False)]  # WETH -> USDC, volatile pool

    try:
        tx = router.functions.swapExactETHForTokens(
            0, routes, account.address, deadline
        ).build_transaction({
            'from': account.address,
            'value': amount_in,
            'nonce': w3.eth.get_transaction_count(account.address),
            'chainId': CHAIN_ID,
            'gasPrice': w3.eth.gas_price,
        })

        gas = w3.eth.estimate_gas(tx)
        tx['gas'] = gas
        print(f'Gas: {gas}')

        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        print(f'Tx: {tx_hash.hex()}')

        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        print(f'Status: {receipt.status}')
        print(f'Block: {receipt.blockNumber}')

        if receipt.status == 1:
            print('\n✅ SWAP SUCCESS!')
        else:
            print('\n❌ Reverted')

    except Exception as e:
        print(f'\nFailed: {e}')
