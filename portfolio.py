#!/usr/bin/env python3
"""
Portfolio Manager — full wallet awareness for the executor.

Uses base_data.py for reliable RPC calls with failover.

Makes the bot aware of ALL tokens in the wallet (not just ETH),
so it can:
- Track bluechip tokens (Basecat, kenzodadon84) with exit conditions
- Avoid accidentally trying to sell spam tokens (no liquidity)
- Use USDC for USDC-quote pair trading
- Sweep dust into ETH when possible
"""
import json
import os
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# Use base_data for RPC calls
from base_data import BaseData, RPCError, WETH, USDC

# Token database: address (lowercase) -> metadata
TOKEN_DB = {
    # Native
    "native": {"symbol": "ETH", "name": "Ether", "decimals": 18, "classification": "native"},
    "0x4200000000000000000000000000000000000006": {"symbol": "WETH", "name": "Wrapped Ether", "decimals": 18, "classification": "native"},
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": {"symbol": "USDC", "name": "USDC", "decimals": 6, "classification": "stable"},
    
    # Bluechips (user-defined)
    "0xb2000000000000000000004c27f6523082f41d01": {"symbol": "Basecat", "name": "Basecat", "decimals": 18, "classification": "bluechip"},
    "0x46fee5e7a02b374700fde31d82a9a157d8915f46": {"symbol": "kenzodadon84", "name": "kenzodadon84", "decimals": 18, "classification": "bluechip"},
    
    # Bluechips
    "0x532f27101965dd16442e59d40670faf5ebb142e4": {"symbol": "BRETT", "name": "Brett", "decimals": 18, "classification": "bluechip"},
    "0xbaa5cc21fd487b8fcc2f632f3f4e8d37262a0842": {"symbol": "MORPHO", "name": "Morpho", "decimals": 18, "classification": "bluechip"},
    "0xb1a03eda10342529bbf8eb700a06c60441fef25d": {"symbol": "MIGGLES", "name": "Mr. Miggles", "decimals": 18, "classification": "bluechip"},
    "0x9a26f5433671751c3276a065f57e5a02d2817973": {"symbol": "KEYCAT", "name": "Keyboard Cat", "decimals": 18, "classification": "bluechip"},
    "0x9b5e262cf9bb04869ab40b19af91d2dc85761722": {"symbol": "NOCK", "name": "NOCK", "decimals": 16, "classification": "bluechip"},
    "0xb095274743941e953c746f9c228da9c18bb6ec29": {"symbol": "LAPTOP", "name": "LAPTOP", "decimals": 18, "classification": "bluechip"},
    "0xb2000000000000000000004884b426556b92883d": {"symbol": "MSTRc", "name": "Strategy Inc.", "decimals": 18, "classification": "bluechip"},
    "0x8b67f2e56139ca052a7ec49cbcd1aa9c83f2752a": {"symbol": "MUTATIO", "name": "MUTATIO", "decimals": 18, "classification": "bluechip"},
    "0xb20000000000000000000074deac544e1e9a1f33": {"symbol": "STONKFLY", "name": "STONKFLY", "decimals": 18, "classification": "bluechip"},
    "0x535930aef284e6e60456cbca83f8d090214b19ce": {"symbol": "AGENT", "name": "AI Agent", "decimals": 18, "classification": "spam"},
    "0x16171b069184a1998c8ecda99f1d941d86ccdc19": {"symbol": "ABTC", "name": "American Bitcoin", "decimals": 18, "classification": "spam"},
    "0x9286f0fc8ad73585f9e61996d757799f33d02076": {"symbol": "BOA", "name": "Base Owl", "decimals": 18, "classification": "spam"},
    "0xb2000000000000000000006b96e07b49964fc43a": {"symbol": "HOUSE", "name": "DOG", "decimals": 18, "classification": "spam"},
    "0x78cdbef65ede022351f2dd635abc41ac74788cbd": {"symbol": "GENESIS", "name": "GENESIS", "decimals": 18, "classification": "spam"},
    "0x538dbc33787c9f12aa824b6597e29a41d644ca41": {"symbol": "GrokBot", "name": "GrokBot", "decimals": 18, "classification": "spam"},
    "0x23771541922dbc3be3210f683607740db733f34a": {"symbol": "KEITA", "name": "KEITA", "decimals": 18, "classification": "spam"},
    "0x54176867114ada4e961fc74e677e21ccc771c677": {"symbol": "LILPEPE", "name": "Little Pepe", "decimals": 18, "classification": "spam"},
    "0x740644d7af50e0e83412eb6271a455c91f412dc7": {"symbol": "MSPEED", "name": "Mr. Speed", "decimals": 18, "classification": "spam"},
    "0x486f662020286e17e7469df4e5f2cf2415f36662": {"symbol": "AI", "name": "OpenAI", "decimals": 18, "classification": "spam"},
    "0x27740c050e3764a2cee00317b7ac68a52d11ab5f": {"symbol": "PEPETO", "name": "Pepe Today", "decimals": 18, "classification": "spam"},
    "0x0c10c8eab05b2ab762ff0a39ef27fbf9f4fb31ed": {"symbol": "SOAS", "name": "Soas", "decimals": 18, "classification": "spam"},
    "0x53815022135f3c66799c8393eafe1038ae92ebcb": {"symbol": "CNPY", "name": "Compound CNPY", "decimals": 18, "classification": "spam"},
}


