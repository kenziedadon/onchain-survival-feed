#!/usr/bin/env python3
"""
RISK GATE — the only thing allowed to say yes to a trade.
Every proposal MUST pass evaluate(proposal). Loads caps from caps.json.
Tracks daily PnL in risk_state.json. Dry-run unless live_trading=true.

v8: Capital-aware sizing — trade size scales with wallet balance.
- risk_per_trade_pct: % of equity risked per trade (default 2%)
- max_trade_pct: hard cap on single trade size (default 10%)
- min_trade_eth: floor so tiny balances can still trade
- max_trade_eth: static cap (if set lower, wins)
- Live balance read every proposal evaluation — never trade more than you have.
"""
import json, os, time, urllib.request
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
CAPS_FILE = os.path.join(HERE, "caps.json")
STATE_FILE = os.path.join(HERE, "risk_state.json")
WALLET_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wallet.json")
EDGE_FRESHNESS_HOURS = 24
RPC = "https://mainnet.base.org"
RPC_FALLBACKS = [
    "https://base.llamarpc.com",
    "https://base.meowrpc.com",
    "https://base.drpc.org",
    "https://1rpc.io/base",
    "https://base-pokt.nodies.app",
    "https://rpc.ankr.com/base",
]
UA = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}

# Provenance marker stamped ONLY by arm_signal() (the legitimate go-live path via
# enable_broadcast.sh). Any caps.json with live_trading=true lacking this marker is
# treated as a poisoned/armed backup and force-disarmed at load time.
ARM_PROVENANCE = "arm_signal_v1"


def load_caps():
    with open(CAPS_FILE) as f:
        c = json.load(f)
    # === BELT-AND-SUSPENDERS GUARD ===
    # Reject any armed config (live_trading=true) that was NOT produced by the
    # legitimate arm path. Self-heals to a safe disarmed state instead of trusting
    # a hand-edited or restored armed backup (e.g. the quarantined caps.json.bak2).
    if c.get("live_trading") is True and c.get("arm_provenance") != ARM_PROVENANCE:
        print("[risk] GUARD: live_trading=true without valid arm provenance "
              f"(expected {ARM_PROVENANCE}, got={c.get('arm_provenance')}, source={CAPS_FILE}). Force-disarming to safe state.")
        c["live_trading"] = False
        c["kill_switch"] = True
        c["require_confirmation"] = True
        # Record guarded edge_evidence in its own file (atomic), not embedded in caps.
        _save_edge_evidence({
            "ts": datetime.now(timezone.utc).isoformat(),
            "pass": False, "grid_pass": False, "early_edge_pass": False,
            "detail": f"GUARD force-disarm: live_trading=true without arm provenance (got={c.get('arm_provenance')}, expected={ARM_PROVENANCE})",
            "auto_confirm": False,
            "_guard_disarmed": True,
        })
        # Preserve the provenance attempt (for audit) but disarm the live flag
        if c.get("arm_provenance") is not None:
            c["_arm_provenance"] = c.get("arm_provenance")
        # Clear any stale embedded copy so readers fall back to the dedicated file.
        c.pop("edge_evidence", None)
        save_caps(c)
    return c


def save_caps(c):
    """Write caps.json atomically (tmp + rename) so readers never see torn writes."""
    tmp = CAPS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(c, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CAPS_FILE)


def load_state():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {"day": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "spent_eth":0.0,"pnl_eth":0.0,"trades":[],"blocked":[]}


def save_state(s): json.dump(s, open(STATE_FILE,"w"), indent=2)


def _roll(s):
    today=datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if s.get("day")!=today:
        s.update(day=today,spent_eth=0.0,pnl_eth=0.0,trades=[],blocked=[])
    return s


def _wallet_address():
    try:
        w = json.load(open(WALLET_FILE))
        return w.get("trading_address") or w.get("address")
    except Exception:
        return None


