#!/usr/bin/env python3
"""
BUILD CHECK — verify the trading build is sane before any live action.
Run: python3 checks.py
"""
import json, os, sys, urllib.request, time

HERE = os.path.dirname(os.path.abspath(__file__))
WALLET_FILE = os.path.join(HERE, "wallet.json")
CAPS_FILE = os.path.join(HERE, "caps.json")
HOLDINGS_FILE = os.path.join(HERE, "holdings.json")
SWAP_JS = os.path.join(HERE, "swap.js")
STATE_FILE = os.path.join(HERE, "risk_state.json")

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

errors = []
warnings = []
info = []


def rpc_call(payload, retries=1):
    last_err = None
    for ep in [RPC, *RPC_FALLBACKS]:
        for i in range(retries):
            try:
                req = urllib.request.Request(ep, data=json.dumps(payload).encode(), headers=UA, method="POST")
                with urllib.request.urlopen(req, timeout=20) as r:
                    return json.loads(r.read()).get("result")
            except Exception as e:
                last_err = e
                time.sleep(0.3)
    return None


def check_files_exist():
    for name, path in [("wallet.json", WALLET_FILE), ("caps.json", CAPS_FILE), ("swap.js", SWAP_JS)]:
        if os.path.exists(path):
            info.append(f"file: {name} exists")
        else:
            errors.append(f"missing file: {name}")


def check_wallet():
    try:
        w = json.load(open(WALLET_FILE))
        addr = w.get("trading_address") or w.get("address")
        if not addr or not addr.startswith("0x") or len(addr) != 42:
            errors.append(f"wallet address malformed: {addr}")
            return
        info.append(f"wallet: {addr} type={w.get('type','?')}")
        raw = rpc_call({"jsonrpc": "2.0", "id": 1, "method": "eth_getBalance", "params": [addr, "latest"]})
        if raw:
            bal = int(raw, 16) / 1e18
            info.append(f"balance: {bal:.8f} ETH")
        else:
            errors.append("could not read wallet balance from any RPC")
    except Exception as e:
        errors.append(f"wallet.json unreadable: {e}")


def check_caps():
    try:
        c = json.load(open(CAPS_FILE))
        info.append(f"caps: live_trading={c.get('live_trading')} kill_switch={c.get('kill_switch')} require_confirmation={c.get('require_confirmation')}")
        if c.get("kill_switch") is not True:
            warnings.append("kill_switch is not True — live trades are possible")
        if c.get("live_trading") is not False:
            warnings.append("live_trading is not False — double-check this is intentional")
        if c.get("max_slippage_bps", 1000) > 200:
            warnings.append(f"max_slippage_bps={c.get('max_slippage_bps')} is high")
        if c.get("max_daily_loss_eth", 0) > 0.05:
            warnings.append(f"max_daily_loss_eth={c.get('max_daily_loss_eth')} is large for micro-scale")
        if c.get("require_confirmation") is not True:
            warnings.append("require_confirmation is False — auto-submit enabled")
    except Exception as e:
        errors.append(f"caps.json unreadable: {e}")


def check_holdings_vs_chain(wallet_addr):
    if not wallet_addr:
        warnings.append("holdings check skipped: no wallet address")
        return
    try:
        h = json.load(open(HOLDINGS_FILE))
        info.append(f"holdings: {len(h)} entries in holdings.json")
        if not h:
            info.append("holdings empty (OK after reconcile)")
            return
        sig = "0x70a08231" + wallet_addr[2:].lower().rjust(64, "0")
        for addr, pos in h.items():
            raw = rpc_call({"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [{"to": addr, "data": sig}, "latest"]})
            if raw and raw not in ("0x0", "0x00", "0x"):
                info.append(f"  {pos.get('name', addr[:8])}: on-chain nonzero")
            else:
                warnings.append(f"  {pos.get('name', addr[:8])}: on-chain balance 0 — stale holding?")
    except Exception as e:
        warnings.append(f"holdings check skipped: {e}")


def check_swap_js():
    try:
        txt = open(SWAP_JS).read()
        if "EXPECTED" not in txt:
            errors.append("swap.js missing EXPECTED wallet check")
        if "RPC_FALLBACKS" not in txt:
            warnings.append("swap.js missing RPC_FALLBACKS")
        if "withRetry" not in txt:
            warnings.append("swap.js missing withRetry")
        info.append("swap.js: config present")
    except Exception as e:
        errors.append(f"swap.js unreadable: {e}")


def main():
    print("=== BUILD CHECK ===\n")
    wallet_addr = None
    try:
        w = json.load(open(WALLET_FILE))
        wallet_addr = (w.get("trading_address") or w.get("address") or "").strip()
    except Exception:
        pass

    check_files_exist()
    check_wallet()
    check_caps()
    check_holdings_vs_chain(wallet_addr)
    check_swap_js()

    print("INFO:")
    for i in info:
        print(f"  OK: {i}")
    print("\nWARNINGS:")
    for w in warnings:
        print(f"  WARN: {w}")
    print("\nERRORS:")
    for e in errors:
        print(f"  FAIL: {e}")

    if errors:
        print(f"\nRESULT: FAIL — {len(errors)} error(s), {len(warnings)} warning(s)")
        sys.exit(1)
    elif warnings:
        print(f"\nRESULT: WARN — {len(warnings)} warning(s), no errors")
        sys.exit(0)
    else:
        print("\nRESULT: OK — build clean")
        sys.exit(0)


if __name__ == "__main__":
    main()
