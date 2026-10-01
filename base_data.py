#!/usr/bin/env python3
"""
base_data.py — free data layer for Base (chain 8453).

Zero-cost, stdlib-only, no API keys required.
Provides RPC failover, token pricing, tx history, gas estimation,
and pair swap logs for the Hermes trading bot.

Usage:
    from base_data import BaseData
    d = BaseData()
    
    d.block_number()                 # chain tip
    d.fee_data()                     # EIP-1559 gas
    d.token_balance(USDC, holder)    # ERC-20 balance
    d.token_price_usd(WETH)          # USD price
    d.pair_swaps_v2(pair, b0, b1)   # swap logs
    d.tx_history(wallet)             # tx list

All sources are free tier. No API keys needed.
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Optional

# --- Constants ---
CHAIN_ID = 8453
CHAIN_NAME = "base"
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

# --- RPC Pool ---
# Ordered by reliability. Each gets a cooldown on failure.
RPC_POOL = [
    {"url": "https://mainnet.base.org", "name": "official", "cooldown": 0},
    {"url": "https://base-rpc.publicnode.com", "name": "publicnode", "cooldown": 0},
    {"url": "https://base.llamarpc.com", "name": "llama", "cooldown": 0},
    {"url": "https://1rpc.io/base", "name": "1rpc", "cooldown": 0},
    {"url": "https://base-pokt.nodies.app", "name": "pokt", "cooldown": 0},
    {"url": "https://rpc.ankr.com/base", "name": "ankr", "cooldown": 0},
    {"url": "https://base.drpc.org", "name": "drpc", "cooldown": 0},
    {"url": "https://base.blockpi.network/v1/rpc/public", "name": "blockpi", "cooldown": 0},
    {"url": "https://base.meowrpc.com", "name": "meow", "cooldown": 0},
]

# --- Token Lists ---
TOKEN_LISTS = {
    "usdc": USDC,
    "weth": WETH,
}

# --- UA ---
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json", "Content-Type": "application/json"}


class RPCError(Exception):
    """Raised when all RPCs fail."""
    pass


class BaseData:
    """
    Free data layer for Base chain.
    
    Methods:
        block_number() -> int
        fee_data() -> dict (base_gwei, priority_gwei, suggested_max_fee_gwei)
        token_balance(token_addr, holder_addr) -> float
        token_price_usd(token_addr) -> float
        pair_swaps_v2(pair_addr, token0, token1, from_block=None, to_block=None) -> list
        tx_history(wallet_addr, limit=50) -> list
        erc20_meta(token_addr) -> dict (name, symbol, decimals)
    """
    
    def __init__(self, rpc_pool=None, timeout=20, max_retries=3):
        self.rpc_pool = rpc_pool or RPC_POOL
        self.timeout = timeout
        self.max_retries = max_retries
        self._rpc_cooldowns = {}  # url -> timestamp when ready again
        self._last_rpc = None
    
    # --- Internal RPC ---
    
    def _rpc_call(self, method: str, params: list, use_cache=False) -> dict:
        """
        Make an RPC call with pool failover.
        Tries each endpoint in order, skipping those on cooldown.
        """
        # Check cache for common calls
        cache_key = f"{method}:{json.dumps(params, sort_keys=True)}"
        if use_cache and hasattr(self, '_cache'):
            cached = self._cache.get(cache_key)
            if cached and (time.time() - cached['ts']) < 10:
                return cached['data']
        
        # Try each RPC in order
        for rpc in self._get_rpc_candidates():
            url = rpc['url']
            
            # Skip if on cooldown
            cooldown_until = self._rpc_cooldowns.get(url, 0)
            if time.time() < cooldown_until:
                continue
            
            try:
                payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": method,
                    "params": params,
                }
                req = urllib.request.Request(
                    url,
                    data=json.dumps(payload).encode(),
                    headers=UA,
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = json.loads(r.read().decode())
                
                if "error" in data:
                    # RPC returned an error - put on short cooldown
                    self._rpc_cooldowns[url] = time.time() + 5
                    continue
                
                self._last_rpc = url
                
                # Cache result
                if use_cache:
                    if not hasattr(self, '_cache'):
                        self._cache = {}
                    self._cache[cache_key] = {'data': data, 'ts': time.time()}
                
                return data
                
            except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
                # Connection failed - put on cooldown
                status = getattr(e, 'code', 0)
                if status == 429:
                    self._rpc_cooldowns[url] = time.time() + 30  # rate limit
                elif status in (403, 401):
                    self._rpc_cooldowns[url] = time.time() + 60  # forbidden
                else:
                    self._rpc_cooldowns[url] = time.time() + 10  # generic
                continue
            except Exception:
                continue
        
        raise RPCError(f"All RPCs failed for {method}")
    
    def _get_rpc_candidates(self):
        """Get RPCs in priority order, preferring ones not on cooldown."""
        now = time.time()
        ready = []
        cooldown = []
        for rpc in self.rpc_pool:
            if now >= self._rpc_cooldowns.get(rpc['url'], 0):
                ready.append(rpc)
            else:
                cooldown.append(rpc)
        # Try ready ones first, then cooldown ones
        return ready + cooldown
    
    def _eth_call(self, to: str, data: str) -> str:
        """Make an eth_call."""
        result = self._rpc_call("eth_call", [{"to": to, "data": data}, "latest"])
        return result.get("result", "0x")
    
    def _get_logs(self, address: str, topics: list, from_block: str, to_block: str) -> list:
        """Get logs with auto-chunking for large ranges."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_getLogs",
            "params": [{
                "address": address,
                "topics": topics,
                "fromBlock": from_block,
                "toBlock": to_block,
            }],
        }
        
        for rpc in self._get_rpc_candidates():
            url = rpc['url']
            try:
                req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=UA)
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = json.loads(r.read().decode())
                if "result" in data:
                    return data["result"]
            except Exception:
                continue
        
        return []
    
    # --- Public API ---
    
    def block_number(self) -> int:
        """Get current block number."""
        result = self._rpc_call("eth_blockNumber", [])
        return int(result.get("result", "0x0"), 16)
    
    def fee_data(self) -> dict:
        """
        Get EIP-1559 fee data.
        Returns: {base_gwei, priority_gwei, suggested_max_fee_gwei}
        """
        # Get latest block for base fee
        result = self._rpc_call("eth_getBlockByNumber", ["latest", False])
        block = result.get("result", {})
        base_fee_hex = block.get("baseFeePerGas", "0x0")
        base_fee_wei = int(base_fee_hex, 16)
        base_gwei = base_fee_wei / 1e9
        
        # Estimate priority fee (maxPriorityFeePerGas)
        try:
            result = self._rpc_call("eth_maxPriorityFeePerGas", [])
            priority_wei = int(result.get("result", "0x0"), 16)
            priority_gwei = priority_wei / 1e9
        except Exception:
            priority_gwei = 0.001  # fallback
        
        # Suggested max fee = base + priority * 2 (buffer)
        suggested_max = base_gwei + (priority_gwei * 2)
        
        return {
            "base_gwei": round(base_gwei, 6),
            "priority_gwei": round(priority_gwei, 6),
            "suggested_max_fee_gwei": round(suggested_max, 6),
            "base_fee_wei": base_fee_wei,
        }
    
    def balance_eth(self, addr: str) -> float:
        """Get native ETH balance."""
        result = self._rpc_call("eth_getBalance", [addr, "latest"])
        return int(result.get("result", "0x0"), 16) / 1e18
    
    def token_balance(self, token_addr: str, holder_addr: str) -> float:
        """
        Get ERC-20 token balance with auto decimal detection.
        Returns human-readable balance.
        """
        # balanceOf selector
        sig = "0x70a08231" + holder_addr[2:].rjust(64, "0")
        result = self._eth_call(token_addr, sig)
        
        if result in ("0x", "0x0", None):
            return 0.0
        
        raw = int(result, 16)
        if raw == 0:
            return 0.0
        
        # Auto-detect decimals
        decimals = self.token_decimals(token_addr)
        return raw / (10 ** decimals)
    
    def token_decimals(self, token_addr: str) -> int:
        """Get ERC-20 decimals. Returns 18 if call fails."""
        try:
            result = self._eth_call(token_addr, "0x313ce567")
            if result and result not in ("0x", "0x0"):
                return int(result, 16)
        except Exception:
            pass
        return 18
    
    def token_symbol(self, token_addr: str) -> str:
        """Get ERC-20 symbol. Returns '?' if call fails."""
        try:
            result = self._eth_call(token_addr, "0x95d89b41")
            if result and len(result) >= 138:
                # Decode from ABI-encoded string
                offset = int(result[2:66], 16)
                length = int(result[66:130], 16)
                hex_str = result[130:130 + length * 2]
                return bytes.fromhex(hex_str).decode("utf-8", errors="replace").strip()
        except Exception:
            pass
        return "?"
    
    def erc20_meta(self, token_addr: str) -> dict:
        """Get ERC-20 metadata (name, symbol, decimals)."""
        return {
            "address": token_addr,
            "symbol": self.token_symbol(token_addr),
            "decimals": self.token_decimals(token_addr),
        }
    
    def token_price_usd(self, token_addr: str) -> float:
        """
        Get USD price for a token.
        Tries DexScreener first (best liquidity pair), falls back to GeckoTerminal.
        Returns 0.0 if both fail.
        """
        price = self._price_dexscreener(token_addr)
        if price > 0:
            return price
        return self._price_geckoterminal(token_addr)
    
    def _price_dexscreener(self, token_addr: str) -> float:
        """Get price from DexScreener (best liquidity Base pair)."""
        try:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{token_addr}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
            
            pairs = data.get("pairs", [])
            if not pairs:
                return 0.0
            
            # Filter to Base chain pairs with USDC or WETH
            best_pair = None
            best_liq = 0
            for p in pairs:
                if p.get("chainId") != "base":
                    continue
                liq = p.get("liquidity", {}).get("usd", 0)
                if liq > best_liq:
                    best_liq = liq
                    best_pair = p
            
            if best_pair:
                return float(best_pair.get("priceUsd", 0))
            
            # Fallback: any pair with a price
            for p in pairs:
                if p.get("chainId") == "base":
                    price = p.get("priceUsd")
                    if price:
                        return float(price)
            
            return 0.0
        except Exception:
            return 0.0
    
    def _price_geckoterminal(self, token_addr: str) -> float:
        """Get price from GeckoTerminal (WETH or USDC pair)."""
        try:
            # Try WETH pair first
            url = f"https://api.geckoterminal.com/api/v2/networks/base/tokens/{token_addr}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json;version=20230302"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
            
            for item in data.get("data", []):
                attrs = item.get("attributes", {})
                price = attrs.get("price_usd")
                if price:
                    return float(price)
            
            return 0.0
        except Exception:
            return 0.0
    
    def pair_swaps_v2(self, pair_addr: str, token0: str, token1: str,
                       from_block: Optional[int] = None, to_block: Optional[int] = None,
                       chunk_size: int = 5000) -> list:
        """
        Get Swap events for a V2 pair (Aerodrome/UniV2 style).
        Auto-chunks large block ranges to avoid RPC limits.
        Returns list of swap dicts.
        """
        # Swap event signature: Swap(address,address,int256,int256,uint160,uint128,int256)
        swap_sig = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
        
        if to_block is None:
            to_block = self.block_number()
        if from_block is None:
            from_block = to_block - 10000  # ~24h of blocks
        
        all_swaps = []
        
        # Chunk the range
        current = from_block
        while current <= to_block:
            end = min(current + chunk_size, to_block)
            
            logs = self._get_logs(
                pair_addr,
                [swap_sig],
                hex(current),
                hex(end),
            )
            
            for log in logs:
                try:
                    # Decode swap log
                    data = log.get("data", "0x")
                    topics = log.get("topics", [])
                    if len(data) < 194:
                        continue
                    
                    # Parse amounts (3 uint256 values after offset)
                    amount0_in = int(data[2:66], 16)
                    amount1_in = int(data[66:130], 16)
                    amount0_out = int(data[130:194], 16)
                    
                    # Determine swap direction
                    if amount0_in > 0:
                        # token0 -> token1 (buy token1)
                        direction = "buy"
                        amount_in = amount0_in
                        amount_out = amount0_out
                    else:
                        # token1 -> token0 (sell)
                        direction = "sell"
                        amount_in = amount1_in
                        amount_out = amount0_in  # this is 0, but we need the actual
                    
                    all_swaps.append({
                        "block": int(log.get("blockNumber", "0x0"), 16),
                        "tx_hash": log.get("transactionHash", ""),
                        "direction": direction,
                        "amount_in": amount_in,
                        "amount_out": amount_out,
                        "pair": pair_addr,
                    })
                except Exception:
                    continue
            
            current = end + 1
        
        return all_swaps
    
    def tx_history(self, wallet_addr: str, limit: int = 50) -> list:
        """
        Get transaction history for a wallet.
        Tries Routescan first (Etherscan-compat, no key), then BaseScan.
        """
        txs = self._tx_routescan(wallet_addr, limit)
        if txs:
            return txs
        return self._tx_basescan(wallet_addr, limit)
    
    def _tx_routescan(self, wallet_addr: str, limit: int = 50) -> list:
        """Routescan Etherscan-compatible API (no key needed for basic)."""
        try:
            url = f"https://api.routescan.io/v2/network/mainnet/evm/{CHAIN_ID}/etherscan/api?module=account&action=txlist&address={wallet_addr}&startblock=0&endblock=999999999&page=1&offset={limit}&sort=desc"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read())
            
            if data.get("status") != "1":
                return []
            
            result = []
            for tx in data.get("result", []):
                result.append({
                    "hash": tx.get("hash", ""),
                    "block": int(tx.get("blockNumber", 0)),
                    "from": tx.get("from", ""),
                    "to": tx.get("to", ""),
                    "value": int(tx.get("value", 0)) / 1e18,
                    "gas_price": int(tx.get("gasPrice", 0)),
                    "gas_used": int(tx.get("gasUsed", 0)),
                    "timestamp": datetime.fromtimestamp(int(tx.get("timeStamp", 0)), tz=timezone.utc).isoformat(),
                    "is_error": tx.get("isError", "0") == "1",
                    "input": tx.get("input", ""),
                })
            return result
        except Exception:
            return []
    
    def _tx_basescan(self, wallet_addr: str, limit: int = 50) -> list:
        """BaseScan API (no key needed for basic calls)."""
        try:
            url = f"https://api.basescan.org/v2/api?module=account&action=txlist&address={wallet_addr}&startblock=0&endblock=99999999&page=1&offset={limit}&sort=desc"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read())
            
            if data.get("status") != "1":
                return []
            
            result = []
            for tx in data.get("result", []):
                result.append({
                    "hash": tx.get("hash", ""),
                    "block": int(tx.get("blockNumber", 0)),
                    "from": tx.get("from", ""),
                    "to": tx.get("to", ""),
                    "value": int(tx.get("value", 0)) / 1e18,
                    "gas_price": int(tx.get("gasPrice", 0)),
                    "gas_used": int(tx.get("gasUsed", 0)),
                    "timestamp": datetime.fromtimestamp(int(tx.get("timeStamp", 0)), tz=timezone.utc).isoformat(),
                    "is_error": tx.get("isError", "0") == "1",
                    "input": tx.get("input", ""),
                })
            return result
        except Exception:
            return []
    
    def token_transfers(self, wallet_addr: str, limit: int = 50) -> list:
        """Get ERC-20 token transfers for a wallet."""
        try:
            url = f"https://api.basescan.org/v2/api?module=account&action=tokentx&address={wallet_addr}&page=1&offset={limit}&sort=desc"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read())
            
            if data.get("status") != "1":
                return []
            
            result = []
            for tx in data.get("result", []):
                result.append({
                    "hash": tx.get("hash", ""),
                    "block": int(tx.get("blockNumber", 0)),
                    "from": tx.get("from", ""),
                    "to": tx.get("to", ""),
                    "token": tx.get("tokenSymbol", "?"),
                    "token_address": tx.get("contractAddress", ""),
                    "value": int(tx.get("value", 0)) / (10 ** int(tx.get("tokenDecimal", 18))),
                    "timestamp": datetime.fromtimestamp(int(tx.get("timeStamp", 0)), tz=timezone.utc).isoformat(),
                })
            return result
        except Exception:
            return []
    
    def health(self) -> dict:
        """Check health of all RPC endpoints."""
        results = {}
        for rpc in self.rpc_pool:
            url = rpc['url']
            start = time.time()
            try:
                payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}
                req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=UA)
                with urllib.request.urlopen(req, timeout=10) as r:
                    data = json.loads(r.read())
                latency = time.time() - start
                block = int(data.get("result", "0x0"), 16)
                results[url] = {"status": "ok", "latency_ms": round(latency * 1000, 1), "block": block}
            except Exception as e:
                latency = time.time() - start
                results[url] = {"status": "error", "latency_ms": round(latency * 1000, 1), "error": str(e)[:80]}
        return results


