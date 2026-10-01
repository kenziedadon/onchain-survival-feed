#!/usr/bin/env python3
"""shadow_learner.py — learn from shadow book to optimize strategy weights.

Minara Harness "Learning System" integration: reviews eligible shadow
decisions against later outcomes and evaluates lessons for future use.

The shadow_book records tokens the bot skipped. If those tokens went up,
our filters are too strict. If they went down, our filters are working.

This module:
  1. Reads shadow_book.json
  2. Computes PnL by skip reason and strategy
  3. Generates actionable insights (which filters to loosen/tighten)
  4. Exports weights that strategy_router consumes

Usage:
    python3 shadow_learner.py              # print report
    python3 shadow_learner.py --export     # export weights to shadow_weights.json
    python3 shadow_learner.py --json       # machine-readable output
"""
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from collections import defaultdict

BOT = os.path.dirname(os.path.abspath(__file__))
SHADOW_FILE = os.path.join(BOT, "shadow_book.json")
WEIGHTS_FILE = os.path.join(BOT, "shadow_weights.json")
CAPS_FILE = os.path.join(BOT, "caps.json")

# Minimum closed shadows before we trust the signal
MIN_SHADOWS = 3

# Skip reasons that map to specific filters we can adjust
REASON_MAP = {
    "skip_no_signal": {
        "description": "Router did not find a qualifying signal",
        "adjustment": "lower_confidence_threshold",
        "module": "strategy_router",
    },
    "skip_ema": {
        "description": "EMA crossover filter rejected",
        "adjustment": "loosen_ema_filter",
        "module": "strategy",
    },
    "skip_kinetic": {
        "description": "Kinetic/pullback filter rejected",
        "adjustment": "loosen_kinetic_filter",
        "module": "strategy",
    },
    "skip_volatility": {
        "description": "ATR volatility filter rejected",
        "adjustment": "loosen_volatility_filter",
        "module": "strategy_v2",
    },
    "skip_macro": {
        "description": "Macro regime veto",
        "adjustment": "loosen_macro_filter",
        "module": "macro_regime",
    },
    "skip_cooldown": {
        "description": "Exit cooldown anti-churn",
        "adjustment": "shorten_cooldown",
        "module": "executor",
    },
    "skip_blocked": {
        "description": "Blocked token (anti-reentry)",
        "adjustment": "review_blocked_list",
        "module": "executor",
    },
    "skip_max_positions": {
        "description": "No position slots available",
        "adjustment": "increase_max_positions",
        "module": "caps",
    },
    "skip_daily_loss": {
        "description": "Daily loss breaker triggered",
        "adjustment": "none (safety working correctly)",
        "module": "risk",
    },
    "gmgn_security": {
        "description": "GMGN security gate rejected",
        "adjustment": "review_security_rules",
        "module": "gmgn_bridge",
    },
}


def load_shadow_book():
    """Load shadow book from disk."""
    if not os.path.exists(SHADOW_FILE):
        return {"open": {}, "closed": []}
    try:
        with open(SHADOW_FILE) as f:
            return json.load(f)
    except Exception:
        return {"open": {}, "closed": []}


