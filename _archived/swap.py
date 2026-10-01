#!/usr/bin/env python3
"""swap.py — execute swap on Base using web3.py"""
from eth_account import Account
from web3 import Web3
import json, os

RPC = 'https://mainnet.base.org'
CHAIN_ID = 8453

# Keys
KEY_FILE = '/home/kenzodadon/onchain-survival/.payer_key_tmp'
UNIV3_ROUTER = '0x2626664c2603336E57B271c5C0b26F421741e481'
WETH = '0x4200000000000000000000000000000000000006'
USDC = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'

# Read key
if __name__ == "__main__":
    with open(KEY_FILE) as f:
        key = f.read().strip()

    account = Account.from_key(key)
    print(f'Address: {account.address}')
    print(f'Match: {account.address.lower() == "0xe7a16be2af1ece8922d9d3ad55c03e29004e540f"}')

    w3 = Web3(Web3.HTTPProvider(RPC))

    # Check connection
    print(f'Chain ID: {w3.eth.chain_id}')
    print(f'Balance: {w3.from_wei(w3.eth.get_balance(account.address), "ether")} ETH')
    print(f'Nonce: {w3.eth.get_transaction_count(account.address)}')

    # Uniswap V3 SwapRouter ABI (exactInputSingle)
    # Function: exactInputSingle((address,address,uint24,address,uint256,uint256,uint256,uint160))
    # Selector: 0x414bf389
    ROUTER_ABI = [
        {
            "inputs": [
                {
                    "components": [
                        {"name": "tokenIn", "type": "address"},
                        {"name": "tokenOut", "type": "address"},
                        {"name": "fee", "type": "uint24"},
                        {"name": "recipient", "type": "address"},
                        {"name": "deadline", "type": "uint256"},
                        {"name": "amountIn", "type": "uint256"},
                        {"name": "amountOutMinimum", "type": "uint256"},
                        {"name": "sqrtPriceLimitX96", "type": "uint160"}
                    ],
                    "name": "params",
                    "type": "tuple"
                }
            ],
            "name": "exactInputSingle",
            "outputs": [{"name": "amountOut", "type": "uint256"}],
            "stateMutability": "payable",
            "type": "function"
        }
    ]

    # Create contract
    router = w3.eth.contract(address=UNIV3_ROUTER, abi=ROUTER_ABI)

    # Build swap params
    amount_in = w3.to_wei(0.0001, 'ether')
    deadline = w3.eth.get_block('latest')['timestamp'] + 300

    for fee in [500, 3000, 10000]:
        print(f'\n--- Fee: {fee/100}% ---')

        try:
            # Build transaction
            tx = router.functions.exactInputSingle(
                (WETH, USDC, fee, account.address, deadline, amount_in, 0, 0)
            ).build_transaction({
                'from': account.address,
                'value': amount_in,
                'nonce': w3.eth.get_transaction_count(account.address),
                'chainId': CHAIN_ID,
            })

            # Estimate gas
            try:
                gas = w3.eth.estimate_gas(tx)
                tx['gas'] = gas
                print(f'Gas: {gas}')
            except Exception as e:
                print(f'Gas estimate failed: {e}')
                continue

            # Gas price
            tx['gasPrice'] = w3.eth.gas_price

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
                break
            else:
                print('❌ Reverted')

        except Exception as e:
            print(f'Failed: {e}')
