#!/usr/bin/env python3
"""swap_v3_pool.py — swap directly on WETH/USDC Uniswap V3 pool"""
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

    print('=== V3 Pool Direct Swap ===\n')

    # Known WETH/USDC 0.3% pool on Base (from GeckoTerminal)
    POOL = w3.to_checksum_address('0x6c561b446416e1a00e8e93e221854d6ea4171372')

    print(f'Pool: {POOL}')

    # Check pool code
    pool_code = w3.eth.get_code(POOL)
    print(f'Pool code: {len(pool_code)} bytes')

    # Check slot0
    slot0 = w3.eth.call({'to': POOL, 'data': '0x3850c7bd'})
    sqrt_price_x96 = int(slot0.hex()[2:66], 16)
    tick = int(slot0.hex()[66:130], 16) & 0xffffffffffff  # mask to 48 bits
    if tick > 0x7fffffffffffffff:
        tick -= 0x10000000000000000
    print(f'sqrtPriceX96: {sqrt_price_x96}')
    print(f'tick: {tick}')

    # token0 and token1
    token0_result = w3.eth.call({'to': POOL, 'data': '0x02e95bed'})
    token0 = '0x' + token0_result.hex()[26:66]
    print(f'token0: {token0}')

    token1_result = w3.eth.call({'to': POOL, 'data': '0xd21220a7'})
    token1 = '0x' + token1_result.hex()[26:66]
    print(f'token1: {token1}')

    # Pool ABI - swap function
    pool_abi = [
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

    pool = w3.eth.contract(address=POOL, abi=pool_abi)

    amount_in = w3.to_wei(0.0001, 'ether')

    # zeroForTrue if we're selling token0 (WETH)
    zero_for_one = token0.lower() == '0x4200000000000000000000000000000000000006'
    print(f'zeroForOne: {zero_for_one}')

    try:
        # Try swap
        tx = pool.functions.swap(
            account.address,
            zero_for_one,
            amount_in,  # positive = exact input
            0,  # sqrtPriceLimitX96 (0 = no limit)
            b''  # data (empty for simple swap)
        ).build_transaction({
            'from': account.address,
            'value': amount_in,
            'nonce': w3.eth.get_transaction_count(account.address),
            'chainId': CHAIN_ID,
            'gasPrice': w3.eth.gas_price,
        })

        print(f'\nEstimating gas...')
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
            print('\n✅ DIRECT POOL SWAP SUCCESS!')
        else:
            print('\n❌ Reverted')

    except Exception as e:
        print(f'\nFailed: {e}')
