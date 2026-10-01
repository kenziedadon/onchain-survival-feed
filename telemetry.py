#!/usr/bin/env python3
"""telemetry.py — unified failure/skip ledger for base-trend-bot.

Every skip, block, RPC failure, or swap error anywhere in the pipeline gets
logged here as one JSON line in telemetry.jsonl. The point: failures are data.
A signal the bot skipped is an experiment running for free — we track what it
would have done (shadow_book.py) and mine the ledger for recurring blockers.

API:
    log_event(kind, token=None, name=None, reason=None, meta=None, price_usd=None)
    summarize(days=7) -> dict of counts by kind/reason
"""
import json
import os
import threading
from datetime import datetime, timezone, timedelta

LEDGER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "telemetry.jsonl")
_lock = threading.Lock()

# Event kinds:
#   skip_entry_timing   — kinetic/pullback filter rejected
#   skip_kinetic        — kinetic filter rejected
#   skip_volatility     — ATR volatility filter rejected
#   skip_macro          — macro regime veto
#   skip_cooldown       — exit cooldown anti-churn
#   skip_blocked        — _blocked_today anti-reentry
#   skip_duplicate      — in-flight dedupe
#   skip_max_positions  — no slots
#   skip_daily_loss     — daily loss breaker
#   rpc_error           — transient RPC/API failure
#   sell_path_blocked   — deterministic sell-side failure
#   swap_fail           — broadcast/execution failure
#   unwrap_fail         — WETH unwrap failure
#   shadow_entry        — recorded by shadow_book.py for a skipped candidate
#   shadow_exit         — shadow position closed with hypothetical pnl


def log_event(kind, token=None, name=None, reason=None, meta=None, price_usd=None):
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "token": token,
        "name": name,
        "reason": reason,
        "price_usd": price_usd,
        "meta": meta or {},
    }
    line = json.dumps(rec, default=str)
    try:
        with _lock:
            with open(LEDGER, "a") as f:
                f.write(line + "\n")
    except Exception:
        pass  # telemetry must never break trading
    return rec


def summarize(days=7):
    """Aggregate ledger over trailing N days. Never raises."""
    out = {"total": 0, "by_kind": {}, "by_reason": {}, "top_skipped_tokens": {}}
    if not os.path.exists(LEDGER):
        return out
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        with open(LEDGER) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                try:
                    ts = datetime.fromisoformat(r["ts"])
                except Exception:
                    continue
                if ts < cutoff:
                    continue
                out["total"] += 1
                k = r.get("kind", "?")
                out["by_kind"][k] = out["by_kind"].get(k, 0) + 1
                if r.get("reason"):
                    key = f"{k}: {r['reason'][:80]}"
                    out["by_reason"][key] = out["by_reason"].get(key, 0) + 1
                if k.startswith(("skip", "shadow_entry")) and r.get("name"):
                    t = r["name"]
                    out["top_skipped_tokens"][t] = out["top_skipped_tokens"].get(t, 0) + 1
    except Exception as e:
        out["error"] = str(e)
    out["top_skipped_tokens"] = dict(sorted(out["top_skipped_tokens"].items(),
                                            key=lambda kv: -kv[1])[:15])
    out["by_reason"] = dict(sorted(out["by_reason"].items(), key=lambda kv: -kv[1])[:15])
    return out


if __name__ == "__main__":
    import sys
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    print(json.dumps(summarize(days), indent=2))
