#!/usr/bin/env python3
"""
execute_buy.py — Hermes Vanguard EOA execution bridge.

This is the "EOA circuit-breaker" the daemon calls. It does NOT self-sign; it
delegates the actual broadcast to the existing, vetted swap.js layer
(node swap.js <tokenOut> <amountEth> <slippageBps>) which reads the payer key
from .payer_key_tmp and broadcasts a self-signed tx on Base mainnet.

Safety gates enforced HERE (hard, before any broadcast):
  1. caps.json kill_switch  -> refuse if armed (exit 2)
  2. per-trade cap          -> amount_eth <= caps.max_trade_eth
  3. daily loss cap         -> cumulative logged spend <= caps.max_daily_loss_eth
  4. pre-trade balance      -> amount_eth + gas buffer <= on-chain ETH balance

Usage:
  python3 execute_buy.py <token_address> <amount_eth> [--slippage 100] [--dry-run]

Exit codes: 0 success | 1 swap failed | 2 safety-blocked | 3 usage/input error
"""
import sys, os, json, argparse, subprocess, urllib.request, time

HERE = os.path.dirname(os.path.abspath(__file__))
SWAP_JS = os.path.join(HERE, "swap.js")
CAPS = os.path.join(HERE, "caps.json")
SPEND_LOG = os.path.join(HERE, "eoa_spend_log.json")
WALLET = "0xe7a16Be2aF1eCE8922d9d3aD55C03e29004e540F"
RPCS = ["https://mainnet.base.org", "https://base.llamarpc.com", "https://base.meowrpc.com"]
GAS_BUFFER_ETH = 0.00006  # ~1.5x a typical Base swap gas cost, kept as safety margin
HTTP_TO = 10


def _http_post(url, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=HTTP_TO) as r:
        return json.loads(r.read().decode())


def load_caps():
    with open(CAPS) as f:
        return json.load(f)


def load_spend():
    if os.path.exists(SPEND_LOG):
        try:
            with open(SPEND_LOG) as f:
                return json.load(f)
        except Exception:
            pass
    return {"date": "", "spent_eth": 0.0, "trades": []}


def save_spend(s):
    with open(SPEND_LOG, "w") as f:
        json.dump(s, f, indent=2)


def onchain_balance():
    today = time.strftime("%Y-%m-%d")
    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_getBalance",
               "params": [WALLET, "latest"]}
    for ep in RPCS:
        try:
            res = _http_post(ep, payload)
            if "result" in res:
                bal = int(res["result"], 16) / 1e18
                return bal
        except Exception:
            continue
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("token_address")
    ap.add_argument("amount_eth", type=float)
    ap.add_argument("--slippage", type=int, default=100)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    tok = args.token_address.strip()
    if not tok.startswith("0x") or len(tok) != 42:
        print(json.dumps({"status": "error", "reason": "invalid_token_address"}))
        sys.exit(3)
    if args.amount_eth <= 0:
        print(json.dumps({"status": "error", "reason": "amount_must_be_positive"}))
        sys.exit(3)

    caps = load_caps()

    # Gate 1: kill switch
    if caps.get("kill_switch", False) and not args.dry_run:
        print(json.dumps({"status": "BLOCKED", "reason": "kill_switch_armed",
                          "detail": "caps.json kill_switch=true — daemon will not broadcast"}))
        sys.exit(2)

    # Gate 2: per-trade cap
    max_trade = float(caps.get("max_trade_eth", 0.0005))
    if args.amount_eth > max_trade:
        print(json.dumps({"status": "BLOCKED", "reason": "exceeds_max_trade_eth",
                          "max_trade_eth": max_trade, "requested": args.amount_eth}))
        sys.exit(2)

    # Gate 3: daily loss cap (cumulative logged spend)
    spend = load_spend()
    today = time.strftime("%Y-%m-%d")
    if spend.get("date") != today:
        spend = {"date": today, "spent_eth": 0.0, "trades": []}
    max_daily = float(caps.get("max_daily_loss_eth", 0.02))
    if spend["spent_eth"] + args.amount_eth > max_daily:
        print(json.dumps({"status": "BLOCKED", "reason": "exceeds_max_daily_loss_eth",
                          "max_daily_loss_eth": max_daily,
                          "already_spent": spend["spent_eth"], "requested": args.amount_eth}))
        sys.exit(2)

    # Gate 4: pre-trade balance
    bal = onchain_balance()
    if bal is None:
        print(json.dumps({"status": "BLOCKED", "reason": "balance_unreadable",
                          "detail": "could not read on-chain ETH balance"}))
        sys.exit(2)
    if args.amount_eth + GAS_BUFFER_ETH > bal:
        print(json.dumps({"status": "BLOCKED", "reason": "insufficient_balance",
                          "balance_eth": bal, "needed_eth": args.amount_eth + GAS_BUFFER_ETH}))
        sys.exit(2)

    if args.dry_run:
        print(json.dumps({"status": "ALLOWED_DRYRUN", "token": tok,
                          "amount_eth": args.amount_eth, "balance_eth": bal,
                          "daily_spent_eth": spend["spent_eth"]}))
        sys.exit(0)

    # ---- Broadcast via the vetted swap.js layer ----
    print(f"[EXECUTOR] Broadcasting BUY: {args.amount_eth} ETH -> {tok} (slip {args.slippage}bps)")
    try:
        res = subprocess.run(
            ["node", SWAP_JS, tok, str(args.amount_eth), str(args.slippage)],
            cwd=HERE, capture_output=True, text=True, timeout=120,
        )
    except Exception as e:
        print(json.dumps({"status": "ERROR", "reason": f"swap_js_invoke_failed: {e}"}))
        sys.exit(1)

    out = (res.stdout + res.stderr).strip()
    print(out)
    if res.returncode != 0:
        print(json.dumps({"status": "FAILED", "returncode": res.returncode}))
        sys.exit(1)

    # Log the spend (circuit-breaker accounting)
    spend["spent_eth"] = round(spend["spent_eth"] + args.amount_eth, 6)
    spend["trades"].append({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                            "token": tok, "amount_eth": args.amount_eth})
    save_spend(spend)

    print(json.dumps({"status": "SUCCESS", "token": tok, "amount_eth": args.amount_eth,
                      "daily_spent_eth": spend["spent_eth"]}))
    sys.exit(0)


if __name__ == "__main__":
    main()