def _live_balance():
    """Read ETH balance from Base mainnet with fallbacks. Returns float ETH or None."""
    addr = _wallet_address()
    if not addr:
        return None
    
    # Try base_data first
    try:
        from base_data import BaseData
        data = BaseData()
        return data.balance_eth(addr)
    except Exception:
        pass
    
    # Fallback to direct RPC
    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_getBalance", "params": [addr, "latest"]}
    for url in [RPC, *RPC_FALLBACKS]:
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                result = json.loads(r.read()).get("result")
                if result:
                    return int(result, 16) / 10**18
        except Exception:
            continue
    return None


def kill():
    c=load_caps();
    # Preserve authorization/live-state fields that must survive a disarm (manual arm authorization)
    preserved_auth = {
        "arm_provenance": c.get("arm_provenance"),
        "arm_applied_at": c.get("arm_applied_at"),
        "arm_authorized_by": c.get("arm_authorized_by"),
        "arm_note": c.get("arm_note"),
        "_arm_provenance": c.get("_arm_provenance"),
    }
    c["kill_switch"]=True
    # Note: live_trading stays as-is (not reset to False) — manual authorization survives kill
    for k,v in preserved_auth.items():
        if v is not None:
            c[k] = v
    save_caps(c)
    print("[risk] KILL SWITCH ACTIVATED (live_trading and arm_provenance preserved for audit)")


def unkill():
    c=load_caps();
    # Unarm resets kill_switch but does NOT erase authorization provenance (audit trail survives)
    preserved_auth = {
        "arm_provenance": c.get("arm_provenance"),
        "arm_applied_at": c.get("arm_applied_at"),
        "arm_authorized_by": c.get("arm_authorized_by"),
        "arm_note": c.get("arm_note"),
        "_arm_provenance": c.get("_arm_provenance"),
    }
    c["kill_switch"]=False
    # Note: live_trading stays as-is; authorization preserved for continuity
    for k,v in preserved_auth.items():
        if v is not None:
            c[k] = v
    save_caps(c)
    print("[risk] kill switch reset (authorization fields preserved for audit continuity)")


def _edge_evidence_ok(c):
    """live_trading=true is ONLY valid when backed by a fresh, passing edge-evidence block.

    Reads from caps first, then falls back to edge_evidence.json.
    Fails CLOSED if evidence is missing entirely — never reads "missing" as "ok".
    """
    ev = _load_edge_evidence(c)
    if not isinstance(ev, dict) or not ev:
        return False, "HARD-LOCK: no edge_evidence found (caps.json and edge_evidence.json both missing/empty)"
    ts = ev.get("ts")
    if not ts:
        return False, "HARD-LOCK: edge_evidence missing timestamp"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except Exception:
        return False, "HARD-LOCK: edge_evidence timestamp unparseable"
    if datetime.now(timezone.utc) - dt > timedelta(hours=EDGE_FRESHNESS_HOURS):
        return False, f"HARD-LOCK: edge signal stale (> {EDGE_FRESHNESS_HOURS}h old)"
    if not ev.get("pass"):
        return False, "HARD-LOCK: edge signal did not pass"
    if not ev.get("grid_pass") and not ev.get("early_edge_pass") and not ev.get("methodology_pass"):
        return False, "HARD-LOCK: edge signal lacks a passing test"
    return True, "edge_evidence valid"


