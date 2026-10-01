#!/usr/bin/env python3
"""failure_digest.py — mine telemetry + shadow book into a human digest.

Run via cron (or manually): python3 failure_digest.py [--days 7]
Prints: top failure kinds, top blocked tokens, RPC error patterns, and the
shadow-book verdict on whether skipped signals carried edge.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import telemetry
import shadow_book


def main():
    days = 7
    if "--days" in sys.argv:
        days = int(sys.argv[sys.argv.index("--days") + 1])
    s = telemetry.summarize(days)
    lines = [f"=== FAILURE DIGEST (last {days}d) ===", f"total events: {s['total']}", ""]
    lines.append("-- by kind --")
    for k, n in sorted(s.get("by_kind", {}).items(), key=lambda kv: -kv[1]):
        lines.append(f"  {k}: {n}")
    lines.append("")
    lines.append("-- most-skipped tokens (free experiments) --")
    for t, n in s.get("top_skipped_tokens", {}).items():
        lines.append(f"  {t}: {n}x")
    lines.append("")
    lines.append("-- recurring reasons --")
    for r, n in s.get("by_reason", {}).items():
        lines.append(f"  {n}x {r}")
    lines.append("")
    # shadow verdict
    book = shadow_book._load()
    closed = book.get("closed", [])
    if closed:
        wins = sum(1 for c in closed if c.get("pnl_pct", 0) > 0)
        avg = sum(c.get("pnl_pct", 0) for c in closed) / len(closed)
        by_reason = {}
        for c in closed:
            by_reason.setdefault(c.get("reason", "?"), []).append(c.get("pnl_pct", 0))
        lines.append("-- SHADOW BOOK VERDICT (would skipped trades have made money?) --")
        lines.append(f"  closed: {len(closed)} | win rate: {wins}/{len(closed)} | avg pnl: {avg:+.2%}")
        for r, ps in sorted(by_reason.items()):
            wr = sum(1 for p in ps if p > 0) / len(ps)
            lines.append(f"    {r}: n={n if (n:=len(ps)) else 0} win_rate={wr:.0%} avg={sum(ps)/len(ps):+.2%}")
        lines.append("")
        lines.append("  RULE: if a skip-reason's shadow win rate >55% with positive avg,")
        lines.append("  that filter is destroying edge — retune it.")
    else:
        lines.append(f"-- shadow book: {len(book.get('open', {}))} open, none closed yet --")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
