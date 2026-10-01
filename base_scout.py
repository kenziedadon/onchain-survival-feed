#!/usr/bin/env python3
"""
Base Scout — automated Base token monitoring.
Alerts on volume spikes (>25%), liquidity drops (>10%), or price breakouts (+15%).
State tracked in scout_state.json between runs.
"""
import json
import sys
import os
import urllib.request

STATE_FILE = "scout_state.json"
HOLDINGS_FILE = "holdings.json"
DISCOVERED_FILE = "discovered.jsonl"

# Static watchlist (always monitor these)
STATIC_WATCHLIST = {
    "0x94017f291504a799614ef2b9e13d3310b2b2f816": "AERO",
    "0x4ed4e862860bed51a9570b96d89af5e1b0efefed": "DEGEN",
}

# Alert thresholds
VOL_SPIKE_THRESHOLD = 1.25    # 25% volume increase
LIQ_DROP_THRESHOLD = 0.90     # 10% liquidity decrease
PRICE_BREAKOUT_THRESHOLD = 1.15  # 15% price increase between checks


def fetch_json(url, data=None, headers=None):
    if headers is None:
        headers = {'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'}
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.loads(response.read().decode('utf-8'))
    except Exception as e:
        return {"error": str(e)}


RPC = "https://mainnet.base.org"
RPC_FALLBACKS = [
    "https://base.llamarpc.com",
    "https://base.meowrpc.com",
    "https://base.drpc.org",
    "https://1rpc.io/base",
    "https://base-pokt.nodies.app",
    "https://rpc.ankr.com/base",
]
UA = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}


def query_base_rpc(method, params=[]):
    payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode('utf-8')
    last_err = None
    for url in [RPC, *RPC_FALLBACKS]:
        try:
            req = urllib.request.Request(url, data=payload, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as response:
                return json.loads(response.read().decode('utf-8'))
        except Exception as e:
            last_err = e
            continue
    return {"error": str(last_err)}


def get_network_indicators():
    block_res = query_base_rpc("eth_blockNumber")
    block_hex = block_res.get("result", "0x0")
    block_num = int(block_hex, 16) if block_hex else 0

    gas_res = query_base_rpc("eth_gasPrice")
    gas_hex = gas_res.get("result", "0x0")
    gas_gwei = (int(gas_hex, 16) / 1e9) if gas_hex else 0.0

    block_data = query_base_rpc("eth_getBlockByNumber", [block_hex, False])
    tx_count = len(block_data.get("result", {}).get("transactions", [])) if "result" in block_data and block_data["result"] else 0

    return {"block_height": block_num, "gas_price_gwei": round(gas_gwei, 4), "block_tx_density": tx_count}


def get_token_indicators(token_address):
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
    data = fetch_json(url)
    pairs = data.get("pairs", [])
    if not pairs:
        return None
    base_pairs = [p for p in pairs if p.get("chainId") == "base"]
    return base_pairs[0] if base_pairs else None


def _load_dynamic_targets():
    targets = {}

    # Include any tokens we currently hold
    if os.path.exists(HOLDINGS_FILE):
        try:
            with open(HOLDINGS_FILE, "r") as f:
                holdings = json.load(f)
            for addr, pos in holdings.items():
                name = pos.get("name") or pos.get("pool", "?")
                targets[addr] = f"HOLD:{name}"
        except Exception:
            pass

    # Include any tokens discovered by the bot but not yet bought
    if os.path.exists(DISCOVERED_FILE):
        try:
            with open(DISCOVERED_FILE, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    for coin in data.get("coins", []):
                        addr = coin.get("token_addr") or coin.get("address")
                        name = coin.get("name", addr)
                        if addr:
                            targets[addr] = f"DISC:{name}"
        except Exception:
            pass

    return targets


def load_previous_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_current_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def execute_cron_evaluation(use_moa=False):
    prev_state = load_previous_state()
    current_state = {}
    alerts = []

    # Merge static watchlist with dynamic holdings/discovery targets
    targets = dict(STATIC_WATCHLIST)
    targets.update(_load_dynamic_targets())

    if not targets:
        print(json.dumps({"status": "NO_TARGETS", "detail": "No static or dynamic tokens to monitor"}, indent=2))
        return

    for addr, symbol in targets.items():
        metrics = get_token_indicators(addr)
        if not metrics:
            continue

        current_vol = float(metrics.get("volume", {}).get("h24", 0))
        current_liq = float(metrics.get("liquidity", {}).get("usd", 0))
        current_price = float(metrics.get("priceUsd", 0))

        current_state[addr] = {
            "volume_24h": current_vol,
            "liquidity_usd": current_liq,
            "price_usd": current_price,
        }

        if addr not in prev_state:
            continue

        old = prev_state[addr]
        old_price = float(old.get("price_usd") or 0)

        # Volume spike
        if old.get("volume_24h", 0) > 0 and (current_vol / old["volume_24h"]) > VOL_SPIKE_THRESHOLD:
            alerts.append(
                f"⚠️ [VOL SPIKE] {symbol} 24h volume grew sharply "
                f"from ${old['volume_24h']:,.2f} to ${current_vol:,.2f}"
            )

        # Liquidity drop
        if old.get("liquidity_usd", 0) > 0 and (current_liq / old["liquidity_usd"]) < LIQ_DROP_THRESHOLD:
            alerts.append(
                f"🚨 [LIQUIDITY DROP] {symbol} lost >10% of Pool Depth! "
                f"Old: ${old['liquidity_usd']:,.2f} -> New: ${current_liq:,.2f}"
            )

        # Price breakout
        if old_price > 0 and (current_price / old_price) > PRICE_BREAKOUT_THRESHOLD:
            alerts.append(
                f"🚀 [PRICE BREAKOUT] {symbol} rose from ${old_price:,.6f} to ${current_price:,.6f} "
                f"({(current_price/old_price - 1)*100:+.1f}%)"
            )

    save_current_state(current_state)

    if alerts:
        print(json.dumps({"status": "ALERT_TRIGGERED", "events": alerts}, indent=2))
        if use_moa:
            try:
                from moa_research import enrich_alerts
                verdict = enrich_alerts(alerts)
                if verdict:
                    print("\n--- MoA synthesis (nous-free) ---")
                    print(verdict)
            except Exception as e:  # noqa: BLE001 - never let MoA break the monitor
                print(f"\n[moa] skipped: {e}")
    else:
        print(json.dumps({"status": "NO_SIGNIFICANT_CHANGE"}, indent=2))


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: python base_scout.py <network | token_address | cron [--moa]>"}, indent=2))
        sys.exit(1)

    use_moa = "--moa" in sys.argv[2:]
    mode = sys.argv[1].lower()
    if mode == "cron":
        execute_cron_evaluation(use_moa=use_moa)
    elif mode == "network":
        print(json.dumps(get_network_indicators(), indent=2))
    else:
        token_data = get_token_indicators(mode)
        print(json.dumps(token_data if token_data else {"error": "No pairs found"}, indent=2))


if __name__ == "__main__":
    main()
