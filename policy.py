"""NemoClaw-style policy enforcement for the Base trend trading bot.

Layers (all must pass):
1. KILL_SWITCH   — immediate halt, no exceptions
2. ARM_PROVENANCE — live_trading ONLY valid with legitimate arm path
3. POLICY_RULES  — declarative constraints from caps.json (immutable at runtime)
4. BUDGET        — daily/weekly spend caps
5. RATE_LIMIT    — max actions per time window
6. ALLOWLIST     — token-level controls
7. AUDIT         — every decision logged with full reasoning
8. GRACEFUL_DEGRADE — stale edge → reduce exposure, not full halt

Fail-closed: any ambiguity blocks the trade.
"""
import json
import os
import hashlib
from datetime import datetime, timezone, timedelta
from typing import Tuple, Dict, Any, Optional, List

HERE = os.path.dirname(os.path.abspath(__file__))
CAPS_FILE = os.path.join(HERE, "caps.json")
STATE_FILE = os.path.join(HERE, "risk_state.json")
POLICY_SNAPSHOT_FILE = os.path.join(HERE, "policy_snapshot.json")
AUDIT_LOG_FILE = os.path.join(HERE, "policy_audit.jsonl")


# === POLICY DEFAULTS (fail-closed) ===
# These are hard minimums — caps.json can only make them STRICTER.
POLICY_DEFAULTS = {
    "max_daily_spend_eth": 0.005,        # hard cap: never spend more than this/day
    "max_weekly_spend_eth": 0.02,        # hard cap: never spend more than this/week
    "max_single_trade_eth": 0.001,       # hard cap: single trade size
    "max_open_positions": 2,             # hard cap: concurrent positions
    "max_buys_per_hour": 3,              # rate limit: buys per rolling hour
    "max_sells_per_hour": 6,             # rate limit: sells per rolling hour (higher for exits)
    "max_slippage_bps": 100,             # hard cap: slippage
    "min_edge_freshness_hours": 24,      # edge evidence must be fresher than this
    "require_edge_for_live": True,       # NEVER go live without fresh edge
    "grace_period_hours": 2,             # after edge expires, reduce size for N hours
    "grace_size_multiplier": 0.5,        # during grace period, halve trade size
    "emergency_stop_daily_loss_eth": 0.003,  # if daily loss hits this, emergency stop
    "allow_stables_as_output": False,    # NEVER buy USDC/DAI/etc with ETH
    "allow_unverified_tokens": False,    # require on-chain verification
    "min_token_liquidity_usd": 50000,    # minimum liquidity to touch a token
    "min_token_vol_24h_usd": 25000,      # minimum 24h volume
    "blocked_token_categories": ["spam", "scam", "honeypot"],  # never touch these
}


def _load_policy(caps: Dict) -> Dict:
    """Merge caps.json policy over defaults (caps can only tighten)."""
    policy = dict(POLICY_DEFAULTS)
    cp = caps.get("policy", {})
    for k, v in cp.items():
        if k in policy:
            # Numeric: take the more conservative (smaller for caps, larger for floors)
            if isinstance(v, (int, float)) and isinstance(policy[k], (int, float)):
                if k.startswith("min_"):
                    policy[k] = max(policy[k], v)  # larger floor = stricter
                else:
                    policy[k] = min(policy[k], v)  # smaller cap = stricter
            else:
                policy[k] = v
        else:
            policy[k] = v  # new policy key from caps.json
    return policy


def _snapshot_policy(policy: Dict) -> str:
    """Hash the active policy for tamper detection."""
    canonical = json.dumps(policy, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def save_policy_snapshot(policy: Dict):
    """Write immutable snapshot for audit."""
    snap = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "policy_hash": _snapshot_policy(policy),
        "policy": policy,
    }
    json.dump(snap, open(POLICY_SNAPSHOT_FILE, "w"), indent=2)


def load_policy_snapshot() -> Optional[Dict]:
    try:
        return json.load(open(POLICY_SNAPSHOT_FILE))
    except Exception:
        return None


