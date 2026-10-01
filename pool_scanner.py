#!/usr/bin/env python3
"""
pool_scanner.py — GeckoTerminal live pool activity scanner for Base.

Fetches real-time pool metrics (liquidity, volume, momentum) and validates
activity gates before EMA crossover signals are accepted.

Integrates with executor.py to filter manual tokens by live pool activity.

Usage:
    from pool_scanner import PoolScanner
    scanner = PoolScanner()
    metrics = scanner.fetch_pool_metrics(pool_address)
    is_active = scanner.validate_activity_gate(metrics)
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# GeckoTerminal v2 API
GECKO_BASE = "https://api.geckoterminal.com/api/v2"
GECKO_HEADERS = {
    "Accept": "application/json;version=20230302",
    "User-Agent": "Mozilla/5.0",
}

# Rate limiting - aggressive for cron scanning
MAX_CALLS_PER_MINUTE = 100
MIN_CALL_INTERVAL = 0.1  # 100ms between calls


class PoolScanner:
    """
    GeckoTerminal pool activity scanner for Base chain.
    
    Fetches real-time liquidity, volume, and momentum metrics.
    Validates activity gates before EMA crossover signals are accepted.
    """
    
    def __init__(self):
        self._call_times = []
    
    def _rate_limit(self):
        """Enforce rate limiting."""
        now = time.time()
        # Remove old calls
        self._call_times = [t for t in self._call_times if now - t < 60]
        
        if len(self._call_times) >= MAX_CALLS_PER_MINUTE:
            sleep_time = 60 - (now - self._call_times[0])
            if sleep_time > 0:
                time.sleep(sleep_time)
        
        # Minimum interval between calls
        if self._call_times:
            elapsed = now - self._call_times[-1]
            if elapsed < MIN_CALL_INTERVAL:
                time.sleep(MIN_CALL_INTERVAL - elapsed)
        
        self._call_times.append(time.time())
    
    def _get(self, url: str, timeout: int = 15) -> Optional[dict]:
        """Make a GET request with rate limiting."""
        self._rate_limit()
        try:
            req = urllib.request.Request(url, headers=GECKO_HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(60)
            return None
        except Exception:
            return None
    
    def fetch_pool_metrics(self, pool_address: str) -> Optional[dict]:
        """
        Fetch pool metrics from GeckoTerminal.
        
        Returns dict with:
            - liquidity_usd: float
            - v5m_usd: float (5-minute volume)
            - v15m_usd: float (15-minute volume)
            - v1h_usd: float (1-hour volume)
            - v24h_usd: float (24-hour volume)
            - token_price_usd: float
            - tx_buy_count: int (24h buys)
            - tx_sell_count: int (24h sells)
            - fdv_usd: float (fully diluted valuation)
        """
        url = f"{GECKO_BASE}/networks/base/pools/{pool_address}"
        data = self._get(url)
        
        if not data:
            return None
        
        pool_data = data.get("data", {})
        attrs = pool_data.get("attributes", {})
        
        volume_usd = attrs.get("volume_usd", {})
        transactions = attrs.get("transactions", {})
        h24_tx = transactions.get("h24", {})
        
        return {
            "liquidity_usd": float(attrs.get("reserve_in_usd", 0)),
            "v5m_usd": float(volume_usd.get("m5", 0)),
            "v15m_usd": float(volume_usd.get("m15", 0)),
            "v1h_usd": float(volume_usd.get("h1", 0)),
            "v24h_usd": float(volume_usd.get("h24", 0)),
            "token_price_usd": float(attrs.get("base_token_price_usd", 0)),
            "fdv_usd": float(attrs.get("fdv_usd", 0)),
            "tx_buy_count": int(h24_tx.get("buys", 0)),
            "tx_sell_count": int(h24_tx.get("sells", 0)),
            "tx_total": int(h24_tx.get("buys", 0)) + int(h24_tx.get("sells", 0)),
            "pool_address": pool_address,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
    
    def fetch_multi_pool_metrics(self, pool_addresses: list) -> dict:
        """
        Fetch metrics for multiple pools in a single call (up to 30).
        
        Returns dict: {pool_address: metrics_dict}
        """
        if not pool_addresses:
            return {}
        
        # GeckoTerminal allows up to 30 addresses per call
        chunk_size = 30
        results = {}
        
        for i in range(0, len(pool_addresses), chunk_size):
            chunk = pool_addresses[i:i + chunk_size]
            addresses_str = ",".join(chunk)
            
            url = f"{GECKO_BASE}/networks/base/pools/multi/{addresses_str}"
            data = self._get(url)
            
            if data:
                for pool_data in data.get("data", []):
                    attrs = pool_data.get("attributes", {})
                    pool_addr = pool_data.get("id", "").split("_")[-1]
                    
                    volume_usd = attrs.get("volume_usd", {})
                    transactions = attrs.get("transactions", {})
                    h24_tx = transactions.get("h24", {})
                    
                    results[pool_addr] = {
                        "liquidity_usd": float(attrs.get("reserve_in_usd", 0)),
                        "v5m_usd": float(volume_usd.get("m5", 0)),
                        "v15m_usd": float(volume_usd.get("m15", 0)),
                        "v1h_usd": float(volume_usd.get("h1", 0)),
                        "v24h_usd": float(volume_usd.get("h24", 0)),
                        "token_price_usd": float(attrs.get("base_token_price_usd", 0)),
                        "fdv_usd": float(attrs.get("fdv_usd", 0)),
                        "tx_buy_count": int(h24_tx.get("buys", 0)),
                        "tx_sell_count": int(h24_tx.get("sells", 0)),
                        "tx_total": int(h24_tx.get("buys", 0)) + int(h24_tx.get("sells", 0)),
                        "pool_address": pool_addr,
                    }
        
        return results
    
    def fetch_trending_pools(self, page: int = 1) -> list:
        """
        Fetch trending pools on Base.
        
        Returns list of pool dicts with metrics.
        """
        url = f"{GECKO_BASE}/networks/base/trending_pools?page={page}"
        data = self._get(url)
        
        if not data:
            return []
        
        pools = []
        for pool_data in data.get("data", []):
            attrs = pool_data.get("attributes", {})
            volume_usd = attrs.get("volume_usd", {})
            
            pools.append({
                "address": attrs.get("address", ""),
                "name": attrs.get("name", "?"),
                "liquidity_usd": float(attrs.get("reserve_in_usd", 0)),
                "v5m_usd": float(volume_usd.get("m5", 0)),
                "v1h_usd": float(volume_usd.get("h1", 0)),
                "v24h_usd": float(volume_usd.get("h24", 0)),
                "token_price_usd": float(attrs.get("base_token_price_usd", 0)),
            })
        
        return pools
    
    def validate_activity_gate(
        self,
        metrics: Optional[dict],
        min_liq_usd: float = 10000,
        min_5m_vol_usd: float = 5,
        min_1h_vol_usd: float = 500,
    ) -> bool:
        """
        Validate pool has enough baseline momentum and depth for EMA entry.
        
        Args:
            metrics: Pool metrics dict from fetch_pool_metrics()
            min_liq_usd: Minimum liquidity in USD
            min_5m_vol_usd: Minimum 5-minute volume in USD
            min_1h_vol_usd: Minimum 1-hour volume in USD
        
        Returns:
            True if pool passes activity gate
        """
        if not metrics:
            return False
        
        has_liquidity = metrics.get("liquidity_usd", 0) >= min_liq_usd
        has_5m_momentum = metrics.get("v5m_usd", 0) >= min_5m_vol_usd
        has_1h_volume = metrics.get("v1h_usd", 0) >= min_1h_vol_usd
        
        return has_liquidity and has_5m_momentum and has_1h_volume
    
    def calculate_pace_ratio(self, metrics: dict) -> Optional[float]:
        """
        Calculate the pace ratio: current 5m volume vs trailing hourly pace.
        
        Pace = vol_5m / (vol_1h / 12)
        
        Returns:
            Pace ratio (float) or None if baseline is unusable.
            >= 1.0 means current flow is at or above hourly average.
        """
        v5m = metrics.get("v5m_usd", 0)
        v1h = metrics.get("v1h_usd", 0)
        
        if v5m < 250:  # Noise floor
            return 0.0
        
        if v1h <= 0:
            return None
        
        # Check if baseline is usable (vol_1h must be > vol_5m * 2)
        if v1h < v5m * 2:
            return None
        
        expected_5m = v1h / 12.0
        if expected_5m <= 0:
            return None
        
        pace = v5m / expected_5m
        
        # Cap implausible spikes
        if pace > 4.0:
            return None
        
        return pace


# --- Integration helpers ---

def get_scanner() -> PoolScanner:
    """Get a PoolScanner instance."""
    return PoolScanner()


def fetch_and_validate(pool_address: str, min_liq: float = 5000) -> Optional[dict]:
    """
    Fetch metrics and validate activity gate in one call.
    
    Returns metrics dict if pool passes gate, None otherwise.
    """
    scanner = PoolScanner()
    metrics = scanner.fetch_pool_metrics(pool_address)
    
    if not scanner.validate_activity_gate(metrics, min_liq_usd=min_liq):
        return None
    
    return metrics


if __name__ == "__main__":
    import argparse
    
    ap = argparse.ArgumentParser(description="GeckoTerminal Pool Scanner")
    ap.add_argument("--pool", help="Pool address to scan")
    ap.add_argument("--trending", action="store_true", help="Show trending pools")
    ap.add_argument("--validate", action="store_true", help="Validate activity gate")
    ap.add_argument("--watchlist", nargs="+", help="Scan multiple pools")
    args = ap.parse_args()
    
    scanner = PoolScanner()
    
    if args.trending:
        print("=== Trending Pools (Base) ===")
        pools = scanner.fetch_trending_pools()
        for p in pools[:10]:
            print(f"  {p['name'][:30]:30} liq=${p['liquidity_usd']:>10,.0f} v5m=${p['v5m_usd']:>8,.0f}")
    
    elif args.pool:
        print(f"=== Pool Metrics: {args.pool} ===")
        metrics = scanner.fetch_pool_metrics(args.pool)
        if metrics:
            print(f"  Liquidity: ${metrics['liquidity_usd']:,.0f}")
            print(f"  Vol 5m:  ${metrics['v5m_usd']:,.0f}")
            print(f"  Vol 1h:  ${metrics['v1h_usd']:,.0f}")
            print(f"  Vol 24h: ${metrics['v24h_usd']:,.0f}")
            print(f"  Price:   ${metrics['token_price_usd']:,.6f}")
            print(f"  TXs 24h: {metrics['tx_buy_count']} buys / {metrics['tx_sell_count']} sells")
            
            if args.validate:
                passes = scanner.validate_activity_gate(metrics)
                print(f"  Gate: {'PASS' if passes else 'FAIL'}")
        else:
            print("  Failed to fetch metrics")
    
    elif args.watchlist:
        print(f"=== Multi-Pool Scan ({len(args.watchlist)} pools) ===")
        results = scanner.fetch_multi_pool_metrics(args.watchlist)
        for addr, m in results.items():
            print(f"  {addr[:20]}... liq=${m['liquidity_usd']:>10,.0f} v5m=${m['v5m_usd']:>8,.0f}")
    
    else:
        print("Use --pool, --trending, or --watchlist")
