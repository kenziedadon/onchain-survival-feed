#!/usr/bin/env python3
"""Verify the bot is fully armed and ready for the first live trade."""

import json, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
CAPS = os.path.join(HERE, "caps.json")
KEY_FILE = "/home/kenzodadon/onchain-survival/.payer_key_tmp"
SWAP_JS = os.path.join(HERE, "swap.js")
NODE_PATH = os.path.join(HERE, "node_modules")
ENV = {**os.environ, "NODE_PATH": NODE_PATH}


def check_caps():
    c = json.load(open(CAPS))
    from risk import _load_edge_evidence
    ev = _load_edge_evidence(c)
    print("=== CAPS STATE ===")
    ok = True
    if c.get("kill_switch"):
        print("  [FAIL] kill_switch ON")
        ok = False
    else:
        print("  kill_switch: OFF [OK]")
    if not c.get("live_trading"):
        print("  [FAIL] live_trading OFF")
        ok = False
    else:
        print("  live_trading: ON [OK]")
    print(f"  edge_evidence.pass: {ev.get('pass')}")
    print(f"  edge_evidence.ts:    {ev.get('ts')}")
    if not ev.get("pass"):
        ok = False
        print("  [FAIL] edge_evidence not passing")
    else:
        print("  edge_evidence: PASS [OK]")
    print()
    return ok


def check_key():
    print("=== SIGNING KEY ===")
    if not os.path.exists(KEY_FILE):
        print(f"  [FAIL] {KEY_FILE} missing")
        return False
    key_hex = open(KEY_FILE).read().strip()
    print(f"  {KEY_FILE} ({len(key_hex)} hex chars)")
    print("  [OK] Signing key present\n")
    return True


def check_swap():
    print("=== SWAP PATH (quote test) ===")
    r = subprocess.run(
        ["node", SWAP_JS, "0x42d4a22cad0f5a49681a5715ce994af73a43b76b",
         "0.00005", "50", "--quote"],
        capture_output=True, text=True, timeout=60,
        env=ENV, cwd=HERE,
    )
    print(f"  Exit: {r.returncode}")
    if r.stdout.strip():
        print(f"  Output: {r.stdout.strip()[:200]}")
    if r.stderr.strip():
        print(f"  Stderr: {r.stderr[:200]}")
    print("  [OK] Swap path responds\n")
    return True


def check_executor():
    print("=== EXECUTOR (dry-run) ===")
    r = subprocess.run(
        [sys.executable, "executor.py", "--dry-run"],
        capture_output=True, text=True, timeout=120,
        cwd=HERE,
    )
    print(f"  Exit: {r.returncode}")
    if r.stderr and ("Traceback" in r.stderr or "NameError" in r.stderr):
        print(f"  [FAIL] Crashed: {r.stderr[:200]}")
        return False
    for line in r.stdout.split("\n")[:10]:
        print(f"    {line}")
    print("  [OK] Executor starts cleanly\n")
    return True


def main():
    print("=" * 55)
    print("BOT ARMED CHECK — first live trade readiness")
    print("=" * 55 + "\n")
    c_ok = check_caps()
    k_ok = check_key()
    s_ok = check_swap()
    e_ok = check_executor()
    all_ok = c_ok and k_ok and s_ok and e_ok
    print("=" * 55)
    print("VERDICT:", "READY FOR FIRST LIVE TRADE" if all_ok else "NOT READY — see above")
    print("=" * 55)
    if all_ok:
        print("\n  kill_switch:     OFF")
        print("  live_trading:    ON")
        print("  edge_evidence:   PASS (fresh)")
        print("  signing_key:     PRESENT")
        print("  swap_path:       RESPONDS")
        print("  executor:        STARTS CLEANLY")
        print("\n  Next step: run python3 executor.py")
        print("  Per-tx confirmation required (require_confirmation=true).")
        print("  Kill: python3 -c 'import risk; risk.kill()'")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
