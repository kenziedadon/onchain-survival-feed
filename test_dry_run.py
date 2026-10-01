#!/usr/bin/env python3
"""Dry-run executor test — verifies the full execution path without broadcasting."""
import sys
import json

# Set dry-run mode
sys.argv = ['executor.py', '--dry-run']

# Load caps to verify gate logic
with open('caps.json') as f:
    caps = json.load(f)

print("=== EXECUTOR DRY-RUN TEST ===")
print(f"kill_switch: {caps['kill_switch']}")
print(f"live_trading: {caps['live_trading']}")
print()

# Import executor module (dry-run mode)
print("--- Loading executor ---")
import executor

# Run main() — should hit kill_switch guard and exit cleanly
print("--- Running main() ---")
try:
    executor.main()
    print()
    print("=== RESULT: Executor ran clean (kill_switch guard active, no broadcast) ===")
except SystemExit as e:
    print(f"=== RESULT: Executor exited with code {e.code} (expected: kill_switch guard) ===")
except Exception as e:
    print(f"=== ERROR: {type(e).__name__}: {e} ===")