class PortfolioManager:
    """Manage and scan wallet portfolio using base_data."""
    
    def __init__(self):
        self.data = BaseData()
        self._wallet = None
        self._portfolio_file = os.path.join(HERE, "portfolio_state.json")
    
    def _wallet_address(self):
        if not self._wallet:
            try:
                w = json.load(open(os.path.join(HERE, "wallet.json")))
                self._wallet = (w.get("trading_address") or w.get("address")).lower()
            except Exception:
                self._wallet = "0xe7a16be2af1ece8922d9d3ad55c03e29004e540f"
        return self._wallet
    
    def get_eth_balance(self):
        """Get native ETH balance."""
        return self.data.balance_eth(self._wallet_address())
    
    def get_all_balances(self):
        """Get all token balances for the wallet."""
        portfolio = {}
        
        # ETH
        eth = self.get_eth_balance()
        if eth > 0:
            portfolio["ETH"] = {"balance": eth, "decimals": 18, "address": "native", "classification": "native"}
        
        # ERC-20s
        for addr, meta in TOKEN_DB.items():
            if addr == "native":
                continue
            bal = self.data.token_balance(addr, self._wallet_address())
            if bal > 0:
                portfolio[meta["symbol"]] = {
                    "balance": bal,
                    "decimals": meta["decimals"],
                    "address": addr,
                    "classification": meta["classification"],
                }
        
        return portfolio
    
    def get_portfolio(self):
        """Get full portfolio with classifications."""
        all_tokens = self.get_all_balances()
        
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
        
        for symbol, info in all_tokens.items():
            addr = info["address"]
            classification = info.get("classification", "unknown")
            
            portfolio["tokens"][addr] = {
                "symbol": symbol,
                "name": symbol,
                "balance": info.get("balance", 0),
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
    
    def get_bluechip_positions(self):
        """Get bluechip tokens that should be tracked."""
        portfolio = self.get_all_balances()
        positions = {}
        
        for symbol, info in portfolio.items():
            if info.get("classification") == "bluechip" and info.get("balance", 0) > 0:
                positions[info["address"]] = info
        
        return positions
    
    def get_sellable_tokens(self):
        """Get tokens that can be sold (spam above floor)."""
        portfolio = self.get_all_balances()
        sellable = []
        
        for symbol, info in portfolio.items():
            if info["classification"] == "spam" and info["balance"] > 0:
                sellable.append({
                    "symbol": symbol,
                    "address": info["address"],
                    "balance": info["balance"],
                    "classification": "spam",
                })
        
        return sellable
    
    def print_portfolio(self):
        """Print formatted portfolio."""
        portfolio = self.get_all_balances()
        
        print("=" * 60)
        print("PORTFOLIO SCAN")
        print(f"Wallet: {self._wallet_address()}")
        print(f"Time: {datetime.now(timezone.utc).isoformat()}")
        print("=" * 60)
        
        # Group by classification
        groups = {"native": [], "stable": [], "bluechip": [], "spam": [], "unknown": []}
        for symbol, info in sorted(portfolio.items()):
            cls = info.get("classification", "unknown")
            groups.setdefault(cls, []).append((symbol, info))
        
        if groups["native"]:
            print("\n--- Native ---")
            for s, i in groups["native"]:
                print(f"  {s:10} {i['balance']:>15,.6f}")
        
        if groups["stable"]:
            print("\n--- Stable ---")
            for s, i in groups["stable"]:
                print(f"  {s:10} {i['balance']:>15,.6f}")
        
        if groups["bluechip"]:
            print("\n--- Bluechip ---")
            for s, i in groups["bluechip"]:
                print(f"  {s:10} {i['balance']:>15,.6f}")
        
        if groups["spam"]:
            print(f"\n--- Spam/Dust ({len(groups['spam'])} tokens) ---")
            for s, i in groups["spam"]:
                print(f"  {s:10} {i['balance']:>15,.6f}")
        
        print(f"\nTotal: {len(portfolio)} tokens")


# --- Module-level convenience functions ---

def _wallet_address():
    """Get trading wallet address."""
    try:
        w = json.load(open(os.path.join(HERE, "wallet.json")))
        return (w.get("trading_address") or w.get("address")).lower()
    except Exception:
        return "0xe7a16be2af1ece8922d9d3ad55c03e29004e540f"


def get_all_balances():
    """Get all token balances."""
    mgr = PortfolioManager()
    return mgr.get_all_balances()


def get_eth_balance():
    """Get native ETH balance."""
    data = BaseData()
    return data.balance_eth(_wallet_address())


def get_token_balance(token_addr):
    """Get ERC-20 balance for a token."""
    data = BaseData()
    return data.token_balance(token_addr, _wallet_address())


def is_token_spam(token_addr):
    """Check if a token is spam."""
    addr = token_addr.lower()
    if addr in TOKEN_DB:
        return TOKEN_DB[addr]["classification"] == "spam"
    return False


def is_token_bluechip(token_addr):
    """Check if a token is bluechip."""
    addr = token_addr.lower()
    if addr in TOKEN_DB:
        return TOKEN_DB[addr]["classification"] == "bluechip"
    return False


def is_token_known(token_addr):
    """Check if a token is in the database."""
    addr = token_addr.lower()
    return addr in TOKEN_DB


def get_token_info(token_addr):
    """Get token metadata."""
    addr = token_addr.lower()
    if addr in TOKEN_DB:
        return TOKEN_DB[addr]
    return {"symbol": "?", "name": "Unknown", "decimals": 18, "classification": "unknown"}


# --- Enhanced holdings for executor ---

def _load_holdings_enhanced():
    """
    Enhanced holdings loader that scans the wallet for ALL tokens.
    
    Returns dict: {
        token_addr: {
            "balance": float,
            "entry_eth_value": float or None,
            "classification": str,
            "symbol": str,
            "is_wallet_token": True,
        }
    }
    """
    holdings = {}
    
    # Load bot's own holdings
    hf = os.path.join(HERE, "holdings.json")
    try:
        bot_holdings = json.load(open(hf))
    except Exception:
        bot_holdings = {}
    
    # Add bot holdings
    for addr, entry in bot_holdings.items():
        holdings[addr.lower()] = {
            "balance": entry.get("amount_token", 0),
            "entry_eth_value": entry.get("entry_eth_value"),
            "classification": "bot_bought",
            "symbol": entry.get("name", "?"),
            "is_wallet_token": False,
            "bot_entry": entry,
        }
    
    # Scan wallet for all tokens
    portfolio = get_all_balances()
    
    for symbol, info in portfolio.items():
        addr = info["address"]
        addr_lower = addr.lower() if addr != "native" else "native"
        
        # Skip if already tracked
        if addr_lower in holdings:
            holdings[addr_lower]["balance"] = info["balance"]
            continue
        
        # Skip native ETH
        if addr_lower == "native":
            continue
        
        holdings[addr_lower] = {
            "balance": info["balance"],
            "entry_eth_value": None,
            "classification": info["classification"],
            "symbol": symbol,
            "is_wallet_token": True,
            "bot_entry": None,
        }
    
    return holdings


def should_exit_token(token_addr):
    """Check if a token should be exited."""
    addr = token_addr.lower()
    info = get_token_info(addr)
    classification = info.get("classification", "unknown")
    
    if classification == "spam":
        return True, "spam token — exit if liquidity allows"
    if classification == "unknown":
        return False, "unknown token — manual review needed"
    if classification == "bluechip":
        return None, "bluechip — use normal exit conditions"
    return False, "native/stable — hold"


if __name__ == "__main__":
    mgr = PortfolioManager()
    mgr.print_portfolio()
