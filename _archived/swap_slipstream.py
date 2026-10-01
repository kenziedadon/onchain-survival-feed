#!/usr/bin/env python3
"""swap_slipstream.py — try swap function on SlipStream (Aerodrome V3) router"""
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

    print('=== SlipStream (Aerodrome V3) Swap ===\n')

    WETH = w3.to_checksum_address('0x4200000000000000000000000000000000000006')
    USDC = w3.to_checksum_address('0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913')

    # SlipStream router
    SLIPSTREAM = w3.to_checksum_address('0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43')

    # SlipStream V3 Router ABI - swap function
    # swap(address,bool,int256,uint160,bytes)
    swap_abi = [
        {
            "inputs": [
                {"name": "recipient", "type": "address"},
                {"name": "zeroForOne", "type": "bool"},
                {"name": "amountSpecified", "type": "int256"},
                {"name": "sqrtPriceLimitX96", "type": "uint160"},
                {"name": "data", "type": "bytes"}
            ],
            "name": "swap",
            "outputs": [
                {"name": "amount0", "type": "int256"},
                {"name": "amount1", "type": "int256"}
            ],
            "stateMutability": "payable",
            "type": "function"
        }
    ]

    router = w3.eth.contract(address=SLIPSTREAM, abi=swap_abi)

    amount_in = w3.to_wei(0.0001, 'ether')

    # WETH -> USDC (zeroForOne = True since WETH < USDC in address ordering)
    try:
        tx = router.functions.swap(
            account.address, True, amount_in, 0, b''
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
