#!/usr/bin/env python3
import sys, json, time, urllib.request
sys.path.insert(0, '/home/kenzodadon/base-trend-bot')

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json;version=20230302"}

def get(url, timeout=15, retries=3):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode()
        except Exception as e:
            wait = 3 + i*2 if "429" in str(e) else 1.5
            if i < retries - 1:
                time.sleep(wait); continue
            raise

from discovery import discover_trending
from rpc_verify import RpcClient, WETH, verify_pool
from trend_discovery import (gecko_trending, gecko_new, dexscreener_boosts,
    gmgn_trending, fomo_trending, score, MANUAL_TOKENS)

print("=== SOURCES ===")
g_trend = gecko_trending(); print(f"gecko_trending: {len(g_trend)}")
g_new = gecko_new(); print(f"gecko_new: {len(g_new)}")
dx = dexscreener_boosts(); print(f"dexscreener: {len(dx)}")
gm = gmgn_trending(); print(f"gmgn: {len(gm)}")
fm = fomo_trending(); print(f"fomo: {len(fm)}")

merged = {}
for src in (g_trend, g_new, dx, gm, fm):
    for addr, c in src.items():
        if addr in merged:
            merged[addr]["reasons"] = list(set(merged[addr].get("reasons",[]) + c.get("reasons",[])))
            merged[addr]["liquidity"] = max(merged[addr]["liquidity"], c["liquidity"])
            merged[addr]["vol_5m"] = max(merged[addr]["vol_5m"], c["vol_5m"])
            merged[addr]["source"] = "multi:" + merged[addr]["source"] + "+" + c["source"]
        else:
            merged[addr] = c
for addr, c in MANUAL_TOKENS.items():
    if addr not in merged:
        merged[addr] = c

coins = [score(c) for c in merged.values()]
coins = [c for c in coins if (c["source"] in ("gmgn_trending","manual_add") or c["liquidity"] >= 250_000)
         and "USDC" not in c["name"].upper() and "USDT" not in c["name"].upper() and "DAI" not in c["name"].upper()]

stale = [c for c in coins if c["score"] == 0 and c["source"] != "manual_add"]
coins = [c for c in coins if c["score"] > 0 or c["source"] == "manual_add"]

for c in coins:
    pool_addr = c.get("address")
    if pool_addr and len(pool_addr) == 42 and c.get("token_addr") is None:
        c["token_addr"] = None  # resolve below

coins.sort(key=lambda x: x["score"], reverse=True)

print(f"\nAfter filter: {len(coins)} coins (stale: {len(stale)})")
print("\n=== Coins ===")
for i, c in enumerate(coins):
    tok = c.get("token_addr") or "(unresolved)"
    print(f"{i+1}. {c['name'][:22]:22} score={c['score']:>3} tok={str(tok)[:24]} pool={c.get('address','')[:24]}")

# On-chain gate
print("\n=== ON-CHAIN GATE ===")
_rpc = RpcClient(timeout=8)
verified, unverified = [], []
for i, c in enumerate(coins):
    token = c.get("token_addr") or c.get("address")
    pool = c.get("address")
    print(f"\n[{i+1}/{len(coins)}] {c['name'][:20]} token={str(token)[:30]}")
    if not token or len(token) != 42:
        c["verified"] = False; c["reason"] = "no token addr"
        unverified.append(c); continue
    try:
        code = _rpc.get_code(token)
        if code in ("0x","0x0",None,""):
            c["verified"] = False; c["reason"] = "no code"
            unverified.append(c); print(f"  -> REJECT: no code"); continue
        c["onchain"] = {"codesize": max(0,(len(code)-2)//2)}
        if pool and pool != token and len(pool) == 42:
            r = verify_pool(_rpc, pool, WETH, token)
            if r["verified"]:
                c["onchain"].update(token0=r["token0"], token1=r["token1"])
                verified.append(c); print(f"  -> OK codesize={r['codesize']}")
            else:
                c["verified"] = False; c["reason"] = r["reason"]
                unverified.append(c); print(f"  -> REJECT: {r['reason'][:50]}")
        else:
            verified.append(c); print(f"  -> OK (token-only, cs={c['onchain']['codesize']})")
    except Exception as e:
        c["verified"] = False; c["reason"] = f"rpc fail: {e}"
        unverified.append(c); print(f"  -> ERROR: {e}")

print(f"\n=== RESULT: {len(verified)} verified, {len(unverified)} rejected ===")
if unverified:
    for c in unverified:
        print(f"  - {c['name'][:26]:26} {(c.get('reason') or '?')[:60]}")

# GMGN security + risk officer + token grade (fast, no rpc)
print("\n=== POST-GATES ===")
try:
    from gmgn_bridge import security_check
    safe, unsafe = [], []
    for c in verified:
        addr = c.get("token_addr") or c.get("address")
        if not addr or len(addr) != 42: continue
        if c.get("source") == "manual_add": safe.append(c); continue
        sec = security_check(addr)
        if sec.get("safe"): safe.append(c)
        else: unsafe.append(c)
    if unsafe:
        print(f"GMGN dropped {len(unsafe)}: " + "; ".join(f"{c['name'][:15]}:{c.get('gmgn_reject','')[:20]}" for c in unsafe))
    verified = safe
except Exception as e:
    print(f"GMGN gate unavailable: {e}")

from risk_officer import veto_check
passed, vetoed = [], []
for c in verified:
    prop = {"action":"swap","token_out":c.get("token_addr"),"pool":c.get("address"),
            "amount_eth":0.0005,"score":c.get("score",0),"slippage_bps":c.get("fee_bps",30),
            "liquidity":c.get("liquidity",0),"vol_5m":c.get("vol_5m",0),"source":c.get("source","")}
    ok, fails = veto_check(prop)
    if ok: passed.append(c)
    else: vetoed.append(c)
if vetoed:
    print(f"Officer vetoed {len(vetoed)}: " + "; ".join(f"{c['name'][:15]}:{c.get('officer_veto','')[:30]}" for c in vetoed))
verified = passed

try:
    from token_grade import TokenGrader
    grader = TokenGrader()
    graded = []
    for c in verified:
        addr = c.get("token_addr") or c.get("address")
        if not addr or len(addr) != 42: continue
        grade = grader.grade(addr)
        if grade["verdict"] != "BLOCK":
            c["token_grade"] = grade; graded.append(c)
        else:
            print(f"BLOCKED: {c['name'][:22]} - {', '.join(grade['reasons'][:2])}")
    verified = graded
except Exception as e:
    print(f"Token grade unavailable: {e}")

print(f"\n=== FINAL: {len(verified)} discovered coins ===")
for i, c in enumerate(verified[:15]):
    pace = c.get("live_pace")
    ps = f"{pace:.2f}x" if pace else "n/a"
    print(f"  {i+1:2}. {c['name'][:26]:26} score={c['score']:>3} liq=${c['liquidity']:>10,.0f} v5m=${c['vol_5m']:>8,.0f} pace={ps:>6} [{c['source']}]")

# Write cache
from datetime import datetime, timezone
from _atomic_write import atomic_jsonl_append
rec = {"ts": datetime.now(timezone.utc).isoformat(), "coins": verified}
atomic_jsonl_append("/home/kenzodadon/base-trend-bot/discovered.jsonl", rec)
print(f"\nWrote {len(verified)} coins to discovered.jsonl")
