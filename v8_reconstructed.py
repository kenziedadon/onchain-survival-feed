# RECONSTRUCTED v8 (A — workspace artifacts ONLY; NOT real E-350 v8).
# Replay unrecovered (dropped); execution broken (swap checksum); edge 56% false.
import json
caps = json.load(open('caps.json'))
from risk import _load_edge_evidence
print("ARM:", caps.get('live_trading'), "kill:", caps.get('kill_switch'),
      "arm:", caps.get('arm_provenance'), "edge_pass:", _load_edge_evidence(caps).get('pass'))
print("RECONSTRUCTED v8 (fixture-based). NO live tx. Soak: 2/5 done. Blockers documented.")