def _append_audit(event: str, proposal: Dict, result: str, details: str = ""):
    """Append one audit line to the tamper-evident log."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "proposal_token": proposal.get("token_out", "?"),
        "proposal_amount_eth": proposal.get("amount_eth", 0),
        "proposal_action": proposal.get("action", "?"),
        "result": result,
        "details": details,
    }
    with open(AUDIT_LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


def audit(event: str, proposal: Dict, result: str, details: str = ""):
    """Public audit interface."""
    _append_audit(event, proposal, result, details)


# === LAYER 1: KILL SWITCH ===
def check_kill_switch(caps: Dict) -> Tuple[bool, str]:
    if caps.get("kill_switch") is True:
        return False, "KILL_SWITCH: emergency halt active"
    return True, "ok"


# === LAYER 2: ARM PROVENANCE ===
def check_arm_provenance(caps: Dict) -> Tuple[bool, str]:
    if caps.get("live_trading") is True:
        # Accept EITHER key shape (bare `arm_provenance` or `_arm_provenance`).
        # arm_signal() stamps the underscore-prefixed key; reading only one shape
        # made a legitimately-armed config veto itself. Neither key matching still
        # fails closed.
        prov = caps.get("arm_provenance") or caps.get("_arm_provenance")
        if prov != "arm_signal_v1":
            return False, f"ARM_PROVENANCE: live_trading=true without valid arm (got={prov})"
    return True, "ok"


# === LAYER 3: POLICY RULES (declarative) ===
def check_policy_rules(caps: Dict, proposal: Dict) -> Tuple[bool, str]:
    policy = _load_policy(caps)
    amt = proposal.get("amount_eth", 0)

    # Single trade cap
    if amt > policy["max_single_trade_eth"]:
        return False, f"POLICY: amount {amt:.6f} > max_single_trade {policy['max_single_trade_eth']}"

    # Slippage cap
    slip = proposal.get("slippage_bps", 0)
    if slip > policy["max_slippage_bps"]:
        return False, f"POLICY: slippage {slip} > max {policy['max_slippage_bps']}bps"

    # Stables check
    if not policy["allow_stables_as_output"]:
        stables = ["0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
                   "0x50c5725949a6f0c72e6c4a641f24049a917db0cb",  # DAI
                   "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2",  # USDT
                   "0x2ae3f1ec7f1f5012cfeab0185bfc7aa3cf0dec22",  # cbETH (not stable but staked)
                   ]
        token_out = proposal.get("token_out", "")
        if token_out.lower() in [s.lower() for s in stables]:
            return False, f"POLICY: stables as output blocked ({token_out})"

    # Blocked tokens
    blocked = caps.get("blocked_tokens", [])
    if proposal.get("token_out") in blocked:
        return False, f"POLICY: token {proposal.get('token_out')} is blocked"

    # Token category check (from portfolio_state.json)
    if policy.get("blocked_token_categories"):
        try:
            pf = json.load(open(os.path.join(HERE, "portfolio_state.json")))
            tokens = pf.get("tokens", {})
            token_addr = proposal.get("token_out", "").lower()
            if token_addr in tokens:
                cat = tokens[token_addr].get("classification", "")
                if cat in policy["blocked_token_categories"]:
                    return False, f"POLICY: token category '{cat}' is blocked"
        except Exception:
            pass  # portfolio_state missing → skip category check

    return True, "ok"


# === LAYER 4: BUDGET ===
def check_budget(caps: Dict, proposal: Dict, state: Dict) -> Tuple[bool, str]:
    policy = _load_policy(caps)
    amt = proposal.get("amount_eth", 0)
    now = datetime.now(timezone.utc)

    # Daily budget
    today = now.strftime("%Y-%m-%d")
    daily_spent = state.get("spent_eth", 0.0) if state.get("day") == today else 0.0
    if daily_spent + amt > policy["max_daily_spend_eth"]:
        return False, f"BUDGET: daily spend {daily_spent:.6f} + {amt:.6f} > cap {policy['max_daily_spend_eth']}"

    # Weekly budget (sum last 7 days from audit log)
    weekly_spent = _weekly_spend()
    if weekly_spent + amt > policy["max_weekly_spend_eth"]:
        return False, f"BUDGET: weekly spend {weekly_spent:.6f} + {amt:.6f} > cap {policy['max_weekly_spend_eth']}"

    # Emergency stop on daily loss
    daily_pnl = state.get("pnl_eth", 0.0) if state.get("day") == today else 0.0
    if daily_pnl < 0 and abs(daily_pnl) >= policy["emergency_stop_daily_loss_eth"]:
        return False, f"BUDGET: daily loss {daily_pnl:.6f} >= emergency stop {policy['emergency_stop_daily_loss_eth']}"

    return True, "ok"


def _weekly_spend() -> float:
    """Sum trade amounts from the last 7 days in the audit log."""
    total = 0.0
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    try:
        with open(AUDIT_LOG_FILE) as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    ts = datetime.fromisoformat(entry["ts"].replace("Z", "+00:00"))
                    if ts > cutoff and entry.get("event") == "FILL":
                        total += entry.get("proposal_amount_eth", 0)
                except Exception:
                    continue
    except Exception:
        pass
    return total


# === LAYER 5: RATE LIMIT ===
def check_rate_limit(caps: Dict, proposal: Dict) -> Tuple[bool, str]:
    policy = _load_policy(caps)
    action = proposal.get("action", "buy")
    now = datetime.now(timezone.utc)
    window = timedelta(hours=1)
    cutoff = now - window

    # Count recent actions from audit log
    count = 0
    try:
        with open(AUDIT_LOG_FILE) as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    ts = datetime.fromisoformat(entry["ts"].replace("Z", "+00:00"))
                    if ts > cutoff and entry.get("proposal_action") == action and entry.get("result") == "ALLOW":
                        count += 1
                except Exception:
                    continue
    except Exception:
        pass

    # "swap" is treated as a buy (ETH → token)
    if action in ("buy", "swap") and count >= policy["max_buys_per_hour"]:
        return False, f"RATE: {action} count {count}/{policy['max_buys_per_hour']} per hour"
    if action == "sell" and count >= policy["max_sells_per_hour"]:
        return False, f"RATE: {action} count {count}/{policy['max_sells_per_hour']} per hour"

    return True, "ok"


# === LAYER 6: EDGE FRESHNESS + GRACEFUL DEGRADATION ===
def check_edge_graceful(caps: Dict) -> Tuple[bool, str, float]:
    """Returns (ok, reason, size_multiplier).

    If edge is fresh → multiplier 1.0
    If edge expired but within grace → multiplier 0.5
    If edge expired beyond grace → block
    """
    from risk import _load_edge_evidence
    policy = _load_policy(caps)
    ev = _load_edge_evidence(caps)

    if not policy["require_edge_for_live"]:
        return True, "edge not required", 1.0

    if not isinstance(ev, dict) or not ev.get("ts"):
        return False, "EDGE: no edge_evidence timestamp", 0.0

    try:
        ts = datetime.fromisoformat(ev["ts"].replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except Exception:
        return False, "EDGE: unparseable timestamp", 0.0

    age = datetime.now(timezone.utc) - ts
    max_age = timedelta(hours=policy["min_edge_freshness_hours"])

    if age <= max_age:
        return True, "edge fresh", 1.0

    grace = timedelta(hours=policy.get("grace_period_hours", 0))
    if age <= max_age + grace:
        return True, f"edge stale ({age.total_seconds()/3600:.1f}h) — grace period, size halved", policy.get("grace_size_multiplier", 0.5)

    return False, f"EDGE: expired ({age.total_seconds()/3600:.1f}h > {max_age.total_seconds()/3600:.1f}h + {grace.total_seconds()/3600:.1f}h grace)", 0.0


# === MASTER EVALUATOR ===
def policy_evaluate(caps: Dict, proposal: Dict, state: Dict) -> Tuple[bool, str]:
    """Run all policy layers. All must pass. Returns (allowed, reason)."""

    checks = [
        ("KILL_SWITCH", check_kill_switch, (caps,)),
        ("ARM_PROVENANCE", check_arm_provenance, (caps,)),
        ("POLICY_RULES", check_policy_rules, (caps, proposal)),
        ("BUDGET", check_budget, (caps, proposal, state)),
        ("RATE_LIMIT", check_rate_limit, (caps, proposal)),
    ]

    for name, fn, args in checks:
        ok, reason = fn(*args)
        if not ok:
            audit("POLICY_BLOCK", proposal, "BLOCK", f"{name}: {reason}")
            return False, reason

    # Edge check (graceful degradation)
    edge_ok, edge_reason, size_mult = check_edge_graceful(caps)
    if not edge_ok:
        audit("POLICY_BLOCK", proposal, "BLOCK", f"EDGE: {edge_reason}")
        return False, edge_reason

    # Apply size multiplier from graceful degradation
    if size_mult < 1.0:
        original = proposal.get("amount_eth", 0)
        adjusted = original * size_mult
        proposal["amount_eth"] = adjusted
        audit("POLICY_ADJUST", proposal, "ADJUST", f"size {original:.6f} → {adjusted:.6f} ({edge_reason})")

    # Save policy snapshot for audit
    policy = _load_policy(caps)
    save_policy_snapshot(policy)

    audit("POLICY_PASS", proposal, "ALLOW", edge_reason)
    return True, "all policy layers passed"
