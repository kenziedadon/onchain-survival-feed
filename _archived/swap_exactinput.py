#!/usr/bin/env python3
"""swap_exactinput.py — swap WETH for USDC using Universal Router exactInput"""
from eth_account import Account
from web3 import Web3

RPC = 'https://mainnet.base.org'
CHAIN_ID = 8453

KEY_FILE = '/home/kenzodadon/onchain-survival/.payer_key_tmp'
UNIVERSAL_ROUTER = '0x2626664c2603336E57B271c5C0b26F421741e481'

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

    # Universal Router ABI - exactInput function
    # exactInput((bytes,address,uint256,uint256,uint256))
    # Where the tuple contains:
    # - bytes: the path (encoded as tokenIn + fee + tokenOut)
    # - address: recipient
    # - uint256: deadline
    # - uint256: amountIn
    # - uint256: amountOutMin

    ROUTER_ABI = [
        {
            "inputs": [
                {
                    "components": [
                        {"name": "path", "type": "bytes"},
                        {"name": "recipient", "type": "address"},
                        {"name": "deadline", "type": "uint256"},
                        {"name": "amountIn", "type": "uint256"},
                        {"name": "amountOutMinimum", "type": "uint256"}
                    ],
                    "name": "params",
                    "type": "tuple"
                }
            ],
            "name": "exactInput",
            "outputs": [{"name": "amountOut", "type": "uint256"}],
            "stateMutability": "payable",
            "type": "function"
        }
    ]

    router = w3.eth.contract(address=UNIVERSAL_ROUTER, abi=ROUTER_ABI)

    # Build path: WETH + fee (3000 = 0x0bb8) + USDC
    # Path format: address (20 bytes) + fee (3 bytes) + address (20 bytes) = 43 bytes
    fee = 3000  # 0.3%
    path = bytes.fromhex(WETH[2:]) + fee.to_bytes(3, 'big') + bytes.fromhex(USDC[2:])

    deadline = w3.eth.get_block('latest')['timestamp'] + 300
    amount_in = w3.to_wei(0.0001, 'ether')
    amount_out_min = 0

    print(f'\nPath: {path.hex()}')
    print(f'Amount in: {amount_in}')

    # Build transaction
    try:
        tx = router.functions.exactInput(
            (path, account.address, deadline, amount_in, amount_out_min)
        ).build_transaction({
            'from': account.address,
            'value': amount_in,
            'nonce': w3.eth.get_transaction_count(account.address),
            'chainId': CHAIN_ID,
            'gasPrice': w3.eth.gas_price,
        })

        print(f'Gas: {tx["gas"]}')

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
            print('\n✅ WETH->USDC SWAP SUCCESS!')
        else:
            print('\n❌ Reverted')

    except Exception as e:
        print(f'\n❌ Failed: {e}')
