#!/usr/bin/env python3
"""
ema_engine.py — local 9/21 EMA crossover detection using GeckoTerminal OHLCV.

Fetches hourly candles from GeckoTerminal, computes EMA(9) and EMA(21),
and detects crossovers. Integrates with pool_scanner.py for activity gating.

Usage:
    from ema_engine import EmaEngine
    engine = EmaEngine()
    signal = engine.check_crossover(pool_address)
    signals = engine.batch_check([pool1, pool2, ...])
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

from pool_scanner import PoolScanner

# GeckoTerminal v2 API
GECKO_BASE = "https://api.geckoterminal.com/api/v2"
GECKO_HEADERS = {
    "Accept": "application/json;version=20230302",
    "User-Agent": "Mozilla/5.0",
}

# Rate limiting - aggressive for cron scanning
MAX_CALLS_PER_MINUTE = 100
MIN_CALL_INTERVAL = 0.1


class EmaEngine:
    """
    Compute 9/21 EMA crossovers from GeckoTerminal OHLCV data.
    """
    
    def __init__(self, fast_period: int = 9, slow_period: int = 21):
        self.fast_period = fast_period
        self.slow_period = slow_period
        self._call_times = []
    
    def _rate_limit(self):
        """Enforce rate limiting."""
        now = time.time()
        self._call_times = [t for t in self._call_times if now - t < 60]
        
        if len(self._call_times) >= MAX_CALLS_PER_MINUTE:
            sleep_time = 60 - (now - self._call_times[0])
            if sleep_time > 0:
                time.sleep(sleep_time)
        
        if self._call_times:
            elapsed = now - self._call_times[-1]
            if elapsed < MIN_CALL_INTERVAL:
                time.sleep(MIN_CALL_INTERVAL - elapsed)
        
        self._call_times.append(time.time())
    
    def _get(self, url: str, timeout: int = 15) -> Optional[dict]:
        """Rate-limited GET request."""
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
    
    def fetch_ohlcv(self, pool_address: str, limit: int = 168) -> list:
        """
        Fetch hourly OHLCV candles from GeckoTerminal.
        
        API: /networks/base/pools/{address}/ohlcv/hour?aggregate=1&limit=N
        
        Returns list of candle dicts: {ts, o, h, l, c, v}
        """
        pool_address = pool_address.lower()
        
        url = (
            f"{GECKO_BASE}/networks/base/pools/{pool_address}"
            f"/ohlcv/hour?aggregate=1&limit={limit}"
        )
        
        data = self._get(url)
        if not data:
            return []
        
        ohlcv_list = data.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
        
        candles = []
        for row in ohlcv_list:
            if len(row) >= 6:
                candles.append({
                    "ts": row[0],
                    "o": float(row[1]),
                    "h": float(row[2]),
                    "l": float(row[3]),
                    "c": float(row[4]),
                    "v": float(row[5]),
                })
        
        return sorted(candles, key=lambda x: x["ts"])
    
    @staticmethod
    def compute_ema(values: list, period: int) -> list:
        """
        Compute EMA series from a list of values.
        Returns list aligned with input (None for first period-1 entries).
        """
        if len(values) < period:
            return [None] * len(values)
        
        ema = [None] * len(values)
        multiplier = 2.0 / (period + 1)
        
        # SMA for initial value
        sma = sum(values[:period]) / period
        ema[period - 1] = sma
        
        for i in range(period, len(values)):
            ema[i] = (values[i] - ema[i - 1]) * multiplier + ema[i - 1]
        
        return ema
    
    def check_crossover(self, pool_address: str, require_activity: bool = True) -> dict:
        """
        Check for EMA crossover on a pool.
        
        Returns dict with:
            - crossover: bool
            - direction: "bullish", "bearish", or None
            - fast_ema, slow_ema, prev_fast, prev_slow, price
            - activity_passed: bool (if require_activity)
        """
        result = {
            "pool": pool_address,
            "crossover": False,
            "direction": None,
            "fast_ema": None,
            "slow_ema": None,
            "prev_fast": None,
            "prev_slow": None,
            "price": None,
            "activity_passed": None,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        
        # Activity gate
        if require_activity:
            scanner = PoolScanner()
            metrics = scanner.fetch_pool_metrics(pool_address)
            if metrics:
                result["activity_passed"] = scanner.validate_activity_gate(metrics)
                if not result["activity_passed"]:
                    return result
            else:
                result["activity_passed"] = False
                return result
        
        # Fetch candles
        candles = self.fetch_ohlcv(pool_address)
        if len(candles) < self.slow_period + 1:
            return result
        
        # Compute EMAs
        closes = [c["c"] for c in candles]
        fast_ema = self.compute_ema(closes, self.fast_period)
        slow_ema = self.compute_ema(closes, self.slow_period)
        
        # Find last two valid EMA pairs
        valid_idx = [
            i for i in range(len(candles))
            if fast_ema[i] is not None and slow_ema[i] is not None
        ]
        
        if len(valid_idx) < 2:
            return result
        
        curr = valid_idx[-1]
        prev = valid_idx[-2]
        
        result["fast_ema"] = round(fast_ema[curr], 8)
        result["slow_ema"] = round(slow_ema[curr], 8)
        result["prev_fast"] = round(fast_ema[prev], 8)
        result["prev_slow"] = round(slow_ema[prev], 8)
        result["price"] = round(closes[curr], 8)
        
        # Detect crossover
        prev_diff = fast_ema[prev] - slow_ema[prev]
        curr_diff = fast_ema[curr] - slow_ema[curr]
        
        if prev_diff <= 0 and curr_diff > 0:
            result["crossover"] = True
            result["direction"] = "bullish"
        elif prev_diff >= 0 and curr_diff < 0:
            result["crossover"] = True
            result["direction"] = "bearish"
        
        return result
    
    def batch_check(self, pool_addresses: list, require_activity: bool = True) -> dict:
        """Check multiple pools for crossovers."""
        results = {}
        for addr in pool_addresses:
            results[addr] = self.check_crossover(addr, require_activity=require_activity)
        return results


# --- Bluechip Watchlist ---

BLUECHIP_WATCHLIST = {
    "0x9b5e262cf9bb04869ab40b19af91d2dc85761722": {"name": "NOCK"},
    "0xb095274743941e953c746f9c228da9c18bb6ec29": {"name": "LAPTOP"},
    "0xb2000000000000000000004884b426556b92883d": {"name": "MSTRc"},
    "0x8b67f2e56139ca052a7ec49cbcd1aa9c83f2752a": {"name": "MUTATIO"},
    "0xb20000000000000000000074deAC544E1E9a1f33": {"name": "STONKFLY"},
    "0xb2000000000000000000004c27f6523082f41d01": {"name": "Basecat"},
    "0x46fee5e7a02b374700fde31d82a9a157d8915f46": {"name": "kenzodadon84"},
    "0x532f27101965dd16442e59d40670faf5ebb142e4": {"name": "BRETT"},
    "0xbaa5cc21fd487b8fcc2f632f3f4e8d37262a0842": {"name": "MORPHO"},
    "0xb1a03eda10342529bbf8eb700a06c60441fef25d": {"name": "MIGGLES"},
    "0x9a26f5433671751c3276a065f57e5a02d2817973": {"name": "KEYCAT"},
}


def discover_pools(token_addresses: list) -> dict:
    """
    Discover best WETH/USDC pool for each token via DexScreener.
    Returns {token_addr: pool_addr}
    """
    results = {}
    
    for addr in token_addresses:
        try:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{addr}"
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json"
            })
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
            
            pairs = data.get("pairs", [])
            best = None
            best_liq = 0
            
            for p in pairs:
                if p.get("chainId") != "base":
                    continue
                liq = p.get("liquidity", {}).get("usd", 0)
                quote = p.get("quoteToken", {}).get("symbol", "")
                if quote in ("WETH", "USDC"):
                    if liq > best_liq:
                        best_liq = liq
                        best = p.get("pairAddress")
            
            if best:
                results[addr] = best
        except Exception:
            continue
    
    return results


def check_watchlist() -> dict:
    """Check all bluechip tokens for EMA crossovers."""
    engine = EmaEngine()
    
    # Discover pools
    tokens = list(BLUECHIP_WATCHLIST.keys())
    pools = discover_pools(tokens)
    
    results = {}
    for token_addr, pool_addr in pools.items():
        name = BLUECHIP_WATCHLIST.get(token_addr, {}).get("name", token_addr[:12])
        result = engine.check_crossover(pool_addr)
        result["name"] = name
        result["token"] = token_addr
        results[token_addr] = result
        
        # Print summary
        cross = "🟢" if result["crossover"] and result["direction"] == "bullish" else \
                "🔴" if result["crossover"] else "⚪"
        activity = "✅" if result.get("activity_passed") else "❌"
        print(f"  {cross} {activity} {name:15} cross={result['crossover']} dir={result['direction']}")
    
    return results


if __name__ == "__main__":
    import argparse
    
    ap = argparse.ArgumentParser(description="EMA Crossover Engine")
    ap.add_argument("--pool", help="Check single pool for crossover")
    ap.add_argument("--watchlist", action="store_true", help="Check all bluechip tokens")
    ap.add_argument("--discover", action="store_true", help="Discover pools for watchlist")
    ap.add_argument("--candles", type=int, default=168, help="Number of candles")
    args = ap.parse_args()
    
    engine = EmaEngine()
    
    if args.discover:
        print("=== Discovering Pools ===")
        pools = discover_pools(list(BLUECHIP_WATCHLIST.keys()))
        for token, pool in pools.items():
            name = BLUECHIP_WATCHLIST.get(token, {}).get("name", "?")
            print(f"  {name:15} → {pool}")
    
    elif args.watchlist:
        print("=== Bluechip Watchlist Scan ===")
        check_watchlist()
    
    elif args.pool:
        print(f"=== EMA Crossover: {args.pool} ===")
        result = engine.check_crossover(args.pool)
        
        print(f"  Price: ${result.get('price', 'N/A')}")
        print(f"  EMA9:  {result.get('fast_ema', 'N/A')}")
        print(f"  EMA21: {result.get('slow_ema', 'N/A')}")
        print(f"  Crossover: {result['crossover']}")
        print(f"  Direction: {result['direction']}")
        print(f"  Activity: {result.get('activity_passed')}")
    else:
        print("Use --pool, --watchlist, or --discover")
