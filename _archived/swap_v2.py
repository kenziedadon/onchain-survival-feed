#!/usr/bin/env python3
"""swap_v2.py — try swap function on Universal Router"""
from eth_account import Account
from web3 import Web3

RPC = 'https://mainnet.base.org'
CHAIN_ID = 8453

KEY_FILE = '/home/kenzodadon/onchain-survival/.payer_key_tmp'
ROUTER = '0x2626664c2603336E57B271c5C0b26F421741e481'

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
    print(f'Chain ID: {w3.eth.chain_id}')
    print(f'Balance: {w3.from_wei(w3.eth.get_balance(account.address), "ether")} ETH')

    # Try different ABIs
    ABIS = [
        # ABI 1: swap(address tokenIn, uint256 amountIn, uint256 amountOutMin, bytes data)
        [
            {
                "inputs": [
                    {"name": "tokenIn", "type": "address"},
                    {"name": "amountIn", "type": "uint256"},
                    {"name": "amountOutMin", "type": "uint256"},
                    {"name": "data", "type": "bytes"}
                ],
                "name": "swap",
                "outputs": [{"name": "amountOut", "type": "uint256"}],
                "stateMutability": "payable",
                "type": "function"
            }
        ],
        # ABI 2: exactInputSingle with tuple as array
        [
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
        ],
    ]

    # Try each ABI
    for i, abi in enumerate(ABIS):
        print(f'\n=== ABI {i+1} ===')

        try:
            router = w3.eth.contract(address=ROUTER, abi=abi)

            amount_in = w3.to_wei(0.0001, 'ether')

            if i == 0:
                # swap function
                # Path: WETH -> USDC (just addresses for this simplified function)
                path = bytes.fromhex(WETH[2:] + USDC[2:])
                tx = router.functions.swap(
                    WETH, amount_in, 0, path
                ).build_transaction({
                    'from': account.address,
                    'value': amount_in,
                    'nonce': w3.eth.get_transaction_count(account.address),
                    'chainId': CHAIN_ID,
                    'gasPrice': w3.eth.gas_price,
                })
            elif i == 1:
                # exactInputSingle
                deadline = w3.eth.get_block('latest')['timestamp'] + 300
                params = (
                    WETH, USDC, 3000, account.address, deadline, amount_in, 0, 0
                )
                tx = router.functions.exactInputSingle(
                    params
                ).build_transaction({
                    'from': account.address,
                    'value': amount_in,
                    'nonce': w3.eth.get_transaction_count(account.address),
                    'chainId': CHAIN_ID,
                    'gasPrice': w3.eth.gas_price,
                })

            print(f'Gas: {tx.get("gas", "N/A")}')

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
                print('\n❌ Reverted')

        except Exception as e:
            print(f'Failed: {e}')