def load_caps():
    """Load caps.json for context."""
    try:
        with open(CAPS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def analyze(days=30):
    """
    Analyze shadow book and return insights.

    Args:
        days: lookback window for closed shadows

    Returns:
        dict with status, insights by reason, and recommended weights
    """
    book = load_shadow_book()
    closed = book.get("closed", [])

    if not closed:
        return {
            "status": "no_data",
            "message": "No closed shadows yet — run the bot to accumulate data",
            "weights": _default_weights(),
        }

    # Filter to recent window
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    recent = []
    for s in closed:
        try:
            ts = datetime.fromisoformat(s.get("ts", ""))
            if ts >= cutoff:
                recent.append(s)
        except Exception:
            continue

    if not recent:
        return {
            "status": "stale",
            "message": f"No closed shadows in last {days} days",
            "weights": _default_weights(),
        }

    # Group by reason
    by_reason = defaultdict(list)
    for s in recent:
        reason = s.get("reason", "unknown")
        # Normalize reason (some have details after colon)
        base_reason = reason.split(":")[0] if ":" in reason else reason
        pnl = s.get("pnl_pct", 0)
        by_reason[base_reason].append({
            "pnl_pct": pnl,
            "name": s.get("name", "unknown"),
            "token": s.get("token", ""),
            "entry": s.get("entry_price_usd", 0),
            "exit": s.get("exit_price_usd", 0),
            "how": s.get("how", "unknown"),
        })

    insights = {}
    for reason, entries in by_reason.items():
        pnls = [e["pnl_pct"] for e in entries]
        avg_pnl = sum(pnls) / len(pnls)
        median_pnl = sorted(pnls)[len(pnls) // 2]
        win_rate = sum(1 for p in pnls if p > 0) / len(pnls)

        # Determine action
        mapping = REASON_MAP.get(reason, {})
        adjustment = mapping.get("adjustment", "review")

        if avg_pnl > 0.05 and len(entries) >= MIN_SHADOWS:
            action = "LOOSEN"
            action_detail = f"Shadows avg +{avg_pnl*100:.1f}% — filter may be too strict"
        elif avg_pnl > 0.02 and len(entries) >= MIN_SHADOWS:
            action = "MONITOR"
            action_detail = f"Shadows slightly positive (+{avg_pnl*100:.1f}%) — watch"
        elif avg_pnl < -0.05 and len(entries) >= MIN_SHADOWS:
            action = "TIGHTEN"
            action_detail = f"Shadows avg {avg_pnl*100:.1f}% — filter working, could be stricter"
        else:
            action = "KEEP"
            action_detail = f"Shadows neutral ({avg_pnl*100:.1f}%) — filter calibrated"

        insights[reason] = {
            "count": len(entries),
            "avg_pnl_pct": round(avg_pnl * 100, 2),
            "median_pnl_pct": round(median_pnl * 100, 2),
            "win_rate": round(win_rate, 3),
            "action": action,
            "action_detail": action_detail,
            "adjustment": adjustment,
            "module": mapping.get("module", "unknown"),
            "sample_entries": entries[:3],  # include a few examples
        }

    # Generate strategy weights
    weights = _compute_weights(insights)

    return {
        "status": "ok",
        "period_days": days,
        "total_closed": len(recent),
        "by_reason": insights,
        "weights": weights,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _default_weights():
    """Default equal weights for all strategies."""
    return {
        "ema_crossover": 1.0,
        "rsi_meanrev": 1.0,
        "momentum_breakout": 1.0,
        "volume_spike": 1.0,
        "pullback": 1.0,
        "confidence_threshold": 0.3,
    }


def _compute_weights(insights):
    """
    Compute strategy weights based on shadow PnL analysis.

    If skip_no_signal shadows are strongly positive, the router is too
    conservative — lower the confidence threshold and boost all strategies.
    """
    weights = _default_weights()

    # Check skip_no_signal insights
    signal_insight = insights.get("skip_no_signal", {})
    if signal_insight.get("action") == "LOOSEN":
        # Shadows are positive → lower confidence threshold
        weights["confidence_threshold"] = max(0.2, 0.3 - 0.05)
        # Boost all strategies slightly
        for k in ["ema_crossover", "rsi_meanrev", "momentum_breakout", "volume_spike", "pullback"]:
            weights[k] = 1.2
    elif signal_insight.get("action") == "MONITOR":
        weights["confidence_threshold"] = 0.25

    # Check skip_ema insights
    ema_insight = insights.get("skip_ema", {})
    if ema_insight.get("action") == "LOOSEN":
        weights["ema_crossover"] = 1.3

    # Check skip_kinetic insights
    kinetic_insight = insights.get("skip_kinetic", {})
    if kinetic_insight.get("action") == "LOOSEN":
        weights["pullback"] = 1.3

    # Check skip_volatility insights
    vol_insight = insights.get("skip_volatility", {})
    if vol_insight.get("action") == "LOOSEN":
        weights["momentum_breakout"] = 1.2

    return weights


def export_weights(analysis):
    """Export weights to JSON file for strategy_router to consume."""
    output = {
        "weights": analysis.get("weights", _default_weights()),
        "generated_at": analysis.get("generated_at", datetime.now(timezone.utc).isoformat()),
        "status": analysis.get("status", "unknown"),
    }
    with open(WEIGHTS_FILE, "w") as f:
        json.dump(output, f, indent=2)
    return WEIGHTS_FILE


def print_report(analysis):
    """Print human-readable report."""
    print("=" * 60)
    print("SHADOW LEARNER REPORT")
    print("=" * 60)
    print(f"Status: {analysis['status']}")
    print(f"Generated: {analysis.get('generated_at', 'unknown')}")
    print()

    if analysis["status"] == "no_data":
        print(analysis["message"])
        return

    print(f"Closed shadows (last {analysis.get('period_days', 30)}d): {analysis['total_closed']}")
    print()

    insights = analysis.get("by_reason", {})
    if not insights:
        print("No insights generated.")
        return

    print("INSIGHTS BY SKIP REASON:")
    print("-" * 60)
    for reason, detail in sorted(insights.items()):
        print(f"\n  {reason}:")
        print(f"    Count: {detail['count']}")
        print(f"    Avg PnL: {detail['avg_pnl_pct']:+.2f}%")
        print(f"    Win Rate: {detail['win_rate']:.1%}")
        print(f"    Action: {detail['action']} — {detail['action_detail']}")
        if detail['sample_entries']:
            print(f"    Samples:")
            for e in detail['sample_entries'][:2]:
                print(f"      - {e['name']}: {e['pnl_pct']:+.1f}% ({e['how']})")

    print()
    print("STRATEGY WEIGHTS:")
    print("-" * 60)
    weights = analysis.get("weights", {})
    for k, v in weights.items():
        print(f"  {k}: {v}")
    print()


if __name__ == "__main__":
    analysis = analyze()

    if "--json" in sys.argv:
        print(json.dumps(analysis, indent=2, default=str))
    else:
        print_report(analysis)

    if "--export" in sys.argv:
        path = export_weights(analysis)
        print(f"Exported weights to {path}")
