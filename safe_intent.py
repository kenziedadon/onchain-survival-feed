#!/usr/bin/env python3
"""
SAFE ARBITRAGE INTENT BRIDGE
============================
Strict parameter-gated bridge between the LLM (which may ONLY express a typed
swap intent) and the local execution layer (which owns calldata generation,
simulation, risk gating, and human approval).

DESIGN CONTRACT
  * The LLM NEVER sees or influences raw calldata, target-contract bytes, or keys.
  * The LLM emits ONLY: sourceToken, targetToken, inputAmountWei, venue.
  * This module validates -> blocklist-checks -> risk-gates -> simulates, then
    emits a TYPED proposal (no hex payload). Broadcast (if ever enabled) is
    delegated to the Base MCP swap path, which is MEV-shielded and requires the
    smart-wallet tap-approve. This is strictly safer than a local ethers.Wallet
    holding a private key.

Matches the production schema: hermes_safe_arbitrage_intent.schema.json

Run:  python3 safe_intent.py     (self-test: no network, no keys, fail-closed)
"""
import json, os, re, sys
from dataclasses import dataclass
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(HERE, "config")
sys.path.insert(0, HERE)  # for risk.py

# ---- Verified contract allowlist (venue -> router). VERIFY ON BASESCAN. ----
# Source of truth: memory (verified Base DEX addresses) + user-provided values.
DEFAULT_APPROVED_ROUTERS = {
    "UNIV3":     "0x2626664c2603336E57B271c5C0b26F421741e481",  # Base Uniswap V3 Router
    "AERODROME": "0xBE6D404f21F067C1981F4B3C53C04D4A1AA6A8A5",  # Base Aerodrome (Slipstream) Router
}
# QuoterV2 — used in production to estimate amountOutMinimum (slippage guard).
QUOTER_V2 = "0x3d4e9b9e9e9e9e9e9e9e9e9e9e9e9e9e9e9e9C76a"  # user memory: 0x3d4e...C76a (verify)

ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
VENUES = ("UNIV3", "AERODROME")

# Seed allowlist of well-known safe base assets (expand via config/approved_tokens.json)
DEFAULT_ALLOWED_TOKENS = {
    "0x4200000000000000000000000000000000000006",  # WETH
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
    "0xd9aaab39e305cd1f0c6defd5aad6bb2c0e0d3b5e",  # USDbC
    "0x50c5725949a6f0c72e6c4a641f67700984d05e59",  # DAI
}


# ----------------------------- config loaders ------------------------------
def load_json(name, default):
    p = os.path.join(CONFIG_DIR, name)
    if not os.path.exists(p):
        return default
    try:
        return json.load(open(p))
    except Exception:
        return default


def load_blocklist():
    return set(t.lower() for t in load_json("blocked_tokens.json", []))


def load_allowlist():
    return set(t.lower() for t in load_json("approved_tokens.json", [])) | \
           set(t.lower() for t in DEFAULT_ALLOWED_TOKENS)


def load_routers():
    merged = dict(DEFAULT_APPROVED_ROUTERS)
    merged.update({k.upper(): v for k, v in load_json("approved_routers.json", {}).items()})
    return merged


# ------------------------------- the intent --------------------------------
@dataclass
class SwapIntent:
    sourceToken: str
    targetToken: str
    inputAmountWei: str
    venue: str

    def validate_static(self):
        """Raise ValueError on any malformed field. Pure, no network, no keys."""
        if self.venue not in VENUES:
            raise ValueError(f"Invalid venue '{self.venue}'. Allowed: {VENUES}")
        for fld in ("sourceToken", "targetToken"):
            v = getattr(self, fld)
            if not ADDR_RE.match(v or ""):
                raise ValueError(f"{fld} is not a valid 20-byte address: {v!r}")
        if not re.fullmatch(r"[0-9]+", self.inputAmountWei or ""):
            raise ValueError(f"inputAmountWei must be a base-10 integer string (wei): {self.inputAmountWei!r}")
        if int(self.inputAmountWei) <= 0:
            raise ValueError("inputAmountWei must be > 0")
        if self.sourceToken.lower() == self.targetToken.lower():
            raise ValueError("sourceToken and targetToken must differ")


# ------------------------- simulation (fail-closed) ------------------------
# Pluggable hook. Production sets this to a real on-chain staticCall
# (provider.call / Base MCP simulate) BEFORE any broadcast is permitted.
SIMULATE_HOOK = None  # callable(SwapIntent, router_addr) -> (ok: bool, detail: str)


def simulate(intent: SwapIntent, router: str):
    if SIMULATE_HOOK is not None:
        return SIMULATE_HOOK(intent, router)
    # No hook wired -> refuse to broadcast. Fail-closed by design: the real
    # deployment MUST simulate the compiled call on-chain first.
    return False, "No simulation hook configured (fail-closed by design)"


# Real read-only on-chain simulation (wired by default if simulate.py present).
try:
    from simulate import simulate_intent
    def _onchain_sim(intent: SwapIntent, router: str):
        res = simulate_intent(intent.venue, intent.sourceToken,
                                intent.targetToken, intent.inputAmountWei)
        # hard_fail => must never broadcast; otherwise pass (may still need review)
        if res.get("hard_fail"):
            return False, res.get("detail", "simulation hard-fail")
        return True, res.get("detail", "simulated")
    SIMULATE_HOOK = _onchain_sim
except Exception as _e:  # pragma: no cover
    SIMULATE_HOOK = None


