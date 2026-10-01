#!/usr/bin/env python3
"""
Coinbase Price Feed — real-time market data from Coinbase Advanced Trade.

Wraps the `coinbase` CLI to fetch:
- Product prices (spot, 24h change, volume)
- OHLCV candles (hourly, daily)
- Order book (bid/ask)
- Ticker (recent trades)

Designed as a drop-in price oracle for the executor pipeline.
Falls back to GeckoTerminal if Coinbase doesn't list the asset.

Usage:
    from coinbase_feed import CoinbaseFeed
    feed = CoinbaseFeed()
    price = feed.get_price("BTC-USD")
    candles = feed.get_candles("ETH-USD", granularity="1h", limit=120)
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))


class CoinbaseError(Exception):
    pass


class CoinbaseFeed:
    """Thin wrapper around the `coinbase` CLI for market data."""

    def __init__(self, env="live", timeout=30):
        self.env = env
        self.timeout = timeout
        self._available = None
        self._product_cache = {}
        self._cache_ts = 0
        self._cache_ttl = 300  # 5 minutes

    def _run(self, *args, check=True):
        """Run a coinbase CLI command and return parsed JSON."""
        cmd = ["coinbase"] + list(args)
        if self.env:
            cmd.extend(["-e", self.env])
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            if result.returncode != 0:
                stderr = result.stderr.strip()
                if "no key" in stderr.lower() or "unauthorized" in stderr.lower():
                    self._available = False
                    raise CoinbaseError(f"Coinbase CLI not authenticated: {stderr}")
                if check:
                    raise CoinbaseError(f"coinbase CLI error: {stderr}")
                return None
            stdout = result.stdout.strip()
            if not stdout:
                return None
            # Some commands return plain text (e.g., "Switched to live.")
            # Try JSON first, fall through to raw string
            try:
                return json.loads(stdout)
            except json.JSONDecodeError:
                return stdout
        except subprocess.TimeoutExpired:
            raise CoinbaseError(f"coinbase CLI timeout after {self.timeout}s")
        except json.JSONDecodeError:
            raise CoinbaseError(f"coinbase CLI returned invalid JSON")
        except FileNotFoundError:
            self._available = False
            raise CoinbaseError("coinbase CLI not installed (npm i -g @coinbase/coinbase-cli)")

    @property
    def available(self):
        """Check if Coinbase CLI is installed and authenticated."""
        if self._available is not None:
            return self._available
        try:
            self._run("env", check=False)
            self._available = True
        except CoinbaseError:
            self._available = False
        return self._available

    def balance(self):
        """Get account balances."""
        return self._run("balance")

    def get_price(self, product_id):
        """
        Get current price for a product.
        Returns dict with price, volume, change, etc.
        """
        data = self._run("products", "get", product_id, "--jq", ".")
        if not data:
            return None
        return {
            "product_id": product_id,
            "price": float(data.get("price", 0)),
            "volume_24h": float(data.get("volume_24h", 0)),
            "change_24h": float(data.get("volume_change_24h", 0)),
            "low_24h": float(data.get("low_24h", 0)),
            "high_24h": float(data.get("high_24h", 0)),
            "best_bid": float(data.get("best_bid", 0)),
            "best_ask": float(data.get("best_ask", 0)),
            "source": "coinbase",
        }

    def get_ticker(self, product_id):
        """Get recent trades and best bid/ask."""
        data = self._run("products", "ticker", product_id, "--jq", ".")
        if not data:
            return None
        return {
            "product_id": product_id,
            "price": float(data.get("price", 0)),
            "bid": float(data.get("bid", 0)),
            "ask": float(data.get("ask", 0)),
            "volume": float(data.get("volume", 0)),
            "trades": data.get("trades", []),
            "source": "coinbase",
        }

    def get_book(self, product_id, level=2):
        """Get order book (bids + asks)."""
        data = self._run("products", "book", product_id, f"--level={level}", "--jq", ".")
        if not data:
            return None
        return {
            "product_id": product_id,
            "bids": [[float(p), float(s)] for p, s in data.get("bids", [])[:20]],
            "asks": [[float(p), float(s)] for p, s in data.get("asks", [])[:20]],
            "sequence": data.get("sequence"),
            "source": "coinbase",
        }

    def get_candles(self, product_id, granularity="1h", limit=120):
        """
        Get OHLCV candles.
        granularity: 1m, 5m, 15m, 1h, 6h, 1d
        Returns list of dicts: {ts, o, h, l, c, v}
        """
        # Map granularity to CLI format
        gran_map = {
            "1m": "1m",
            "5m": "5m",
            "15m": "15m",
            "30m": "30m",
            "1h": "1h",
            "2h": "2h",
            "4h": "4h",
            "6h": "6h",
            "1d": "1d",
        }
        gran = gran_map.get(granularity, "ONE_HOUR")
        data = self._run(
            "products", "candles", product_id,
            f"granularity=={gran}",
            f"--limit={limit}",
            "--jq", ".",
        )
        if not data:
            return []
        # CLI returns {"candles": [...]} — extract the array
        if isinstance(data, dict):
            data = data.get("candles", [])
        candles = []
        for c in data:
            candles.append({
                "ts": c.get("start") or c.get("timestamp") or 0,
                "o": float(c.get("open", 0)),
                "h": float(c.get("high", 0)),
                "l": float(c.get("low", 0)),
                "c": float(c.get("close", 0)),
                "v": float(c.get("volume", 0)),
            })
        return sorted(candles, key=lambda x: x["ts"])

    def list_products(self, symbol=None, product_type="SPOT"):
        """List tradeable products. Filter by symbol (e.g., 'BTC') or type (SPOT, FUTURE, EQUITY)."""
        args = ["products", "list"]
        if symbol:
            args.append(f"symbol=={symbol}")
        if product_type:
            args.append(f"product_type={product_type}")
        args.extend(["--jq", "."])
        data = self._run(*args)
        if not data:
            return []
        return data if isinstance(data, list) else [data]

    def get_products_by_quote(self, quote="USD"):
        """Get all products with a given quote currency."""
        data = self._run("products", "list", f"symbol=={quote}", "--jq", ".")
        if not data:
            return []
        return data if isinstance(data, list) else [data]

    def get_best_bid_ask(self, product_ids):
        """Get best bid/ask for multiple products at once."""
        if isinstance(product_ids, str):
            product_ids = [product_ids]
        ids_str = ",".join(product_ids)
        data = self._run("products", "best-bid-ask", f"product_ids={ids_str}", "--jq", ".")
        if not data:
            return {}
        if isinstance(data, list):
            return {item.get("product_id"): item for item in data}
        return {data.get("product_id"): data}

    def get_fees(self):
        """Get current fee tier."""
        return self._run("fees")

    def preview_order(self, product_id, side, order_type, **kwargs):
        """
        Preview an order (dry-run).
        kwargs: quote_size, base_size, limit_price, etc.
        """
        args = ["orders", "preview", f"product_id={product_id}", f"side={side}", f"type={order_type}"]
        for k, v in kwargs.items():
            if isinstance(v, list):
                args.append(f"{k}:='{json.dumps(v)}'")
            else:
                args.append(f"{k}={v}")
        args.extend(["--jq", "."])
        return self._run(*args)

    def place_order(self, product_id, side, order_type, **kwargs):
        """
        Place an order.
        kwargs: quote_size, base_size, limit_price, client_order_id, etc.
        """
        args = ["orders", "create", f"product_id={product_id}", f"side={side}", f"type={order_type}"]
        for k, v in kwargs.items():
            if isinstance(v, list):
                args.append(f"{k}:='{json.dumps(v)}'")
            else:
                args.append(f"{k}={v}")
        args.extend(["--jq", "."])
        return self._run(*args)

    def list_orders(self, status=None):
        """List orders. Filter by status (open, filled, cancelled)."""
        args = ["orders", "list"]
        if status:
            args.append(f"status={status}")
        args.extend(["--jq", "."])
        return self._run(*args)

    def list_fills(self):
        """List all trade fills."""
        return self._run("orders", "fills", "--jq", ".")

    def cancel_order(self, order_ids):
        """Cancel one or more orders."""
        if isinstance(order_ids, str):
            order_ids = [order_ids]
        return self._run("orders", "cancel", f"order_ids:='{json.dumps(order_ids)}'", "--jq", ".")

    def get_portfolio(self, portfolio_id):
        """Get portfolio breakdown."""
        return self._run("portfolios", "get", portfolio_id, "--jq", ".")

    def list_portfolios(self):
        """List all portfolios."""
        return self._run("portfolios", "list", "--jq", ".")

    def transfer(self, amount, currency, from_uuid, to_uuid):
        """Transfer funds between portfolios."""
        return self._run(
            "transfer",
            f"amount={amount}",
            f"currency={currency}",
            f"from={from_uuid}",
            f"to={to_uuid}",
            "--jq", ".",
        )

    def convert_quote(self, from_cur, to_cur, amount):
        """Get a conversion quote."""
        return self._run(
            "convert", "quote",
            f"from={from_cur}",
            f"to={to_cur}",
            f"amount={amount}",
            "--jq", ".",
        )

    def convert_execute(self, quote_id, from_cur, to_cur):
        """Execute a conversion."""
        return self._run(
            "convert", "execute",
            quote_id,
            f"from={from_cur}",
            f"to={to_cur}",
            "--jq", ".",
        )


# --- Convenience functions for the executor pipeline ---

def get_coinbase_price(product_id):
    """Quick price lookup. Returns float or None."""
    try:
        feed = CoinbaseFeed()
        data = feed.get_price(product_id)
        return data["price"] if data else None
    except CoinbaseError:
        return None


def get_coinbase_candles(product_id, granularity="1h", limit=120):
    """Quick candle lookup. Returns list of OHLCV dicts."""
    try:
        feed = CoinbaseFeed()
        return feed.get_candles(product_id, granularity, limit)
    except CoinbaseError:
        return []


def coinbase_to_gecko_candles(candles):
    """
    Convert Coinbase candles to GeckoTerminal format for compatibility
    with the existing executor pipeline (ATR, EMA, etc.).
    """
    if not candles:
        return []
    return sorted([{
        "ts": c["ts"],
        "o": c["o"],
        "h": c["h"],
        "l": c["l"],
        "c": c["c"],
        "v": c["v"],
    } for c in candles], key=lambda x: x["ts"])


def get_price_with_fallback(product_id, gecko_fallback_fn=None):
    """
    Get price from Coinbase first, fall back to GeckoTerminal if unavailable.
    gecko_fallback_fn: callable that returns price from GeckoTerminal
    """
    try:
        feed = CoinbaseFeed()
        if feed.available:
            data = feed.get_price(product_id)
            if data and data.get("price", 0) > 0:
                return data["price"], "coinbase"
    except CoinbaseError:
        pass

    if gecko_fallback_fn:
        price = gecko_fallback_fn()
        if price and price > 0:
            return price, "geckoterminal"

    return None, None


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Coinbase Price Feed CLI")
    ap.add_argument("command", choices=["price", "ticker", "book", "candles", "products", "balance", "test"])
    ap.add_argument("--product", "-p", help="Product ID (e.g., BTC-USD)")
    ap.add_argument("--granularity", "-g", default="1h", help="Candle granularity")
    ap.add_argument("--limit", "-l", type=int, default=120, help="Number of candles")
    ap.add_argument("--symbol", "-s", help="Symbol filter (e.g., BTC)")
    ap.add_argument("--env", "-e", default="live", help="Environment (live/development)")
    args = ap.parse_args()

    feed = CoinbaseFeed(env=args.env)

    if args.command == "test":
        print("Testing Coinbase feed...")
        try:
            bal = feed.balance()
            print(f"Balance: {len(bal.get('accounts', []))} accounts")
        except CoinbaseError as e:
            print(f"Auth error: {e}")
        price = feed.get_price("BTC-USD")
        print(f"BTC-USD: ${price['price']:,.2f}" if price else "BTC-USD: unavailable")
        candles = feed.get_candles("ETH-USD", limit=5)
        print(f"ETH-USD candles: {len(candles)}")

    elif args.command == "price":
        if not args.product:
            print("--product required")
            sys.exit(1)
        data = feed.get_price(args.product)
        print(json.dumps(data, indent=2) if data else "No data")

    elif args.command == "ticker":
        if not args.product:
            print("--product required")
            sys.exit(1)
        data = feed.get_ticker(args.product)
        print(json.dumps(data, indent=2) if data else "No data")

    elif args.command == "book":
        if not args.product:
            print("--product required")
            sys.exit(1)
        data = feed.get_book(args.product)
        print(json.dumps(data, indent=2) if data else "No data")

    elif args.command == "candles":
        if not args.product:
            print("--product required")
            sys.exit(1)
        data = feed.get_candles(args.product, args.granularity, args.limit)
        print(json.dumps(data[:5], indent=2) if data else "No data")

    elif args.command == "products":
        data = feed.list_products(symbol=args.symbol)
        print(f"Found {len(data)} products")
        for p in data[:10]:
            print(f"  {p.get('product_id'):15} {p.get('base_name','?'):10} price={p.get('price','?')}")

    elif args.command == "balance":
        data = feed.balance()
        print(json.dumps(data, indent=2) if data else "No data")
