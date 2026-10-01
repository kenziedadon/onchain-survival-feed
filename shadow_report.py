#!/usr/bin/env python3
"""shadow_report.py — comprehensive shadow-mode monitoring report.

Shows what the bot WOULD have traded, hypothetical PnL, signal frequency,
and filter analysis. Run manually or via cron.

Usage:
    python3 shadow_report.py           # full report
    python3 shadow_report.py --compact # one-line summary
"""
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

BOT = "/home/kenzodadon/base-trend-bot"
TELEMETRY = os.path.join(BOT, "telemetry.jsonl")
SHADOW_FILE = os.path.join(BOT, "shadow_book.json")
CAPS_FILE = os.path.join(BOT, "caps.json")


def load_telemetry():
    records = []
    if not os.path.exists(TELEMETRY):
        return records
    with open(TELEMETRY) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def load_shadow():
    if os.path.exists(SHADOW_FILE):
        try:
            return json.load(open(SHADOW_FILE))
        except Exception:
            pass
    return {"open": {}, "closed": []}


def load_caps():
    if os.path.exists(CAPS_FILE):
        try:
            return json.load(open(CAPS_FILE))
        except Exception:
            pass
    return {}


def _load_edge_evidence(caps):
    """Load edge_evidence.json, fall back to caps edge_evidence if missing."""
    edge_file = os.path.join(BOT, "edge_evidence.json")
    if os.path.exists(edge_file):
        try:
            return json.load(open(edge_file))
        except Exception:
            pass
    # Fallback to caps.edge_evidence or default
    if "edge_evidence" in caps:
        return caps["edge_evidence"]
    return {"pass": False, "grid_pass": False}


def parse_ts(ts_str):
    try:
        return datetime.fromisoformat(ts_str)
    except Exception:
        return None