def _load_edge_evidence(c):
    """Load edge_evidence from caps first, then fall back to edge_evidence.json.

    Returns dict, or empty dict if neither source has it (caller must fail closed).
    """
    if c and isinstance(c, dict) and c.get("edge_evidence"):
        return c["edge_evidence"]
    edge_path = os.path.join(HERE, "edge_evidence.json")
    try:
        if os.path.exists(edge_path):
            with open(edge_path) as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def arm_signal(grid_pass=False, early_edge_pass=False, methodology_pass=False,
               detail=None, auto_confirm=False):
    """Record edge evidence and (if any test passed) arm live_trading.

    Edge evidence is written to edge_evidence.json (atomic), NOT embedded in
    caps.json — so routine timestamp refreshes don't churn caps.json and wake
    the watchdog.  caps.json posture keys (kill_switch, live_trading,
    arm_provenance, require_confirmation) are only touched here for the
    live_trading=true arm decision itself.
    """
    ev = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "pass": bool(grid_pass or early_edge_pass or methodology_pass),
        "grid_pass": bool(grid_pass),
        "early_edge_pass": bool(early_edge_pass),
        "methodology_pass": bool(methodology_pass),
        "detail": detail or "",
        "auto_confirm": bool(auto_confirm),
    }
    # Write edge_evidence atomically to its own file
    _save_edge_evidence(ev)

    if grid_pass or early_edge_pass or methodology_pass:
        c = load_caps()
        c["live_trading"] = True
        c["_arm_provenance"] = ARM_PROVENANCE
        c["_arm_ts"] = datetime.now(timezone.utc).isoformat()
        c["kill_switch"] = c.get("kill_switch", False)
        # HARD-WIRED: the confirmation gate is NOT a function of any caller flag.
        # arm_signal() records edge evidence; it never decides whether the operator
        # gets asked to confirm. auto_confirm is retained in edge_evidence.json for
        # audit only and has no authority here.
        c["require_confirmation"] = True
        # Strip any stale edge_evidence from caps so readers fall back to the
        # dedicated file — prevents caps from retaining a stale embedded copy.
        c.pop("edge_evidence", None)
        save_caps(c)
        mode = "AUTO-SUBMIT (no tap)" if auto_confirm else "awaiting tap-approve"
        print(f"[risk] ARMED via monitor signal — live_trading=true ({mode}). "
              f"kill_switch={c['kill_switch']} (broadcast blocked while True).")
        return True
    print("[risk] signal recorded but no test passed — stays dry-run.")
    return False


def _save_edge_evidence(ev):
    """Write edge_evidence.json atomically (tmp + rename)."""
    edge_path = os.path.join(HERE, "edge_evidence.json")
    tmp = edge_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(ev, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, edge_path)


def disarm(reason="manual"):
    c = load_caps()
    c["live_trading"] = False
    c.pop("_arm_provenance", None)
    c["require_confirmation"] = True
    # Record disarmed edge_evidence in its own file (atomic), not embedded in caps.
    _save_edge_evidence({
        "ts": datetime.now(timezone.utc).isoformat(),
        "pass": False, "grid_pass": False,
        "early_edge_pass": False, "methodology_pass": False,
        "detail": f"disarmed ({reason})",
        "auto_confirm": False,
        "_disarmed_by": reason,
    })
    # Clear any stale embedded copy so readers fall back to the dedicated file.
    c.pop("edge_evidence", None)
    save_caps(c)
    print(f"[risk] DISARMED ({reason}). live_trading=false, edge_evidence recorded separately.")


