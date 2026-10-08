#!/usr/bin/env python3
"""Daily edge_evidence re-arm — keeps live_trading valid past the 24h freshness window.

Runs once per day via cron. Re-arms with the same passing evidence that was
originally used (grid_pass + early_edge_pass + methodology_pass).

WARNING: this DOES touch trading posture. arm_signal() sets live_trading=true,
stamps _arm_ts, and writes the arm provenance marker. It no longer lowers
require_confirmation (risk.py hard-wires that to true) — but it can still flip
live_trading. Do not schedule this without an explicit operator decision.
"""
import sys, json, os
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from risk import arm_signal, load_caps, _load_edge_evidence

ev = _load_edge_evidence(load_caps())
if not ev.get("pass"):
    print("[rearm] SKIP: edge_evidence does not pass — nothing to refresh")
    sys.exit(0)

arm_signal(
    grid_pass=ev.get("grid_pass", False),
    early_edge_pass=ev.get("early_edge_pass", False),
    methodology_pass=ev.get("methodology_pass", False),
    detail=ev.get("detail", ""),
    auto_confirm=False,  # audit-only; no longer lowers the confirmation gate
)
print(f"[rearm] Edge evidence refreshed at {ev.get('ts','?')} → now")
print(f"  pass={ev.get('pass')} grid={ev.get('grid_pass')} early={ev.get('early_edge_pass')} method={ev.get('methodology_pass')}")
