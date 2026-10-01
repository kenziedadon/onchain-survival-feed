#!/usr/bin/env python3
"""
ops-briefing status report — reads ground truth, prints an HONEST status.

Self-contained: reads caps.json, risk_state.json, holdings.json,
portfolio_state.json, and on-chain balance, then reports what is ACTUALLY
true. Never repeats a stale status claim without re-checking source of truth.

Designed to run as a cron routine for the ops-briefing bot profile. Prints the
report to stdout; the cron job delivers it (telegram/origin/local).

Usage:
  python3 ops_briefing_status.py
"""
import json
import sys
from datetime import datetime, timezone

from risk import load_caps, _live_balance, _edge_evidence_ok

RPCS = [
    "https://base-rpc.publicnode.com",
    "https://1rpc.io/base",
    "https://base.drpc.org",
]
WALLET = "0xe7a16Be2aF1eCE8922d9d3aD55C03e29004e540F"


def _rpc_balance():
    import urllib.request
    for url in RPCS:
        try:
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_getBalance",
                               "params": [WALLET, "latest"]}).encode()
            req = urllib.request.Request(url, data=body,
                                         headers={"Content-Type": "application/json",
                                                   "User-Agent": "Mozilla/5.0 ops-briefing/1.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                hexbal = json.loads(r.read())["result"]
            return int(hexbal, 16) / 1e18
        except Exception:
            continue
    return None


def main():
    c = load_caps()
    s = json.load(open("risk_state.json")) if __import__("os").path.exists("risk_state.json") else {}
    holdings = json.load(open("holdings.json")) if __import__("os").path.exists("holdings.json") else {}
    pf = json.load(open("portfolio_state.json")) if __import__("os").path.exists("portfolio_state.json") else {}
    eval_path = "methodology_eval.json"

    bal = _rpc_balance()
    ev_ok, ev_why = _edge_evidence_ok(c)

    print("=" * 54)
    print(f"  BASE TREND BOT — STATUS  ({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')})")
    print("=" * 54)
    print(f"  Mode:        {'ARMED live_trading=true' if c.get('live_trading') else 'DRY-RUN (live_trading=false)'}")
    print(f"  Kill switch: {c.get('kill_switch')}")
    print(f"  Confirm:     {c.get('require_confirmation')}  (no auto-broadcast)")
    # Deep drift check: caps claims edge_evidence valid, but the eval file may
    # disagree (caps.json is set by the arm; the eval is the actual evidence).
    eval_drift = None
    if __import__("os").path.exists(eval_path):
        ev = json.load(open(eval_path))
        if c.get("live_trading") and ev.get("any_passed") is False:
            eval_drift = (f"caps.json claims edge_evidence valid but {eval_path} "
                          f"(ts {ev.get('ts','?')}) shows passed={ev.get('passed')}, "
                          f"any_passed=false — arm is NOT backed by a passing eval")
    print(f"  Edge evid:   {ev_why}  [status={c.get('edge_evidence', {}).get('status')}]")
    ee_status = (c.get("edge_evidence") or {}).get("status")
    ee_detail = (c.get("edge_evidence") or {}).get("detail")
    if ee_status == "PENDING_HUMAN_ARM":
        print("  !! EDGE GATE: awaiting explicit human arm — not auto-armed.")
    if ee_detail and "floor" in ee_detail and "n=" in ee_detail:
        print(f"  !! SAMPLE FLOOR: {ee_detail}")
    if eval_drift:
        print(f"  !! {eval_drift}")
    print(f"  Strategy:    {c.get('strategy', {}).get('name', '?')}")
    print(f"  Sizing cap:  {c.get('max_trade_eth')} ETH  | slippage cap {c.get('max_slippage_bps')}bps")
    print(f"  Wallet:      {WALLET}")
    print(f"  On-chain:    {('%.6f ETH' % bal) if bal is not None else 'RPC UNREACHABLE'}")
    print(f"  PnL (ETH):   {s.get('pnl_eth', 0.0):.6f}  | trades filled: {len(s.get('trades', []))}")
    print(f"  Blocks:      {len(s.get('blocked', []))}  | officer vetoes: {len(s.get('officer_vetoes', []))}")
    print(f"  Holdings:    {json.dumps(holdings)[:80] if holdings else '{}'}")
    # Drift check: stated live vs ground truth
    if c.get("live_trading") and not ev_ok:
        print("  !! DRIFT: live_trading=true but edge evidence invalid -> GUARD would disarm")
    print("=" * 54)


if __name__ == "__main__":
    main()
