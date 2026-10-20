#!/usr/bin/env python3
"""Exit-automation lane — STONKEX only, shadow mode (signs nothing).

WO: exit-automation-wo-2026-10-20 (Phase 2 build).
Lane ships DISARMED. Shadow mode logs would-be actions; signs nothing.

Price basis: GeckoTerminal hourly OHLCV (USD-denominated close).
Lines: STONKEX cut 0.0050 (two consecutive closes below → sell ALL),
       TP1 0.011182 (sell 1/3), TP2 0.016773 (sell 1/3).
Output: ETH (single-leg token→WETH via Uniswap V3, fee=10000bps).
Slippage bound: 300 bps/leg.
"""
import json
import os
import time
import urllib.request
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
STONKEX_TOKEN = "0x5ab000ff9b9ffe0349ce5ffa5fd86f217c3680f5"
STONKEX_POOL = "0x7692acc1cdd771d09ebcae3663e1843b2911bec7"
STONKEX_DECIMALS = 18

CUT_PRICE = 0.0050
TP1_PRICE = 0.011182
TP2_PRICE = 0.016773

HELD_AMOUNT = 447.1503235
TP_FRACTION = 1.0 / 3.0

SLIPPAGE_BPS = 300
GECKO_URL = ("https://api.geckoterminal.com/api/v2/networks/base/pools/"
             "{pool}/ohlcv/hour?aggregate=1&limit={limit}")

STATE_DIR = os.path.expanduser("~/.exit-lane")
STATE_FILE = os.path.join(STATE_DIR, "exit_lane_state.json")
AUDIT_FILE = os.path.join(STATE_DIR, "exit_lane_audit.jsonl")
CAPS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "exit_lane_caps.json")


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------
def _ensure_dir():
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)


def default_state():
    return {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "positions": {
            "STONKEX": {
                "token": STONKEX_TOKEN,
                "pool": STONKEX_POOL,
                "decimals": STONKEX_DECIMALS,
                "held_amount": HELD_AMOUNT,
                "lines": {
                    "cut": {
                        "price": CUT_PRICE,
                        "condition": "two_consecutive_closes_below",
                        "fired": True,       # already fired 2026-10-19 + 2026-10-20
                        "fired_at": "2026-10-19",
                        "executed": False,
                        "executed_at": None,
                        "executed_tx": None,
                    },
                    "tp1": {
                        "price": TP1_PRICE,
                        "fraction": TP_FRACTION,
                        "fired": False,
                        "executed": False,
                        "executed_at": None,
                        "executed_tx": None,
                    },
                    "tp2": {
                        "price": TP2_PRICE,
                        "fraction": TP_FRACTION,
                        "fired": False,
                        "executed": False,
                        "executed_at": None,
                        "executed_tx": None,
                    },
                },
                "consecutive_below_count": 0,
                "last_close": None,
                "last_close_ts": None,
                "last_check_ts": None,
            }
        },
    }


def load_state():
    _ensure_dir()
    if not os.path.exists(STATE_FILE):
        st = default_state()
        save_state(st)
        return st
    with open(STATE_FILE) as f:
        return json.load(f)


def save_state(state):
    _ensure_dir()
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)
    os.chmod(STATE_FILE, 0o600)


# ---------------------------------------------------------------------------
# Caps (separate from probe)
# ---------------------------------------------------------------------------
def load_caps():
    if not os.path.exists(CAPS_FILE):
        return {"kill_switch": True, "live_trading": False, "slippage_bps": 300,
                "sell_only": True, "output_asset": "ETH"}
    with open(CAPS_FILE) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Price fetch (read-only, GeckoTerminal)
# ---------------------------------------------------------------------------
def fetch_hourly_closes(pool, limit=5):
    """Fetch the last `limit` completed hourly candles. Returns list of
    (timestamp, close) tuples, oldest first. Fail-closed: returns None on error."""
    url = GECKO_URL.format(pool=pool, limit=limit)
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json;version=20230302",
        })
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
        candles = data.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
        if not candles:
            return None
        # ohlcv_list is newest-first; reverse to oldest-first
        candles = list(reversed(candles))
        now = time.time()
        result = []
        for c in candles:
            ts, o, h, l, close, vol = c[0], c[1], c[2], c[3], c[4], c[5]
            # only completed candles
            if now >= ts + 3600:
                result.append((ts, float(close)))
        return result if result else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------
