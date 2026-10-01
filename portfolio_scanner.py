#!/usr/bin/env python3
"""
Portfolio Scanner — full wallet inventory + management.

Scans all ERC-20 tokens in the wallet, classifies them, and provides
a unified view for the executor to manage (not just track capital).

Classification:
- bluechip: User-defined (Basecat, kenzodadon84) — track with exit conditions
- spam/dust: Known spam (BRETT, MORPHO, MIGGLES, KEYCAT, etc.) — sell/unwrap
- native: ETH/WETH — unwrap WETH to ETH
- unknown: New tokens — flag for review

Usage:
    from portfolio_scanner import PortfolioScanner
    scanner = PortfolioScanner()
    portfolio = scanner.scan_wallet()
    scanner.sell_dust()  # Sell all spam tokens above floor
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))

# Token classification database
# Address (lowercase) -> {name, classification, action}
TOKEN_DB = {
    # Native
    "0x4200000000000000000000000000000000000006": {"name": "WETH", "classification": "native", "action": "unwrap"},
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": {"name": "USDC", "classification": "stable", "action": "hold"},
    
    # Bluechips (user-defined)
    "0xb2000000000000000000004c27f6523082f41d01": {"name": "Basecat", "classification": "bluechip", "action": "track"},
    "0x46fee5e7a02b374700fde31d82a9a157d8915f46": {"name": "kenzodadon84", "classification": "bluechip", "action": "track"},
    
    # Spam/Dust (sell candidates)
    "0x532f27101965dd16442e59d40670faf5ebb142e4": {"name": "BRETT", "classification": "spam", "action": "sell"},
    "0xbaa5cc21fd487b8fcc2f632f3f4e8d37262a0842": {"name": "MORPHO", "classification": "spam", "action": "sell"},
    "0xb1a03eda10342529bbf8eb700a06c60441fef25d": {"name": "MIGGLES", "classification": "spam", "action": "sell"},
    "0x9a26f5433671751c3276a065f57e5a02d2817973": {"name": "KEYCAT", "classification": "spam", "action": "sell"},
    "0x535930aef284e6e60456cbca83f8d090214b19ce": {"name": "AGENT", "classification": "spam", "action": "sell"},
    "0x16171b069184a1998c8ecda99f1d941d86ccdc19": {"name": "ABTC", "classification": "spam", "action": "sell"},
    "0x9286f0fc8ad73585f9e61996d757799f33d02076": {"name": "BOA", "classification": "spam", "action": "sell"},
    "0xb2000000000000000000006b96e07b49964fc43a": {"name": "HOUSE", "classification": "spam", "action": "sell"},
    "0x78cdbef65ede022351f2dd635abc41ac74788cbd": {"name": "GENESIS", "classification": "spam", "action": "sell"},
    "0x538dbc33787c9f12aa824b6597e29a41d644ca41": {"name": "GrokBot", "classification": "spam", "action": "sell"},
    "0x23771541922dbc3be3210f683607740db733f34a": {"name": "KEITA", "classification": "spam", "action": "sell"},
    "0x54176867114ada4e961fc74e677e21ccc771c677": {"name": "LILPEPE", "classification": "spam", "action": "sell"},
    "0x740644d7af50e0e83412eb6271a455c91f412dc7": {"name": "MSPEED", "classification": "spam", "action": "sell"},
    "0x486f662020286e17e7469df4e5f2cf2415f36662": {"name": "AI", "classification": "spam", "action": "sell"},
    "0x27740c050e3764a2cee00317b7ac68a52d11ab5f": {"name": "PEPETO", "classification": "spam", "action": "sell"},
    "0x0c10c8eab05b2ab762ff0a39ef27fbf9f4fb31ed": {"name": "SOAS", "classification": "spam", "action": "sell"},
    "0x53815022135f3c66799c8393eafe1038ae92ebcb": {"name": "CNPY", "classification": "spam", "action": "sell"},
}

# Known DEXScreener spam patterns (low liquidity, no volume)
SPAM_PATTERNS = ["ABTC", "BOA", "HOUSE", "GENESIS", "GrokBot", "KEITA", "LILPEPE", "MSPEED", "AI", "PEPETO", "SOAS", "CNPY", "AGENT"]


class PortfolioScanner:
    """Scan and manage all tokens in the wallet."""
    
    def __init__(self, rpc_endpoints=None):
        self.rpc_endpoints = rpc_endpoints or [
            "https://base.meowrpc.com",
            "https://base.llamarpc.com",
            "https://base-pokt.nodies.app",
            "https://base-rpc.publicnode.com",
            "https://rpc.ankr.com/base",
            "https://1rpc.io/base",
        ]
        self.ua = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
        self._wallet = None
        self._portfolio_file = os.path.join(HERE, "portfolio_state.json")
    
    def _wallet_address(self):
        if self._wallet:
            return self._wallet
        try:
            w = json.load(open(os.path.join(HERE, "wallet.json")))
            self._wallet = (w.get("trading_address") or w.get("address")).lower()
        except Exception:
            self._wallet = "0xe7a16be2af1ece8922d9d3ad55c03e29004e540f"
        return self._wallet
    
    def _rpc_call(self, payload, timeout=15):
        """Try multiple RPCs until one works."""
        for url in self.rpc_endpoints:
            try:
                req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=self.ua)
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return json.loads(r.read())
            except Exception:
                continue
        return None
    
    def get_eth_balance(self):
        """Get native ETH balance."""
        addr = self._wallet_address()
        payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_getBalance", "params": [addr, "latest"]}
        result = self._rpc_call(payload)
        if result and result.get("result"):
            return int(result["result"], 16) / 1e18
        return 0.0
    
    def get_token_balance(self, token_addr):
        """Get ERC-20 balance for a token."""
        addr = self._wallet_address()
        sig = "0x70a08231" + addr[2:].rjust(64, "0")
        payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [{"to": token_addr, "data": sig}, "latest"]}
        result = self._rpc_call(payload)
        if result and result.get("result"):
            return int(result["result"], 16)
        return 0
    
    def scan_known_tokens(self):
        """Scan all tokens in TOKEN_DB. Returns dict keyed by lowercase address."""
        addr = self._wallet_address()
        results = {}
        
        # ETH (key = "native")
        eth_bal = self.get_eth_balance()
        if eth_bal > 0:
            results["native"] = {
                "address": "native",
                "symbol": "ETH",
                "name": "Ether",
                "balance": eth_bal,
                "decimals": 18,
                "classification": "native",
                "action": "hold",
            }
        
        # ERC-20s (key = lowercase address)
        for token_addr, info in TOKEN_DB.items():
            raw = self.get_token_balance(token_addr)
            if raw > 0:
                decimals = 6 if info["name"] == "USDC" else 18
                balance = raw / (10 ** decimals)
                results[token_addr.lower()] = {
                    "address": token_addr,
                    "symbol": info["name"],
                    "name": info["name"],
                    "balance": balance,
                    "raw": raw,
                    "decimals": decimals,
                    "classification": info["classification"],
                    "action": info["action"],
                }
        
        return results
    
    def scan_wallet_full(self):
        """
        Full wallet scan using BaseScan API or similar.
        Returns all ERC-20 tokens with non-zero balance.
        """
        addr = self._wallet_address()
        tokens = {}
        
        # Use BaseScan API (no key needed for basic endpoint)
        try:
            url = f"https://api.basescan.org/v2/api?module=account&action=tokenlist&address={addr}&page=1&offset=100"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read())
                if data.get("status") == "1":
                    for t in data.get("result", []):
                        addr_lower = t.get("contractAddress", "").lower()
                        balance = int(t.get("balance", 0))
                        if balance > 0:
                            decimals = int(t.get("decimals", 18))
                            sym = t.get("symbol", "?")
                            tokens[addr_lower] = {
                                "symbol": sym,
                                "name": t.get("name", "?"),
                                "balance": balance / (10 ** decimals),
                                "raw": balance,
                                "decimals": decimals,
                                "address": addr_lower,
                            }
        except Exception as e:
            print(f"BaseScan API error: {e}", file=sys.stderr)
        
        return tokens
    
    def classify_token(self, address, symbol="?", name="?"):
        """Classify a token based on known database or heuristics."""
        addr_lower = address.lower() if address != "native" else "native"
        
        # Check known database
        if addr_lower in TOKEN_DB:
            return TOKEN_DB[addr_lower]["classification"]
        
        # Check symbol patterns
        if symbol in SPAM_PATTERNS:
            return "spam"
        
        # Unknown
        return "unknown"
    
    def get_portfolio(self):
        """Get full portfolio with classifications."""
        # Try full scan first
        all_tokens = self.scan_wallet_full()
        
        # If that fails, fall back to known tokens
        if not all_tokens:
            all_tokens = self.scan_known_tokens()
        
        # Classify and organize
        portfolio = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "wallet": self._wallet_address(),
            "tokens": {},
            "summary": {
                "total_tokens": 0,
                "bluechip_value": 0,
                "spam_count": 0,
                "native_value": 0,
            }
        }
        
        for addr, info in all_tokens.items():
            symbol = info.get("symbol", info.get("name", "?"))
            classification = self.classify_token(addr, symbol, info.get("name", "?"))
            
            portfolio["tokens"][addr] = {
                "symbol": symbol,
                "name": info.get("name", "?"),
                "balance": info.get("balance", 0),
                "raw": info.get("raw", 0),
                "decimals": info.get("decimals", 18),
                "address": addr,
                "classification": classification,
                "action": self._get_action(classification, info),
            }
            
            portfolio["summary"]["total_tokens"] += 1
            if classification == "spam":
                portfolio["summary"]["spam_count"] += 1
        
        # Save to file
        with open(self._portfolio_file, "w") as f:
            json.dump(portfolio, f, indent=2)
        
        return portfolio
    
    def _get_action(self, classification, info):
        """Determine action for a token."""
        if classification == "native":
            return "hold"
        elif classification == "stable":
            return "hold"
        elif classification == "bluechip":
            return "track"
        elif classification == "spam":
            return "sell"
        else:
            return "review"
    
    def get_sell_list(self, min_usd_value=0.01):
        """Get list of tokens to sell (spam above floor)."""
        portfolio = self.get_portfolio()
        sell_list = []
        
        for addr, info in portfolio["tokens"].items():
            if info["classification"] == "spam" and info["balance"] > 0:
                # Check if token has liquidity on DEX
                sell_list.append({
                    "symbol": info["symbol"],
                    "address": addr,
                    "balance": info["balance"],
                    "reason": "spam/dust",
                })
        
        return sell_list
    
    def get_bluechip_positions(self):
        """Get bluechip tokens that should be tracked."""
        portfolio = self.get_portfolio()
        positions = {}
        
        for addr, info in portfolio["tokens"].items():
            if info["classification"] == "bluechip" and info["balance"] > 0:
                positions[addr] = info
        
        return positions
    
    def print_portfolio(self):
        """Print formatted portfolio."""
        portfolio = self.get_portfolio()
        
        print("=" * 60)
        print("PORTFOLIO SCAN")
        print(f"Wallet: {portfolio['wallet']}")
        print(f"Time: {portfolio['timestamp']}")
        print("=" * 60)
        
        # Group by classification
        groups = {"native": [], "stable": [], "bluechip": [], "spam": [], "unknown": []}
        for addr, info in portfolio["tokens"].items():
            groups.get(info["classification"], []).append(info)
        
        if groups["native"]:
            print("\n--- Native ---")
            for t in groups["native"]:
                print(f"  {t['symbol']:10} {t['balance']:>15,.6f}")
        
        if groups["stable"]:
            print("\n--- Stable ---")
            for t in groups["stable"]:
                print(f"  {t['symbol']:10} {t['balance']:>15,.6f}")
        
        if groups["bluechip"]:
            print("\n--- Bluechip (Track) ---")
            for t in groups["bluechip"]:
                print(f"  {t['symbol']:10} {t['balance']:>15,.6f}")
        
        if groups["spam"]:
            print(f"\n--- Spam/Dust ({len(groups['spam'])} tokens) ---")
            for t in groups["spam"]:
                print(f"  {t['symbol']:10} {t['balance']:>15,.6f}")
        
        if groups["unknown"]:
            print(f"\n--- Unknown ({len(groups['unknown'])} tokens) ---")
            for t in groups["unknown"]:
                print(f"  {t['symbol']:10} {t['balance']:>15,.6f}")
        
        print(f"\nTotal tokens: {portfolio['summary']['total_tokens']}")
        print(f"Spam tokens to sell: {portfolio['summary']['spam_count']}")
        print("=" * 60)


def scan_wallet():
    """Quick scan function."""
    scanner = PortfolioScanner()
    return scanner.get_portfolio()


if __name__ == "__main__":
    scanner = PortfolioScanner()
    scanner.print_portfolio()
