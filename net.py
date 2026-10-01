#!/usr/bin/env python3
"""
net.py — lazy web3 + account factory for swap modules.

Importing this module does NOT hit the network or read key files.
Call get_w3() / get_account() inside functions or under __main__ guards.
"""

RPC = 'https://1rpc.io/base'
CHAIN_ID = 8453
KEY_FILE = '/home/kenzodadon/onchain-survival/.payer_key_tmp'

_w3 = None
_account = None
_key = None


def get_w3():
    """Lazy Web3 instance — first call hits RPC, subsequent calls reuse."""
    global _w3
    if _w3 is None:
        from web3 import Web3
        _w3 = Web3(Web3.HTTPProvider(RPC))
    return _w3


def get_account():
    """Lazy account from key file — first call reads KEY_FILE, subsequent reuse."""
    global _account, _key
    if _account is None:
        from eth_account import Account
        with open(KEY_FILE) as f:
            _key = f.read().strip()
        _account = Account.from_key(_key)
    return _account


def get_key():
    """Return the raw private key string (triggers account load if needed)."""
    if _key is None:
        get_account()
    return _key


def reset():
    """Clear cached instances — useful for tests."""
    global _w3, _account, _key
    _w3 = None
    _account = None
    _key = None
