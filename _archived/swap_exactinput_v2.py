#!/usr/bin/env python3
"""swap_exactinput_v2.py — use Universal Router's exactInput with correct ABI"""
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

    print('=== Universal Router exactInput ===\n')

    WETH = w3.to_checksum_address('0x4200000000000000000000000000000000000006')
    USDC = w3.to_checksum_address('0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913')
    ROUTER = w3.to_checksum_address('0x2626664c2603336E57B271c5C0b26F421741e481')

    # Universal Router ABI - exactInput with bytes path
    # exactInput((bytes path, address recipient, uint256 deadline, uint256 amountIn, uint256 amountOutMin))
    exactInput_abi = [
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

    # Path: WETH + fee (3000 = 0bb8) + USDC = 20 bytes + 3 bytes + 20 bytes = 43 bytes
    fee = 3000
    path = bytes.fromhex(WETH[2:]) + fee.to_bytes(3, 'big') + bytes.fromhex(USDC[2:])

    print(f'Path: {path.hex()}')
    print(f'Path length: {len(path)} bytes')

    router = w3.eth.contract(address=ROUTER, abi=exactInput_abi)

    deadline = w3.eth.get_block('latest')['timestamp'] + 300
    amount_in = w3.to_wei(0.0001, 'ether')
    amount_out_min = 0

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

        print(f'Tx built. Estimating gas...')
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
