#!/usr/bin/env python3
"""swap_high_liquidity.py — swap high-liquidity pairs on Base via Uniswap V3"""
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

    print('=== High-Liquidity Swap on Base ===\n')

    WETH = w3.to_checksum_address('0x4200000000000000000000000000000000000006')
    USDC = w3.to_checksum_address('0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913')

    # Uniswap V3 Factory
    UNIV3_FACTORY = w3.to_checksum_address('0x33128a8fc17869897dce68ed026d694621f6FDfD')

    # Find WETH/USDC pool at 0.3%
    getPoolData = '0x1698ee82' + WETH[2:] + USDC[2:] + '0000000000000000000000000000000000000000000000000000000000000bb8'

    result = w3.eth.call({'to': UNIV3_FACTORY, 'data': getPoolData})
    POOL = '0x' + result[26:66]
    print(f'WETH/USDC 0.3% pool: {POOL}')

    # Check pool code
    pool_code = w3.eth.get_code(POOL)
    print(f'Pool code: {len(pool_code)} bytes')

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

    # Check slot0 to determine token ordering
    slot0 = w3.eth.call({'to': POOL, 'data': '0x3850c7bd'})
    sqrt_price_x96 = int(slot0.hex()[2:66], 16)
    tick = int(slot0.hex()[66:130], 16)
    print(f'sqrtPriceX96: {sqrt_price_x96}')
    print(f'tick: {tick}')

    # token0 and token1
    token0_result = w3.eth.call({'to': POOL, 'data': '0x02e95bed'})
    token0 = '0x' + token0_result.hex()[26:66]
    print(f'token0: {token0}')

    token1_result = w3.eth.call({'to': POOL, 'data': '0xd21220a7'})
    token1 = '0x' + token1_result.hex()[26:66]
    print(f'token1: {token1}')

    # zeroForOne: True if token0 < token1 and we're selling token0
    zero_for_one = True if token0.lower() < token1.lower() else False
    print(f'zeroForOne (WETH->USDC): {zero_for_one}')

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
            print('\n✅ HIGH-LIQUIDITY SWAP SUCCESS!')
        else:
            print('\n❌ Reverted')

    except Exception as e:
        print(f'\nFailed: {e}')
