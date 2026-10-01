#!/usr/bin/env python3
"""capital_manager.py — responsible position sizing with auto-compounding.

Design:
  - Trade size scales with wallet balance (gas-aware minimums)
  - Auto-compounding: profits increase position size
  - Drawdown circuit breaker: stops trading if daily loss exceeds threshold
  - Size ladder: promotes after N profitable trades, demotes after N losses
  - Never risks more than X% of wallet per trade (tiered)

Tiers (balance → max risk per trade):
  < 0.005 ETH  → 70% (micro, aggressive to grow)
  < 0.01 ETH   → 50% (small)
  < 0.05 ETH   → 30% (medium)
  < 0.1 ETH    → 20% (large)
  >= 0.1 ETH   → 10% (conservative at scale)

Gas-aware minimum: gas cost must be < 10% of notional.
Base gas ≈ $0.01-0.02 per swap → min trade ≈ $0.10-0.20
At ETH ~$2450 → min trade ≈ 0.00004 ETH (already handled by capital_band)
"""
import json
import os
from datetime import datetime, timezone

BOT = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BOT, "capital_state.json")
CAPS_FILE = os.path.join(BOT, "caps.json")

# Gas cost on Base (very low)
GAS_COST_ETH = 0.00004  # ~$0.10 per swap
MAX_GAS_PCT = 0.10  # max 10% gas cost per trade

# Tier thresholds (ETH) → max risk per trade
TIERS = [
    (0.005, 0.70),   # < 0.005 ETH: risk 70% per trade
    (0.01, 0.50),    # < 0.01 ETH: risk 50% per trade
    (0.05, 0.30),   # < 0.05 ETH: risk 30% per trade
    (0.1, 0.20),    # < 0.1 ETH: risk 20% per trade
    (float('inf'), 0.10),  # >= 0.1 ETH: risk 10% per trade
]

# Size ladder: step → size_eth, min_trades to promote, max_slippage_bps
SIZE_LADDER = [
    {"step": 1, "size_eth": 0.0005, "min_trades": 20, "max_slippage_bps": 100},
    {"step": 2, "size_eth": 0.001, "min_trades": 25, "max_slippage_bps": 100},
    {"step": 3, "size_eth": 0.0025, "min_trades": 30, "max_slippage_bps": 150},
    {"step": 4, "size_eth": 0.005, "min_trades": 40, "max_slippage_bps": 150},
    {"step": 5, "size_eth": 0.01, "min_trades": 50, "max_slippage_bps": 200},
]

# Demotion rules
DEMOTE_CONSECUTIVE_LOSSES = 3
DEMOTE_MAX_DRAWDOWN_PCT = 15  # 15% drawdown → demote