# --- Convenience functions ---

def get_data() -> BaseData:
    """Get a BaseData instance."""
    return BaseData()


if __name__ == "__main__":
    import argparse
    
    ap = argparse.ArgumentParser(description="Base Data Layer")
    ap.add_argument("--health", action="store_true", help="Check RPC health")
    ap.add_argument("--smoke", action="store_true", help="Run smoke test")
    ap.add_argument("--block", action="store_true", help="Get block number")
    ap.add_argument("--fee", action="store_true", help="Get fee data")
    ap.add_argument("--balance", help="Get ETH balance for address")
    ap.add_argument("--token-balance", nargs=2, metavar=("TOKEN", "HOLDER"), help="Get token balance")
    ap.add_argument("--price", help="Get token price in USD")
    ap.add_argument("--txs", help="Get tx history for address")
    args = ap.parse_args()
    
    d = BaseData()
    
    if args.health:
        print("=== RPC Health Check ===")
        health = d.health()
        for url, info in health.items():
            status = info['status']
            latency = info.get('latency_ms', '?')
            block = info.get('block', '?')
            print(f"  {url:45} {status:6} {latency:>8}ms  block={block}")
    
    elif args.smoke:
        print("=== Smoke Test ===")
        print()
        
        # Block number
        try:
            block = d.block_number()
            print(f"Block: {block}")
        except Exception as e:
            print(f"Block: ERROR {e}")
        
        # Fee data
        try:
            fee = d.fee_data()
            print(f"Fee: base={fee['base_gwei']:.4f} gwei, priority={fee['priority_gwei']:.4f} gwei")
        except Exception as e:
            print(f"Fee: ERROR {e}")
        
        # USDC balance of a known holder
        try:
            bal = d.token_balance(USDC, "0xe7a16Be2aF1eCE8922d9d3aD55C03e29004e540F")
            print(f"USDC balance: {bal:.6f}")
        except Exception as e:
            print(f"USDC balance: ERROR {e}")
        
        # WETH price
        try:
            price = d.token_price_usd(WETH)
            print(f"WETH price: ${price:,.2f}")
        except Exception as e:
            print(f"WETH price: ERROR {e}")
        
        # Block number again (cache test)
        try:
            block2 = d.block_number()
            print(f"Block (cached): {block2}")
        except Exception as e:
            print(f"Block (cached): ERROR {e}")
    
    elif args.block:
        print(d.block_number())
    
    elif args.fee:
        fee = d.fee_data()
        print(json.dumps(fee, indent=2))
    
    elif args.balance:
        print(f"{d.balance_eth(args.balance):.6f} ETH")
    
    elif args.token_balance:
        token, holder = args.token_balance
        print(f"{d.token_balance(token, holder):.6f}")
    
    elif args.price:
        print(f"${d.token_price_usd(args.price):,.2f}")
    
    elif args.txs:
        txs = d.tx_history(args.txs)
        print(f"{len(txs)} transactions")
        for tx in txs[:5]:
            print(f"  {tx['hash'][:20]}... block={tx['block']} value={tx['value']:.6f}")
    
    else:
        # Default: health + block
        print("=== Base Data Layer ===")
        try:
            block = d.block_number()
            print(f"Block: {block}")
        except Exception as e:
            print(f"Block: ERROR {e}")
        
        try:
            fee = d.fee_data()
            print(f"Base fee: {fee['base_gwei']:.4f} gwei")
        except Exception as e:
            print(f"Fee: ERROR {e}")
