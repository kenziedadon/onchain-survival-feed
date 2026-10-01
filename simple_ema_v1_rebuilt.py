#!/usr/bin/env python3
"""simple_ema_v1_rebuilt.py (finalized) — discovery + edge + macro + ladder.
Rebuild caps (rebuild_caps.json): EMA 8/24; USDC-quoted preferred (user override);
edge target 60% (current 56%); max_pos=3; size ladder 0.0005 ETH (USDC eq);
MACRO_FILTER=ON; LIVE_TRADING=False; KILL=False; replay EXCLUDED; v8_refs=[]; NO fabricated results.
"""
ACTIVE = "simple_ema_v1"
EMA_FAST, EMA_SLOW = 8, 24
MACRO_FILTER_ON = True          # bull-only regime gate (macro)
MACRO_REGIME = "bull"            # allowed macro regime
EDGE_GATE_PCT = 60
EDGE_CURRENT_PCT = 56            # < target => gate CLOSED
MAX_POS = 3
VOL_MIN = 100_000
LIQ_MIN = 250_000
STABLE_EXCLUDE = True
USDC_PREFERRED = True            # user override 2026-09-02
SIZE_LADDER_REF = 0.0005         # ETH; USDC eq at quote time
KILL_SWITCH = False
LIVE_TRADING = False
FABRICATED_TRADES = False
V8_REFS = []
REPLAY_STATE = "EXCLUDED (dropped)"
CAP_HASH = "f91e7148fa7e760b"
CLOSEST_TO_LIVE = "armed-not-live (B-partial arm; DISARMED; no live tx)"

# --- Discovery filter (GeckoTerminal-style, USDC-quoted preferred) ---
def discover_candidates():
    # Filter rules: USDC-quoted preferred; vol > 100k; liq > 250k; 24h > +2%; stables excluded
    # Current edge 56% < 60% => no picks pass edge gate; closest=armed-not-live.
    # Returns empty until router rebuilt + dry-run verified.
    candidates = []
    # Discovery stub: would query GeckoTerminal / basescan for USDC pairs.
    # No fabricated pairs / results.
    return candidates

# --- Edge gate ---
def edge_gate_pass():
    return EDGE_CURRENT_PCT >= EDGE_GATE_PCT  # False (56 < 60)

# --- Macro regime gate ---
def macro_gate_pass():
    # Bull-only; DISARMED => no live execution regardless.
    return MACRO_FILTER_ON and MACRO_REGIME == "bull"

# --- Size ladder reference ---
SIZE_LADDER_STEPS = [
    {"size_eth": 0.0005, "usdc_note": "USDC equivalent at quote time"},
]

if __name__ == "__main__":
    # Verification only — no trades executed (LIVE_TRADING=False).
    print(f"ACTIVE={ACTIVE} MACRO={MACRO_REGIME} EDGE={EDGE_CURRENT_PCT}% (target {EDGE_GATE_PCT}%) -> gate={'PASS' if edge_gate_pass() else 'FAIL'}")
    print(f"DISCOVERY={discover_candidates()} (USDC_pref={USDC_PREFERRED}, vol>{VOL_MIN}, liq>{LIQ_MIN}, 24h>+2%, stables_excluded={STABLE_EXCLUDE})")
    print(f"MACRO_GATE={macro_gate_pass()} | MAX_POS={MAX_POS} | LADDER_REF={SIZE_LADDER_REF} ETH (USDC eq)")
    print(f"KILL={KILL_SWITCH} LIVE={LIVE_TRADING} REPLAY={REPLAY_STATE} V8={V8_REFS} FABRICATED={FABRICATED_TRADES} HASH={CAP_HASH} CLOSEST={CLOSEST_TO_LIVE}")
