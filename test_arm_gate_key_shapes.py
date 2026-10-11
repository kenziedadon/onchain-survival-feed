#!/usr/bin/env python3
"""
Regression test for the arm-gate key-shape fix (2026-10-11).

Defect: arm_signal() writes `_arm_provenance` (underscore-prefixed), but both
arm gates read only `arm_provenance` (bare). A legitimately-armed config
therefore force-disarmed itself on the next load — a silent veto of the
legitimate go-live path.

Fix: both gates accept EITHER key shape; a config matching NEITHER still fails
closed. This test pins that behavior against the REAL functions (no logic
replication) and the ORIGINAL caps input (each gate gets its real input, not the
post-transform output of the other).

Covers BOTH gates:
  - risk.load_caps()        (self-healing guard, mutates + writes)
  - policy.check_arm_provenance()  (read-only Layer 2 check)

Run: python3 test_arm_gate_key_shapes.py
Exit 0 = all pass.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

for _m in ("risk", "policy"):
    if _m in sys.modules:
        del sys.modules[_m]
import risk  # noqa: E402
import policy  # noqa: E402

ARM = "arm_signal_v1"

# (label, caps, expected risk verdict, expected policy verdict)
#   risk verdict:   "KEEP" = stays armed; "DISARM" = force-disarmed
#   policy verdict: "allow" = passes Layer 2; "block" = rejected
CASES = [
    ("legit arm, ONLY _arm_provenance (arm_signal shape)",
     {"live_trading": True, "_arm_provenance": ARM}, "KEEP", "allow"),
    ("legit arm, ONLY arm_provenance (restored shape)",
     {"live_trading": True, "arm_provenance": ARM}, "KEEP", "allow"),
    ("legit arm, BOTH keys",
     {"live_trading": True, "arm_provenance": ARM, "_arm_provenance": ARM},
     "KEEP", "allow"),
    ("POISON: live_trading=true, NO provenance (must fail closed)",
     {"live_trading": True}, "DISARM", "block"),
    ("POISON: live_trading=true, WRONG provenance (must fail closed)",
     {"live_trading": True, "arm_provenance": "hand-edited"}, "DISARM", "block"),
    ("disarmed: live_trading=false, no provenance (must pass)",
     {"live_trading": False}, "KEEP", "allow"),
]


def real_load_caps(caps_dict: dict, tmpdir: str) -> dict:
    """Call the REAL risk.load_caps() against a temp caps file."""
    tcap = os.path.join(tmpdir, "caps.json")
    tedge = os.path.join(tmpdir, "edge_evidence.json")
    real = risk.CAPS_FILE
    risk.CAPS_FILE = tcap
    risk._save_edge_evidence = lambda ev: json.dump(ev, open(tedge, "w"))
    try:
        with open(tcap, "w") as f:
            json.dump(caps_dict, f)
        return risk.load_caps()
    finally:
        risk.CAPS_FILE = real


def main() -> int:
    tmpdir = tempfile.mkdtemp()
    allpass = True
    print("=== arm-gate key-shape regression (real functions, original inputs) ===\n")
    for label, caps, exp_risk, exp_pol in CASES:
        out = real_load_caps(caps, tmpdir)
        risk_verdict = ("DISARM"
                        if (caps["live_trading"] and not out.get("live_trading"))
                        else "KEEP")
        p_ok, _ = policy.check_arm_provenance(caps)  # read-only, original input
        pol_verdict = "allow" if p_ok else "block"

        r_ok = risk_verdict == exp_risk
        p_ok_v = pol_verdict == exp_pol
        status = "PASS" if (r_ok and p_ok_v) else "FAIL"
        if not (r_ok and p_ok_v):
            allpass = False
        print(f"[{status}] {label}")
        print(f"        risk={risk_verdict}(want {exp_risk}) "
              f"live={out.get('live_trading')} kill={out.get('kill_switch')} | "
              f"policy={pol_verdict}(want {exp_pol})")

    print("\nRESULT:",
          "ALL PASS — self-veto fixed, fail-closed preserved"
          if allpass else "*** FAILURE ***")
    return 0 if allpass else 1


if __name__ == "__main__":
    sys.exit(main())