def load_state():
    """Load capital manager state (or initialize)."""
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE))
        except Exception:
            pass
    return {
        "current_step": 1,
        "trades_at_step": 0,
        "wins_at_step": 0,
        "losses_at_step": 0,
        "consecutive_losses": 0,
        "peak_balance_eth": 0.0,
        "total_trades": 0,
        "total_wins": 0,
        "total_losses": 0,
        "total_pnl_eth": 0.0,
        "last_promoted_at": None,
        "last_demoted_at": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def save_state(s):
    """Persist capital manager state."""
    json.dump(s, open(STATE_FILE, "w"), indent=2)


def get_tier(balance_eth):
    """Get the tier config for a given balance."""
    for threshold, risk_pct in TIERS:
        if balance_eth < threshold:
            return threshold, risk_pct
    return float('inf'), 0.10


def gas_aware_minimum():
    """Minimum trade size where gas < 10% of notional."""
    return GAS_COST_ETH / MAX_GAS_PCT


def calculate_position_size(balance_eth, confidence=1.0):
    """
    Calculate position size based on balance tier and confidence.
    
    Args:
        balance_eth: Current wallet balance in ETH
        confidence: 0.0-1.0 signal confidence (default 1.0)
    
    Returns:
        (size_eth, reason_str)
    """
    if balance_eth <= 0:
        return 0.0, "zero balance"
    
    # Get tier
    threshold, risk_pct = get_tier(balance_eth)
    
    # Base size: risk_pct of balance
    base_size = balance_eth * risk_pct
    
    # Apply confidence scaling (0.5 confidence → 50% size)
    size = base_size * confidence
    
    # Gas-aware minimum
    gas_min = gas_aware_minimum()
    if size < gas_min:
        size = gas_min
    
    # Don't exceed balance minus gas buffer
    gas_buffer = 0.0005
    max_affordable = balance_eth - gas_buffer
    if size > max_affordable:
        size = max_affordable
    
    # Don't go below minimum
    if size < gas_min:
        return 0.0, f"balance too low (need {gas_min:.6f} ETH min, have {balance_eth:.6f})"
    
    # Round to 6 decimal places
    size = round(size, 6)
    
    reason = f"tier<{threshold} risk={risk_pct*100:.0f}% conf={confidence:.1f} gas_min={gas_min:.6f}"
    return size, reason


def get_ladder_step(state):
    """Get the current size ladder step config."""
    step = state.get("current_step", 1)
    for s in SIZE_LADDER:
        if s["step"] == step:
            return s
    return SIZE_LADDER[0]


def record_trade(state, pnl_eth):
    """
    Record a trade result and update size ladder.
    
    Returns:
        (new_state, action_str) — action is "promoted", "demoted", or "held"
    """
    state["trades_at_step"] += 1
    state["total_trades"] += 1
    state["total_pnl_eth"] += pnl_eth
    
    if pnl_eth > 0:
        state["wins_at_step"] += 1
        state["total_wins"] += 1
        state["consecutive_losses"] = 0
    else:
        state["losses_at_step"] += 1
        state["total_losses"] += 1
        state["consecutive_losses"] += 1
    
    action = "held"
    
    # Check promotion
    step_config = get_ladder_step(state)
    if (state["trades_at_step"] >= step_config["min_trades"] and 
        state["wins_at_step"] > state["losses_at_step"] and
        state["current_step"] < len(SIZE_LADDER)):
        state["current_step"] += 1
        state["trades_at_step"] = 0
        state["wins_at_step"] = 0
        state["losses_at_step"] = 0
        state["last_promoted_at"] = datetime.now(timezone.utc).isoformat()
        action = "promoted"
    
    # Check demotion (consecutive losses)
    if state["consecutive_losses"] >= DEMOTE_CONSECUTIVE_LOSSES and state["current_step"] > 1:
        state["current_step"] -= 1
        state["trades_at_step"] = 0
        state["wins_at_step"] = 0
        state["losses_at_step"] = 0
        state["consecutive_losses"] = 0
        state["last_demoted_at"] = datetime.now(timezone.utc).isoformat()
        action = "demoted"
    
    # Check demotion (drawdown)
    peak = state.get("peak_balance_eth", 0)
    if peak > 0:
        current = peak + state["total_pnl_eth"]
        drawdown_pct = (peak - current) / peak * 100
        if drawdown_pct > DEMOTE_MAX_DRAWDOWN_PCT and state["current_step"] > 1:
            state["current_step"] -= 1
            state["trades_at_step"] = 0
            state["wins_at_step"] = 0
            state["losses_at_step"] = 0
            state["last_demoted_at"] = datetime.now(timezone.utc).isoformat()
            state["peak_balance_eth"] = current  # reset peak after demotion
            action = "demoted_drawdown"
    
    # Update peak balance
    if state["total_pnl_eth"] > 0 and state["total_pnl_eth"] > state.get("peak_balance_eth", 0):
        state["peak_balance_eth"] = state["total_pnl_eth"]
    
    save_state(state)
    return state, action


def get_recommended_size(state, balance_eth, confidence=1.0, atr_val=None, entry_price=None):
    """
    Get recommended position size using tier, ladder, and ATR.
    
    Returns the most conservative of:
    - Tier-based size (risk_pct of balance)
    - Ladder step size
    - ATR-based size (volatility-adjusted, if atr_val provided)
    
    Args:
        state: Capital manager state dict
        balance_eth: Current wallet balance in ETH
        confidence: 0.0-1.0 signal confidence
        atr_val: ATR value for volatility-based sizing (optional)
        entry_price: Entry price for ATR sizing (optional)
    
    Returns:
        (size_eth, reason_str)
    """
    # Tier-based size
    tier_size, tier_reason = calculate_position_size(balance_eth, confidence)
    
    # Ladder step size
    step_config = get_ladder_step(state)
    ladder_size = step_config["size_eth"]
    
    # ATR-based sizing (volatility-adjusted)
    atr_size = None
    if atr_val and entry_price and atr_val > 0 and entry_price > 0:
        try:
            from strategy import size_atr, load_strategy_params
            params = load_strategy_params()
            gas_min = gas_aware_minimum()
            params["band_min"] = gas_min
            params["band_max"] = max(gas_min, min(tier_size, ladder_size))
            atr_size = size_atr(balance_eth, atr_val, entry_price, params)
            if atr_size > ladder_size:
                atr_size = ladder_size
        except Exception:
            pass  # ATR sizing optional, fall through
    
    # Use the most conservative of all three
    size = min(tier_size, ladder_size)
    reason = f"min(tier={tier_size:.6f}, ladder={ladder_size:.6f})"
    
    if atr_size is not None and atr_size < size:
        size = atr_size
        reason = f"atr-limited ({atr_size:.6f})"
    elif atr_size is not None:
        reason += f" (atr={atr_size:.6f} ok)"
    
    return size, reason


def record_fill(state, balance_eth, pnl_eth=0.0):
    """Record a filled trade: update size ladder AND risk state."""
    # Update risk state (daily PnL tracking)
    try:
        from risk import record_fill as risk_record_fill
        risk_record_fill({"action": "swap", "amount_eth": 0, "token_out": ""}, pnl_eth=pnl_eth)
    except Exception:
        pass  # Risk state tracking is best-effort
    
    # Update size ladder
    return record_trade(state, pnl_eth)


def record_block(state, balance_eth, reason=""):
    """Record a blocked trade (no PnL change, but track the reason)."""
    # Don't count as trade, just log
    save_state(state)
    return state


def drawdown_guard(state, balance_eth):
    """
    Check if we should halt trading due to drawdown.
    
    Returns:
        (should_halt, reason_str)
    """
    peak = state.get("peak_balance_eth", 0)
    if peak <= 0:
        return False, "no peak recorded"
    
    current = peak + state.get("total_pnl_eth", 0)
    drawdown_pct = (peak - current) / peak * 100
    
    if drawdown_pct > DEMOTE_MAX_DRAWDOWN_PCT * 2:  # 30% drawdown = halt
        return True, f"drawdown {drawdown_pct:.1f}% exceeds 30% halt threshold"
    
    return False, f"drawdown {drawdown_pct:.1f}% (ok)"


def report(state, balance_eth):
    """Generate a capital management report."""
    lines = []
    lines.append("=" * 60)
    lines.append("  CAPITAL MANAGER REPORT")
    lines.append("=" * 60)
    lines.append("")
    lines.append(f"Balance: {balance_eth:.6f} ETH")
    lines.append(f"Tier: {get_tier(balance_eth)[0]} ETH threshold, {get_tier(balance_eth)[1]*100:.0f}% risk")
    lines.append("")
    lines.append("SIZE LADDER:")
    step = state.get("current_step", 1)
    lines.append(f"  Current step: {step}")
    sc = get_ladder_step(state)
    lines.append(f"  Step size: {sc['size_eth']} ETH")
    lines.append(f"  Trades at step: {state.get('trades_at_step', 0)}/{sc['min_trades']}")
    lines.append(f"  W/L at step: {state.get('wins_at_step', 0)}/{state.get('losses_at_step', 0)}")
    lines.append("")
    lines.append("OVERALL:")
    lines.append(f"  Total trades: {state.get('total_trades', 0)}")
    lines.append(f"  Total W/L: {state.get('total_wins', 0)}/{state.get('total_losses', 0)}")
    lines.append(f"  Total PnL: {state.get('total_pnl_eth', 0):+.6f} ETH")
    lines.append(f"  Consecutive losses: {state.get('consecutive_losses', 0)}")
    lines.append("")
    
    # Recommended size
    size, reason = get_recommended_size(state, balance_eth)
    lines.append(f"RECOMMENDED SIZE: {size:.6f} ETH ({reason})")
    
    # Drawdown
    halt, halt_reason = drawdown_guard(state, balance_eth)
    lines.append(f"Drawdown guard: {'HALT' if halt else 'OK'} ({halt_reason})")
    
    lines.append("=" * 60)
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    from risk import _live_balance
    
    bal = _live_balance()
    if bal is None:
        print("ERROR: Could not read wallet balance")
        sys.exit(1)
    
    state = load_state()
    
    if len(sys.argv) > 1 and sys.argv[1] == "--trade":
        # Simulate a trade result
        pnl = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
        state, action = record_trade(state, pnl)
        print(f"Recorded trade: PnL={pnl:+.6f} ETH → {action}")
        print(report(state, bal))
    else:
        print(report(state, bal))