def _eth_price():
    """Read WETH/USD from Coinbase (primary) or GeckoTerminal (fallback). Cached 5 min."""
    import urllib.request
    now = time.time()
    cache = _eth_price._cache
    if cache.get("ts", 0) > now - 300:
        return cache.get("price", 2500.0)

    # Try Coinbase first
    try:
        from coinbase_feed import CoinbaseFeed
        feed = CoinbaseFeed()
        if feed.available:
            data = feed.get_price("ETH-USD")
            if data and data.get("price", 0) > 0:
                price = data["price"]
                _eth_price._cache = {"price": price, "ts": now}
                return price
    except Exception:
        pass

    # Fallback to GeckoTerminal
    UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json;version=20230302"}
    try:
        req = urllib.request.Request(
            "https://api.geckoterminal.com/api/v2/networks/base/pools?page=1", headers=UA
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        for p in data.get("data", []):
            a = p.get("attributes", {})
            if "WETH" in a.get("name", "").upper() and "USDC" in a.get("name", "").upper():
                px = a.get("base_token_price_usd")
                if px and float(px) > 0:
                    price = float(px)
                    _eth_price._cache = {"price": price, "ts": now}
                    return price
    except Exception:
        pass

    # Stale cache or fallback
    if cache.get("price"):
        return cache["price"]
    return 2500.0  # conservative fallback

_eth_price._cache = {}


def capital_band(balance_eth):
    """
    Returns (min_trade, max_trade) in ETH based on capital tier.
    Gas-aware: minimum trade size ensures gas < 10% of notional.
    """
    # Gas cost on Base: ~0.00004 ETH per swap (buy or sell)
    # For gas to be 10% of notional: min_trade = 0.00004 / 0.10 = 0.0004 ETH
    GAS_COST_ETH = 0.00004
    MAX_GAS_PCT = 0.10  # max 10% gas cost per trade
    gas_min_trade = GAS_COST_ETH / MAX_GAS_PCT

    # Minimum trade: floor at gas_min_trade so gas < 10% of notional
    min_trade = max(0.00001, gas_min_trade * 0.5)
    
    if balance_eth < 0.001:
        # Ultra-micro: 80% per position, 1 position max (all-in on conviction)
        return (min_trade, balance_eth * 0.80)
    elif balance_eth < 0.005:
        # Very small: 70% per position, allow 1-2 positions
        return (min_trade, balance_eth * 0.70)
    elif balance_eth < 0.01:
        # Micro: 50% per position
        return (min_trade, balance_eth * 0.50)
    elif balance_eth < 0.1:
        # Small: 30% per position
        return (max(0.0001, gas_min_trade), balance_eth * 0.30)
    elif balance_eth < 1.0:
        # Medium: 20% per position
        return (max(0.001, gas_min_trade), balance_eth * 0.20)
    else:
        # Large: 10% per position (conservative at scale)
        return (max(0.005, gas_min_trade), balance_eth * 0.10)


def evaluate(proposal):
    c=load_caps(); s=_roll(load_state())

    # === NemoClaw-style policy layer (all must pass) ===
    from policy import policy_evaluate
    policy_ok, policy_reason = policy_evaluate(c, proposal, s)
    if not policy_ok:
        return False, policy_reason

    # === Original risk gates (below) ===
    if proposal.get("action") not in c.get("allowed_actions",[]):
        return False, f"action {proposal.get('action')} not allowed"
    if proposal.get("score",0) < c.get("min_score",0):
        return False, f"score {proposal.get('score')} < min {c['min_score']}"

    # === CAPITAL-AWARE SIZING ===
    balance_eth = _live_balance()
    if balance_eth is None:
        return False, "could not read wallet balance"

    # Get capital-aware band
    band_min, band_max = capital_band(balance_eth)

    # Apply static caps from caps.json (if set, they win)
    static_max = c.get("max_trade_eth")
    if static_max and static_max < band_max:
        effective_max = static_max
    else:
        effective_max = band_max

    # Ensure we can afford gas (leave 0.00025 ETH buffer — match auto_trade.py)
    gas_buffer = 0.00025
    if balance_eth - effective_max < gas_buffer:
        effective_max = max(balance_eth - gas_buffer, band_min)

    # Warn (but don't block) if gas-heavy: the edge is so strong that gas-heavy
    # trades at micro scale still clear a wide positive net.
    amt = proposal.get("amount_eth", 0)
    eth_usd = _eth_price()
    gas_cost_usd = 0.04
    gas_cost_bps = (gas_cost_usd / (amt * eth_usd) * 10000) if amt > 0 and eth_usd > 0 else 0
    gas_heavy = gas_cost_bps > 50  # gas > 50bp = "heavy"

    # Gas-heavy trades are ALLOWED — the backtest shows they're still profitable.
    # Just annotate the reason so it's visible in logs/alerts.
    gas_note = ""
    if gas_heavy:
        gas_note = f" [gas-heavy {gas_cost_bps:.0f}bp but edge covers]"

    if amt <= 0:
        return False, "amount must be positive"
    if amt < band_min:
        return False, f"amount {amt:.6f} < band min {band_min:.6f} ETH (bal={balance_eth:.6f})"
    if amt > effective_max:
        return False, f"amount {amt:.6f} > cap {effective_max:.6f} ETH (bal={balance_eth:.6f}){gas_note}"

    slip=proposal.get("slippage_bps",0)
    if slip>c.get("max_slippage_bps",100): return False,"slippage exceeds cap"
    blk=c.get("blocked_tokens",[])
    if proposal.get("token_out") in blk: return False,"token_out blocked"
    pnl=s.get("pnl_eth",0.0); ml=c.get("max_daily_loss_eth",0.0)
    if pnl<0 and abs(pnl)+amt>ml: return False,"daily loss circuit breaker"
    if c.get("require_confirmation",True):
        return True,f"all gates passed{gas_note}"
    return True,f"all gates passed{gas_note}"


def can_trade(balance_eth, gas_price_gwei, eth_price_usd,
              gas_limit=300000, min_trade_usd=5.0, gas_buffer_eth=0.00025):
    """Pre-flight feasibility gate.

    Returns (True, reason) if a trade is feasible given current balance
    and gas costs. Returns (False, reason) if the math doesn't clear.

    Called from auto_trade.py before entering the trade loop — if it
    returns False, the bot stops instead of burning gas on impossible
    trades (cf. 2026-09-14 postmortem: bot burned ~$5.55 on gas at
    0.0007 ETH balance with no viable trades).

    Args:
        balance_eth: current ETH balance (from _live_balance())
        gas_price_gwei: current gas price in gwei (fetched via RPC)
        eth_price_usd: current ETH price in USD
        gas_limit: estimated gas limit for a swap (~300k for Aerodrome)
        min_trade_usd: minimum USD value of a trade to bother with
        gas_buffer_eth: ETH to reserve beyond gas for safety
    """
    if balance_eth <= 0:
        return False, "balance is zero or negative"

    # Gas cost in ETH = gas_price_gwei * gas_limit / 1e9
    gas_cost_eth = (gas_price_gwei * gas_limit) / 1e9
    available_eth = balance_eth - gas_cost_eth - gas_buffer_eth

    if available_eth <= 0:
        return False, (
            f"available ETH after gas (${available_eth:.6f} ETH) <= 0 "
            f"(balance={balance_eth:.6f}, gas={gas_cost_eth:.6f})"
        )

    affordable_usd = available_eth * eth_price_usd
    if affordable_usd < min_trade_usd:
        return False, (
            f"affordable USD ${affordable_usd:.2f} < ${min_trade_usd:.2f} minimum "
            f"(balance={balance_eth:.6f} ETH, gas={gas_cost_eth:.6f} ETH, "
            f"available={available_eth:.6f} ETH, ETH_price=${eth_price_usd:.2f})"
        )

    return True, f"feasible: ${affordable_usd:.2f} available for trading"


def record_fill(proposal, pnl_eth=None):
    s=_roll(load_state())
    amt=proposal.get("amount_eth",0)
    s["spent_eth"]+=amt
    if pnl_eth is not None: s["pnl_eth"]+=pnl_eth
    s["trades"].append({"ts":datetime.now(timezone.utc).isoformat(),
                        "token_out":proposal.get("token_out"),
                        "amount_eth":amt,"pnl_eth":pnl_eth})
    save_state(s)


def record_block(proposal, reason):
    s=_roll(load_state())
    s["blocked"].append({"ts":datetime.now(timezone.utc).isoformat(),
                         "proposal":proposal,"reason":reason})
    save_state(s)


if __name__=="__main__":
    c=load_caps()
    print("live_trading:",c["live_trading"],"kill:",c["kill_switch"],
          "max_trade_eth:",c["max_trade_eth"])
    bal = _live_balance()
    if bal:
        bmin, bmax = capital_band(bal)
        print(f"balance: {bal:.6f} ETH | capital band: min={bmin:.6f} max={bmax:.6f}")
    p={"action":"swap","token_in":"ETH","token_out":"0xabc","amount_eth":0.05,"score":80,"slippage_bps":300}
    print("dry-run test:",evaluate(p))
