#!/usr/bin/env python3
"""
BASE TRENDING-COIN INTELLIGENCE REPORT
=====================================
The sellable product: a ranked, scored feed of trending + early Base coins,
delivered on a schedule. Reuses the proven discovery module (trend_discovery.py).
Pure stdlib + keyless GeckoTerminal/DEXScreener. Dry-run only — never trades.

Outputs a clean plain-text report to stdout (so a cron with no_agent=true can
deliver it verbatim to Telegram as your intel feed / white-label product).

Usage:
  python3 trend_report.py            # print report
  (cron invokes this; its stdout = the delivered message)
"""
import sys, json, os, time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
from trend_discovery import (gecko_trending, gecko_new, dexscreener_boosts,
                             score, MIN_LIQUIDITY, MIN_5M_VOL, BLUECHIPS)

def build_report():
    g_trend = gecko_trending(); time.sleep(1.0)
    g_new = gecko_new();         time.sleep(1.0)
    dx = dexscreener_boosts()

    merged = {}
    for src in (g_trend, g_new, dx):
        for addr, c in src.items():
            if addr in merged:
                merged[addr]["reasons"] = list(set(merged[addr]["reasons"] + c["reasons"]))
                merged[addr]["liquidity"] = max(merged[addr]["liquidity"], c["liquidity"])
                merged[addr]["vol_5m"] = max(merged[addr]["vol_5m"], c["vol_5m"])
                merged[addr]["source"] = merged[addr]["source"] + "+" + c["source"]
            else:
                merged[addr] = c

    coins = [score(c) for c in merged.values()]
    coins = [c for c in coins if c["liquidity"] >= MIN_LIQUIDITY]
    coins.sort(key=lambda x: x["score"], reverse=True)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    L = []
    L.append("BASE TRENDING-COIN INTEL  " + now)
    L.append("=" * 54)
    L.append(f"Tracked: {len(coins)} Base coins (ETH-paired, liq>=$%s)" % f"{MIN_LIQUIDITY:,.0f}")
    L.append("")

    # Early-signal cohort (the real edge: new + boosted, not yet trending)
    early = [c for c in coins if ("early" in c["reasons"] or "boosted" in c["reasons"])]
    L.append("EARLY SIGNALS (pre-trend, higher upside/risk):")
    if early:
        for c in early[:8]:
            flag = "NEW" if "early" in c["reasons"] else "BOOST"
            L.append(f"  [{flag}] {c['name'][:22]:22} liq=${c['liquidity']:>10,.0f} "
                     f"v5m=${c['vol_5m']:>8,.0f} score={c['score']}")
    else:
        L.append("  (none qualifying right now — early pools are sub-$150k / <2h history)")
    L.append("")

    L.append("TOP TRENDING (already hot, liquid):")
    for i, c in enumerate(coins[:12]):
        src = "trend" if "gecko_trending" in c["source"] else c["source"][:10]
        L.append(f"  {i+1:2}. {c['name'][:24]:24} ${c['liquidity']:>10,.0f} "
                 f"v5m=${c['vol_5m']:>8,.0f} fee={c['fee_bps']:.0f}bp [{src}]")
    L.append("")

    # profitability status (cached from last grid run, if any)
    verdict = "monitoring"
    try:
        gr = json.load(open(os.path.join(os.path.dirname(__file__), "grid_results.json")))
        if gr:
            b = gr[0]
            verdict = (f"trending-basket momentum NOT profitable yet "
                       f"(best {b['avg']:+.1f}% avg, {b['prof']}/{b['n']} coins positive) "
                       f"— do not chase; watch early signals")
    except Exception:
        pass
    L.append("STRATEGY STATUS: " + verdict)
    L.append("")
    L.append("Sources: GeckoTerminal trending/new pools + DEXScreener boosts (Base-only).")
    return "\n".join(L)

if __name__ == "__main__":
    print(build_report())