def generate_report(compact=False):
    records = load_telemetry()
    shadow = load_shadow()
    caps = load_caps()

    now = datetime.now(timezone.utc)

    # --- Header ---
    lines = []
    lines.append("=" * 60)
    lines.append("  SHADOW MODE REPORT")
    lines.append(f"  Generated: {now.strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("=" * 60)

    # --- Gate State ---
    lines.append("")
    lines.append("GATE STATE:")
    ks = caps.get("kill_switch", "?")
    lt = caps.get("live_trading", "?")
    ee = _load_edge_evidence(caps)  # caps first, then edge_evidence.json fallback
    lines.append(f"  kill_switch:   {'ARMED (safe)' if ks else 'OFF (live)'}")
    lines.append(f"  live_trading:  {'ON' if lt else 'off'}")
    lines.append(f"  edge_pass:     {ee.get('pass', '?')}")
    lines.append(f"  mode:          {'LIVE' if (not ks and lt) else 'DISARMED'}")

    if compact:
        # One-line summary
        open_count = len(shadow.get("open", {}))
        closed_count = len(shadow.get("closed", []))
        lines.append(f"  Shadow: {open_count} open / {closed_count} closed")
        lines.append("=" * 60)
        return "\n".join(lines)

    # --- Shadow Book ---
    open_pos = shadow.get("open", {})
    closed = shadow.get("closed", [])

    lines.append("")
    lines.append(f"SHADOW BOOK ({len(open_pos)} open, {len(closed)} closed):")

    if open_pos:
        lines.append("")
        lines.append("  Open positions:")
        for addr, pos in open_pos.items():
            name = pos.get("name", "?")
            ts = pos.get("ts", "?")[:16]
            reason = pos.get("reason", "?")
            price = pos.get("price_usd", 0)
            lines.append(f"    {name:25} ${price:>12.6f}  {ts}  {reason[:40]}")

    if closed:
        wins = sum(1 for p in closed if (p.get("pnl_pct") or 0) > 0)
        losses = sum(1 for p in closed if (p.get("pnl_pct") or 0) < 0)
        breakeven = len(closed) - wins - losses
        avg_pnl = sum(p.get("pnl_pct", 0) for p in closed) / len(closed) if closed else 0
        lines.append("")
        lines.append(f"  Closed: {len(closed)} total | {wins}W / {losses}L / {breakeven}BE")
        lines.append(f"  Avg PnL: {avg_pnl:+.2f}%")
        lines.append(f"  Win rate: {wins/len(closed)*100:.0f}%" if closed else "  Win rate: N/A")

        # Last 5 closed
        lines.append("")
        lines.append("  Last 5 closed:")
        for pos in closed[-5:]:
            pnl = pos.get("pnl_pct", 0)
            name = pos.get("name", "?")
            reason = pos.get("reason", "?")
            lines.append(f"    {name:25} {pnl:+.2f}%  {reason[:40]}")

    # --- Telemetry Analysis ---
    if not records:
        lines.append("")
        lines.append("No telemetry records found.")
        lines.append("=" * 60)
        return "\n".join(lines)

    # Filter to last 7 days
    week_ago = now - timedelta(days=7)
    recent = [r for r in records if parse_ts(r.get("ts", "")) and parse_ts(r["ts"]) > week_ago]

    lines.append("")
    lines.append(f"TELEMETRY (last 7 days, {len(recent)} records):")

    # By kind
    kinds = Counter(r.get("kind", "unknown") for r in recent)
    lines.append("  By type:")
    for kind, count in kinds.most_common(10):
        lines.append(f"    {kind:30} {count:5}")

    # Shadow entries by reason
    shadow_entries = [r for r in recent if r.get("kind") == "shadow_entry"]
    if shadow_entries:
        reasons = Counter(r.get("reason", "?") for r in shadow_entries)
        lines.append("")
        lines.append(f"  Shadow entries: {len(shadow_entries)} total")
        lines.append("  By skip reason:")
        for reason, count in reasons.most_common(10):
            lines.append(f"    {reason:40} {count:5}")

    # Signal frequency (entries per day)
    if recent:
        dates = Counter(r.get("ts", "")[:10] for r in recent if r.get("ts"))
        if dates:
            lines.append("")
            lines.append("  Activity by day:")
            for date, count in sorted(dates.items())[-7:]:
                lines.append(f"    {date}  {count:4} events")

    # EMA crossover analysis
    ema_skips = [r for r in recent if "skip_ema" in r.get("kind", "") or (r.get("reason") and "No crossover" in r["reason"])]
    ema_total = len(ema_skips)
    lines.append("")
    lines.append(f"  EMA crossover: {ema_total} skips (no signal)")

    # Unique tokens seen
    tokens = Counter(r.get("name", r.get("token", "?")) for r in recent if r.get("name") or r.get("token"))
    if tokens:
        lines.append("")
        lines.append("  Most scanned tokens (last 7d):")
        for name, count in tokens.most_common(10):
            lines.append(f"    {name:25} {count:5} scans")

    # --- Summary ---
    lines.append("")
    lines.append("=" * 60)
    lines.append("SUMMARY:")
    if not ks and lt:
        lines.append("  🔴 Bot is LIVE — trades WILL broadcast")
    else:
        lines.append("  🟢 Bot is DISARMED — shadow mode only")

    if closed:
        wins = sum(1 for p in closed if (p.get("pnl_pct") or 0) > 0)
        win_rate = wins / len(closed) * 100
        avg_pnl = sum(p.get("pnl_pct", 0) for p in closed) / len(closed)
        if win_rate >= 50 and avg_pnl > 0:
            lines.append(f"  🟢 Shadow PnL positive ({avg_pnl:+.1f}%, {win_rate:.0f}% win)")
        elif win_rate >= 40:
            lines.append(f"  🟡 Shadow PnL marginal ({avg_pnl:+.1f}%, {win_rate:.0f}% win)")
        else:
            lines.append(f"  🔴 Shadow PnL negative ({avg_pnl:+.1f}%, {win_rate:.0f}% win)")

    lines.append(f"  Shadow book: {len(open_pos)} open / {len(closed)} closed")
    lines.append("=" * 60)

    return "\n".join(lines)


if __name__ == "__main__":
    compact = "--compact" in sys.argv
    print(generate_report(compact=compact))