def audit(record):
    _ensure_dir()
    with open(AUDIT_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# Evaluation (shadow mode — logs would-be actions, signs nothing)
# ---------------------------------------------------------------------------
def evaluate(shadow=True):
    """Evaluate STONKEX lines against the latest completed hourly close.

    Returns a dict with:
      - close_price, close_ts
      - consecutive_below_count
      - actions: list of {line, action, amount, reason}
      - skipped: list of {line, reason}
    """
    caps = load_caps()
    state = load_state()
    pos = state["positions"]["STONKEX"]
    now_iso = datetime.now(timezone.utc).isoformat()

    result = {
        "timestamp": now_iso,
        "shadow": shadow,
        "close_price": None,
        "close_ts": None,
        "consecutive_below_count": pos["consecutive_below_count"],
        "actions": [],
        "skipped": [],
        "errors": [],
    }

    # Fetch price basis
    closes = fetch_hourly_closes(pos["pool"], limit=5)
    if closes is None:
        result["errors"].append("price_fetch_failed: GeckoTerminal returned no completed candles")
        audit({"event": "eval_error", "reason": "price_fetch_failed", "ts": now_iso})
        save_state(state)
        return result

    # Use the most recent completed close
    close_ts, close_price = closes[-1]
    prev_close_ts = pos.get("last_close_ts")
    prev_close = pos.get("last_close")

    result["close_price"] = close_price
    result["close_ts"] = close_ts

    # Consecutive-below logic for cut
    # If this is a new close (not already processed), update count
    if prev_close_ts is None or close_ts > prev_close_ts:
        if close_price < CUT_PRICE:
            pos["consecutive_below_count"] += 1
        else:
            pos["consecutive_below_count"] = 0
        pos["last_close"] = close_price
        pos["last_close_ts"] = close_ts

    result["consecutive_below_count"] = pos["consecutive_below_count"]

    # Evaluate lines
    lines = pos["lines"]
    held = pos["held_amount"]

    # CUT (two consecutive closes below)
    cut = lines["cut"]
    if cut["executed"]:
        result["skipped"].append({"line": "cut", "reason": "already_executed"})
    elif cut["fired"] and not cut["executed"]:
        # Already fired (2026-10-19 + 2026-10-20) — execute on activation
        if caps.get("kill_switch", True):
            result["skipped"].append({"line": "cut", "reason": "disarmed (kill_switch=true)"})
            result["actions"].append({
                "line": "cut",
                "action": "WOULD_SELL_ALL",
                "amount_tokens": held,
                "amount_eth_est": None,
                "reason": "already_fired_cut_execute_on_activation",
                "shadow": shadow,
            })
        else:
            result["actions"].append({
                "line": "cut",
                "action": "SELL_ALL",
                "amount_tokens": held,
                "reason": "already_fired_cut_execute_on_activation",
                "shadow": shadow,
            })
    elif pos["consecutive_below_count"] >= 2:
        # Fresh fire
        cut["fired"] = True
        cut["fired_at"] = now_iso
        result["actions"].append({
            "line": "cut",
            "action": "WOULD_SELL_ALL" if shadow else "SELL_ALL",
            "amount_tokens": held,
            "reason": "two_consecutive_closes_below",
            "shadow": shadow,
        })
        if not shadow and not caps.get("kill_switch", True):
            cut["executed"] = True
            cut["executed_at"] = now_iso
    else:
        result["skipped"].append({
            "line": "cut",
            "reason": f"consecutive_below_count={pos['consecutive_below_count']} (need 2)",
        })

    # TP1
    tp1 = lines["tp1"]
    if tp1["executed"]:
        result["skipped"].append({"line": "tp1", "reason": "already_executed"})
    elif close_price >= TP1_PRICE and not tp1["fired"]:
        tp1["fired"] = True
        tp1["fired_at"] = now_iso
        sell_amount = held * TP_FRACTION
        result["actions"].append({
            "line": "tp1",
            "action": "WOULD_SELL_THIRD" if shadow else "SELL_THIRD",
            "amount_tokens": sell_amount,
            "reason": f"close {close_price} >= TP1 {TP1_PRICE}",
            "shadow": shadow,
        })
        if not shadow and not caps.get("kill_switch", True):
            tp1["executed"] = True
            tp1["executed_at"] = now_iso
    else:
        result["skipped"].append({
            "line": "tp1",
            "reason": f"close {close_price} < TP1 {TP1_PRICE} or already fired",
        })

    # TP2
    tp2 = lines["tp2"]
    if tp2["executed"]:
        result["skipped"].append({"line": "tp2", "reason": "already_executed"})
    elif close_price >= TP2_PRICE and not tp2["fired"]:
        tp2["fired"] = True
        tp2["fired_at"] = now_iso
        sell_amount = held * TP_FRACTION
        result["actions"].append({
            "line": "tp2",
            "action": "WOULD_SELL_THIRD" if shadow else "SELL_THIRD",
            "amount_tokens": sell_amount,
            "reason": f"close {close_price} >= TP2 {TP2_PRICE}",
            "shadow": shadow,
        })
        if not shadow and not caps.get("kill_switch", True):
            tp2["executed"] = True
            tp2["executed_at"] = now_iso
    else:
        result["skipped"].append({
            "line": "tp2",
            "reason": f"close {close_price} < TP2 {TP2_PRICE} or already fired",
        })

    pos["last_check_ts"] = now_iso
    save_state(state)

    # Audit
    audit({
        "event": "eval",
        "shadow": shadow,
        "close_price": close_price,
        "close_ts": close_ts,
        "consecutive_below_count": pos["consecutive_below_count"],
        "actions": result["actions"],
        "skipped": result["skipped"],
        "ts": now_iso,
    })

    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Exit-automation lane (STONKEX, shadow mode)")
    parser.add_argument("--shadow", action="store_true", default=True,
                        help="Shadow mode (default: True). Signs nothing.")
    parser.add_argument("--live", action="store_true", default=False,
                        help="Live mode (requires caps kill_switch=false). NOT for Phase 2.")
    args = parser.parse_args()

    shadow = not args.live
    if args.live:
        caps = load_caps()
        if caps.get("kill_switch", True) or not caps.get("live_trading", False):
            print("DISARMED: kill_switch=true or live_trading=false. Shadow mode only.")
            shadow = True

    print(f"Exit lane: STONKEX, shadow={shadow}")
    result = evaluate(shadow=shadow)
    print(json.dumps(result, indent=2))