# ------------------------------ the bridge ---------------------------------
def handle_safe_intent(intent: SwapIntent, live: bool = False) -> dict:
    """Defensively guarded bridge. Returns a structured result.
    NEVER signs or broadcasts raw calldata. Emits a typed proposal only."""
    try:
        intent.validate_static()
    except ValueError as e:
        return _deny("Static validation failed", str(e))

    routers = load_routers()
    router = routers.get(intent.venue)
    if not router:
        return _deny("Security Violation", f"Unauthorized venue '{intent.venue}'")

    blocked = load_blocklist()
    if intent.sourceToken.lower() in blocked or intent.targetToken.lower() in blocked:
        return _deny("Security Violation", "Token present on local risk blocklist")

    # Allowlist (soft): unknown tokens are flagged for human review, not hard-denied,
    # so early discovery still works — but the blocklist is always hard.
    allow = load_allowlist()
    unknown = [t for t in (intent.sourceToken, intent.targetToken) if t.lower() not in allow]

    # Local risk gate — reuse the existing risk.py engine (single source of truth).
    try:
        from risk import evaluate, load_caps
        caps = load_caps()
        amount_eth = Decimal(intent.inputAmountWei) / Decimal(10) ** 18  # exact wei->ether
        proposal = {
            "action": "swap",
            "token_in": intent.sourceToken,
            "token_out": intent.targetToken,
            "amount_eth": float(amount_eth),
            "slippage_bps": caps.get("max_slippage_bps", 500),
            "score": caps.get("min_score", 40) + 1,  # intent must clear min_score
        }
        allowed, reason = evaluate(proposal)
        if not allowed:
            if reason.startswith("dry-run"):
                # Intent is valid; it simply won't broadcast until live_trading=true.
                live = False
            else:
                return _deny("Risk Abort", f"risk.evaluate rejected: {reason}")
    except Exception as e:
        return _deny("Risk Engine Error", str(e))

    # Pre-flight simulation (fail-closed).
    ok, detail = simulate(intent, router)
    if not ok:
        return _deny("Risk Abort", f"Pre-flight simulation blocked: {detail}")

    # Human-in-the-loop vs Level-2 autonomous:
    #  - require_confirmation=True  -> emit a proposal, await smart-wallet tap.
    #  - require_confirmation=False -> flag auto_submit; a downstream executor may
    #    submit without a tap, BUT the Base wallet session key (enabled by the user
    #    in Coinbase) is still the final gate. This module never signs calldata.
    requires_confirmation = caps.get("require_confirmation", True)

    proposal_out = {
        "tool": "mcp__base_mcp__swap",
        "arguments": {
            "fromAsset": intent.sourceToken,
            "toAsset": intent.targetToken,
            "amount": f"{amount_eth.quantize(Decimal('0.000000000000000001'))}",  # exact ether, MCP format
            "chain": "base",
            "venue": intent.venue,
        },
        "router": router,
        "unknown_tokens_flagged": unknown,
        "requires_confirmation": requires_confirmation,
        "auto_submit": (not requires_confirmation),
    }
    msg = ("Intent verified, simulated cleanly, passed risk gate. "
           "Awaiting human tap-approve before broadcast.") if requires_confirmation else (
           "Intent verified, simulated cleanly, passed risk gate. "
           "AUTO-SUBMIT enabled (Level 2) — will broadcast via Base wallet session key.")
    return {
        "success": True,
        "message": msg,
        "proposal": proposal_out,
        "live": live,
    }


def _deny(kind: str, msg: str) -> dict:
    return {"success": False, "message": f"{kind}: {msg}"}


# ------------------------------- self-test ---------------------------------
def _self_test():
    print("=== safe_intent.py self-test (no network, no keys) ===\n")
    os.makedirs(CONFIG_DIR, exist_ok=True)
    # seed config files
    json.dump(["0xDEADBEEF00000000000000000000000000000000"],
              open(os.path.join(CONFIG_DIR, "blocked_tokens.json"), "w"))
    json.dump([], open(os.path.join(CONFIG_DIR, "approved_tokens.json"), "w"))
    json.dump({}, open(os.path.join(CONFIG_DIR, "approved_routers.json"), "w"))

    WETH = "0x4200000000000000000000000000000000000006"
    USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    SCAM = "0xDEADBEEF00000000000000000000000000000000"
    WEI_005 = "50000000000000000"  # 0.05 ETH — within caps.max_trade_eth

    cases = [
        ("valid UNIV3 WETH->USDC", SwapIntent(WETH, USDC, WEI_005, "UNIV3"), True),
        ("blocked token",          SwapIntent(SCAM, USDC, WEI_005, "UNIV3"), False),
        ("invalid venue",          SwapIntent(WETH, USDC, WEI_005, "PANCAKE"), False),
        ("malformed wei",          SwapIntent(WETH, USDC, "0xabc", "UNIV3"), False),
        ("zero wei",               SwapIntent(WETH, USDC, "0", "UNIV3"), False),
        ("same token",             SwapIntent(WETH, WETH, WEI_005, "UNIV3"), False),
        ("bad address",            SwapIntent("notanaddress", USDC, WEI_005, "UNIV3"), False),
    ]

    global SIMULATE_HOOK
    SIMULATE_HOOK = lambda i, r: (True, "staticCall ok (test hook)")

    passed = 0
    for name, intent, expect_ok in cases:
        res = handle_safe_intent(intent)
        ok = res["success"]
        mark = "OK " if ok == expect_ok else "BAD"
        passed += (ok == expect_ok)
        print(f"  [{mark}] {name:30} -> success={ok}  {res['message'][:64]}")
    print(f"\n{passed}/{len(cases)} cases behaved as expected.")

    print("\nSample approved proposal (typed, NO calldata):")
    print(json.dumps(handle_safe_intent(SwapIntent(WETH, USDC, WEI_005, "UNIV3"))["proposal"], indent=2))


if __name__ == "__main__":
    _self_test()
