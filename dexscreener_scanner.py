#!/usr/bin/env python3
"""
dexscreener_scanner.py — DexScreener pool metrics for Base chain.

Free API: 300 requests/minute
Chain ID for Base: 8453

Provides pool metrics (liquidity, volume, price) without rate limiting issues.
Does NOT provide OHLCV candles — those still come from GeckoTerminal.

Usage:
    from dexscreener_scanner import DexScreenerScanner
    ds = DexScreenerScanner()
    pools = ds.get pools_by_addresses([addr1, addr2, ...])
"""
import json
import time
import urllib.request
import urllib.error

BASE_URL = "https://api.dexscreener.com"
CHAIN_ID = "base"  # Base chain

HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0",
}


class DexScreenerScanner:
    """Fetch pool metrics from DexScreener."""

    def __init__(self):
        self._cache = {}  # {pair_address: (timestamp, data)}
        self._cache_ttl = 300  # 5 min cache

    def _clean_cache(self):
        now = time.time()
        expired = [k for k, (ts, _) in self._cache.items() if now - ts >= self._cache_ttl]
        for k in expired:
            del self._cache[k]

    def _fetch(self, url):
        """Fetch with basic retry."""
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers=HEADERS)
                with urllib.request.urlopen(req, timeout=10) as r:
                    return json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(2 ** attempt)
                    continue
                raise
            except Exception:
                if attempt < 2:
                    time.sleep(1)
                    continue
                raise
        return None

    def get_pool_by_address(self, pair_address):
        """Get a single pool by pair address.
        
        Returns dict with pool data or None.
        """
        # Check cache
        if pair_address in self._cache:
            ts, data = self._cache[pair_address]
            if time.time() - ts < self._cache_ttl:
                return data
            del self._cache[pair_address]

        url = f"{BASE_URL}/latest/dex/pairs/{CHAIN_ID}/{pair_address}"
        data = self._fetch(url)

        if data and data.get("pairs"):
            result = data["pairs"][0]
            self._cache[pair_address] = (time.time(), result)
            self._clean_cache()
            return result

        return None

    def get_pools_by_addresses(self, pair_addresses):
        """Get multiple pools. More efficient than individual calls.
        
        Note: DexScreener doesn't have a bulk endpoint, so we call individually
        but with cache to avoid repeat requests.
        """
        results = {}
        for addr in pair_addresses:
            pool = self.get_pool_by_address(addr)
            if pool:
                results[addr] = pool
        return results

    def get_token_pairs(self, token_address):
        """Get all pairs for a token address.
        
        Returns list of pool dicts.
        """
        url = f"{BASE_URL}/token-pairs/v1/{CHAIN_ID}/{token_address}"
        data = self._fetch(url)
        if data:
            self._cache[token_address] = (time.time(), data)
            return data
        return []

    def extract_metrics(self, pool_data):
        """Extract standardized metrics from DexScreener pool data.
        
        Returns dict with:
            - liquidity_usd: float
            - vol_1h_usd: float
            - vol_5m_usd: float  
            - price_usd: float
            - pair_address: str
            - base_token: dict (name, symbol, address)
            - quote_token: dict
        """
        if not pool_data:
            return None

        try:
            base = pool_data.get("baseToken", {})
            quote = pool_data.get("quoteToken", {})
            volume = pool_data.get("volume", {})
            liquidity = pool_data.get("liquidity", {})
            txns = pool_data.get("txns", {})

            return {
                "pair_address": pool_data.get("pairAddress", ""),
                "base_token": {
                    "address": base.get("address", ""),
                    "name": base.get("name", ""),
                    "symbol": base.get("symbol", ""),
                },
                "quote_token": {
                    "address": quote.get("address", ""),
                    "name": quote.get("name", ""),
                    "symbol": quote.get("symbol", ""),
                },
                "price_usd": float(pool_data.get("priceUsd", 0) or 0),
                "liquidity_usd": float(liquidity.get("usd", 0) or 0),
                "vol_1h_usd": float(volume.get("h1", 0) or 0),
                "vol_5m_usd": float(volume.get("m5", 0) or 0),
                "vol_24h_usd": float(volume.get("h24", 0) or 0),
                "txns_1h": {
                    "buys": int(txns.get("h1", {}).get("buys", 0) or 0),
                    "sells": int(txns.get("h1", {}).get("sells", 0) or 0),
                },
                "pair_created_at": pool_data.get("pairCreatedAt", 0),
                "dex_id": pool_data.get("dexId", ""),
            }
        except Exception as e:
            return None


# Convenience function
def get_pool_metrics(pair_address):
    """Get pool metrics from DexScreener. Returns standardized dict or None."""
    scanner = DexScreenerScanner()
    pool_data = scanner.get_pool_by_address(pair_address)
    if pool_data:
        return scanner.extract_metrics(pool_data)
    return None


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        addr = sys.argv[1]
        metrics = get_pool_metrics(addr)
        if metrics:
            print(f"Pool: {metrics['base_token']['symbol']}/{metrics['quote_token']['symbol']}")
            print(f"Price: ${metrics['price_usd']:.6f}")
            print(f"Liquidity: ${metrics['liquidity_usd']:,.0f}")
            print(f"Vol 1h: ${metrics['vol_1h_usd']:,.0f}")
            print(f"Vol 5m: ${metrics['vol_5m_usd']:,.0f}")
            print(f"Vol 24h: ${metrics['vol_24h_usd']:,.0f}")
            print(f"TXNs 1h: {metrics['txns_1h']['buys']} buys / {metrics['txns_1h']['sells']} sells")
            print(f" DEX: {metrics['dex_id']}")
        else:
            print(f"No pool found for {addr}")
    else:
        print("Usage: python3 dexscreener_scanner.py <pair_address>")
