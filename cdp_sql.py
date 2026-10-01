#!/usr/bin/env python3
"""
Coinbase CDP SQL API client for Base on-chain data.

Free tier: 1,000 queries/month at 2 queries/sec.
Requires a free CDP API key from https://portal.cdp.coinbase.com/

Endpoint: POST https://api.cdp.coinbase.com/platform/v2/data/query/run

NOTE: The CDP SQL Playground UI is free without a key, but the REST API
requires authentication. There is no unauthenticated programmatic access.
"""

import json
import os
import urllib.request
from typing import Any

CDP_SQL_URL = "https://api.cdp.coinbase.com/platform/v2/data/query/run"
CDP_API_KEY = os.getenv("CDP_API_KEY", "")


def is_configured() -> bool:
    """Check if CDP API key is available."""
    return bool(CDP_API_KEY)


def run_query(sql: str, max_age_ms: int = 500) -> dict[str, Any]:
    """
    Run a CoinbaSeQL query against Base/Solana data.
    
    Args:
        sql: CoinbaSeQL query string
        max_age_ms: Cache max age in milliseconds (default 500ms)
    
    Returns:
        dict with query results or error
    """
    if not is_configured():
        return {"error": "CDP_API_KEY not set", "configured": False}
    
    payload = json.dumps({
        "sql": sql,
        "cache": {"maxAgeMs": max_age_ms},
    }).encode()
    
    req = urllib.request.Request(
        CDP_SQL_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {CDP_API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
        },
    )
    
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
            return {
                "configured": True,
                "results": data.get("results", []),
                "metadata": data.get("metadata", {}),
            }
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        return {
            "configured": True,
            "error": f"HTTP {e.code}: {body}",
        }
    except Exception as e:
        return {
            "configured": True,
            "error": str(e),
        }


# ========================
# Base-specific queries
# ========================

def get_base_pool_activity(pool_address: str, limit: int = 100) -> dict:
    """
    Get recent swap events for a Base DEX pool.
    Uses base.events table with topic filters.
    """
    sql = f"""
    SELECT 
        block_number,
        transaction_hash,
        log_index,
        event_name,
        decoded_event,
        timestamp
    FROM base.events
    WHERE address = '{pool_address.lower()}'
      AND event_name IN ('Swap', 'Sync', 'Mint', 'Burn')
    ORDER BY block_number DESC
    LIMIT {limit}
    """
    return run_query(sql)


def get_base_token_transfers(token_address: str, limit: int = 100) -> dict:
    """
    Get recent ERC20 Transfer events for a token on Base.
    """
    sql = f"""
    SELECT 
        block_number,
        transaction_hash,
        log_index,
        decoded_event
    FROM base.events
    WHERE address = '{token_address.lower()}'
      AND event_name = 'Transfer'
    ORDER BY block_number DESC
    LIMIT {limit}
    """
    return run_query(sql)


def get_base_gas_trends(limit: int = 20) -> dict:
    """
    Get recent Base block gas data for spike detection.
    """
    sql = f"""
    SELECT 
        block_number,
        timestamp,
        gas_used,
        gas_limit,
        base_fee_per_gas,
        transaction_count
    FROM base.blocks
    WHERE chain = 'base'
    ORDER BY block_number DESC
    LIMIT {limit}
    """
    return run_query(sql)


def get_base_dex_swaps(pool_address: str, since_block: int = 0, limit: int = 50) -> dict:
    """
    Get swap events from a specific pool since a given block.
    """
    sql = f"""
    SELECT 
        block_number,
        transaction_hash,
        timestamp,
        decoded_event
    FROM base.events
    WHERE address = '{pool_address.lower()}'
      AND event_name = 'Swap'
      AND block_number > {since_block}
    ORDER BY block_number ASC
    LIMIT {limit}
    """
    return run_query(sql)


if __name__ == "__main__":
    # Test connection
    if is_configured():
        print("CDP SQL API: configured")
        result = run_query("SELECT 1 as test")
        print(json.dumps(result, indent=2))
    else:
        print("CDP SQL API: not configured (set CDP_API_KEY env var)")
        print("Get a free key at https://portal.cdp.coinbase.com/")
