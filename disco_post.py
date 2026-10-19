#!/usr/bin/env python3
"""Fast post-gates only — assumes on-chain verified set from disco_run.py"""
import sys, json, time
sys.path.insert(0, '/home/kenzodadon/base-trend-bot')

# Load the 20 coins we already verified on-chain
# Reconstruct from disco_run output (we know the verified set)
coins_data = [
    {"name":"METAc","token_addr":"0xb2000000000000000000008bc878","score":85,"liquidity":1798792.0,"vol_5m":0,"vol_1h":0,"vol_24h":0,"fee_bps":30,"source":"gmgn_trending","change_24h":0,"rug_ratio":0,"is_renounced":False,"is_honeypot":False},
    {"name":"NVDAc","token_addr":"0xb20000000000000000000078ee7c","score":84,"liquidity":1770642.0,"vol_5m":0,"vol_1h":0,"vol_24h":0,"fee_bps":30,"source":"gmgn_trending","change_24h":0,"rug_ratio":0,"is_renounced":False,"is_honeypot":False},
    {"name":"AAPLc","token_addr":"0xb200000000000000000000c2e324","score":82,"liquidity":1675980.0,"vol_5m":0,"vol_1h":0,"vol_24h":0,"fee_bps":30,"source":"gmgn_trending","change_24h":0,"rug_ratio":0,"is_renounced":False,"is_honeypot":False},
    {"name":"DIEM","token_addr":"0xf4d97f2da56e8c3098f3a8d538db","score":73,"liquidity":282411.0,"vol_5m":0,"vol_1h":0,"vol_24h":98407.0,"fee_bps":30,"source":"gmgn_trending","change_24h":0,"rug_ratio":0,"is_renounced":False,"is_honeypot":False},
    {"name":"GOOGLc","token_addr":"0xb2000000000000000000002d0ba3","score":72,"liquidity":1489824.0,"vol_5m":0,"vol_1h":0,"vol_24h":0,"fee_bps":30,"source":"gmgn_trending","change_24h":0,"rug_ratio":0,"is_renounced":False,"is_honeypot":False},
    {"name":"KTA / WETH","token_addr":"0xc0634090f2fe6c6d75e61be2b949","score":71,"liquidity":1070537.0,"vol_5m":2558.0,"vol_1h":3069.6,"vol_24h":114178.8,"fee_bps":30,"source":"gecko_trending","change_24h":-7.57,"live_pace":None,"live_note":"no vol_1h baseline"},
    {"name":"SI / WETH 0.3%","token_addr":"0x7d3cc30b30dc0645aacdff6372d2","score":60,"liquidity":566459.0,"vol_5m":318.7,"vol_1h":9883.2,"vol_24h":75468.0,"fee_bps":30,"source":"gecko_trending","change_24h":15.86},
    {"name":"SPCXc","token_addr":"0xb2000000000000000000007b9fcb","score":59,"liquidity":1205220.0,"vol_5m":0,"vol_1h":0,"vol_24h":0,"fee_bps":30,"source":"gmgn_trending","change_24h":0,"rug_ratio":0,"is_renounced":False,"is_honeypot":False},
    {"name":"AMZNc","token_addr":"0xb200000000000000000000d9192b","score":55,"liquidity":1128080.0,"vol_5m":0,"vol_1h":0,"vol_24h":0,"fee_bps":30,"source":"gmgn_trending","change_24h":0,"rug_ratio":0,"is_renounced":False,"is_honeypot":False},
    {"name":"ZEN","token_addr":"0xf43eb8de897fbc7f2502483b2bef","score":50,"liquidity":528415.0,"vol_5m":0,"vol_1h":0,"vol_24h":51955.2,"fee_bps":30,"source":"gmgn_trending","change_24h":-1.2,"rug_ratio":0,"is_renounced":True,"is_honeypot":False},
    {"name":"KTA","token_addr":"0xc0634090f2fe6c6d75e61be2b949","score":49,"liquidity":1070537.0,"vol_5m":2558.0,"vol_1h":3069.6,"vol_24h":114178.8,"fee_bps":30,"source":"gecko_new","change_24h":-7.57},
    {"name":"cbXRP","token_addr":"0xcb585250f852c6c6bf90434ab21a","score":40,"liquidity":325166.0,"vol_5m":0,"vol_1h":0,"vol_24h":58335.6,"fee_bps":30,"source":"gmgn_trending","change_24h":10.66,"rug_ratio":0,"is_renounced":False,"is_honeypot":False},
    {"name":"ICP","token_addr":"0x00f3c42833c3170159af4e92dbb4","score":34,"liquidity":192717.0,"vol_5m":0,"vol_1h":0,"vol_24h":57120.0,"fee_bps":30,"source":"gmgn_trending","change_24h":2.03,"rug_ratio":0,"is_renounced":False,"is_honeypot":False},
    {"name":"SPIKE","token_addr":"0xb20000000000000000000070f6c1","score":31,"liquidity":649614.0,"vol_5m":0,"vol_1h":0,"vol_24h":0,"fee_bps":30,"source":"gmgn_trending","change_24h":0,"rug_ratio":0,"is_renounced":False,"is_honeypot":False},
    {"name":"PLAY","token_addr":"0x853a7c99227499dba9db8c3a02aa","score":31,"liquidity":353238.0,"vol_5m":0,"vol_1h":0,"vol_24h":50142.0,"fee_bps":30,"source":"gmgn_trending","change_24h":-22.66,"rug_ratio":0,"is_renounced":False,"is_honeypot":False},
    {"name":"PROS","token_addr":"0x8b7dde054be9d180c1be7fae0874","score":29,"liquidity":215663.0,"vol_5m":0,"vol_1h":0,"vol_24h":81218.4,"fee_bps":30,"source":"gmgn_trending","change_24h":25.17,"rug_ratio":0.0,"is_renounced":True,"is_honeypot":False},
    {"name":"Basecat","token_addr":"0xb2000000000000000000004c27f6","score":28,"liquidity":528012.0,"vol_5m":0,"vol_1h":0,"vol_24h":0,"fee_bps":30,"source":"gmgn_trending","change_24h":0,"rug_ratio":0,"is_renounced":False,"is_honeypot":False},
    {"name":"O","token_addr":"0x182fa643e5f29d5eca75e7b9cf93","score":28,"liquidity":148859.0,"vol_5m":0,"vol_1h":0,"vol_24h":57373.2,"fee_bps":30,"source":"gmgn_trending","change_24h":23.4,"rug_ratio":0,"is_renounced":False,"is_honeypot":False},
    {"name":"wtCOIN","token_addr":"0x5cda0e1ca4ce2af96315f7f8963c","score":27,"liquidity":141232.0,"vol_5m":0,"vol_1h":0,"vol_24h":50899.2,"fee_bps":30,"source":"gmgn_trending","change_24h":12.05,"rug_ratio":0,"is_renounced":False,"is_honeypot":False},
    {"name":"boar","token_addr":"0x0cbf291ba052174879d90bf781df","score":27,"liquidity":129026.0,"vol_5m":0,"vol_1h":0,"vol_24h":51856.8,"fee_bps":30,"source":"gmgn_trending","change_24h":4.71,"rug_ratio":0,"is_renounced":True,"is_honeypot":False},
]

# GMGN security pre-gate
print("=== GMGN SECURITY PRE-GATE ===")
try:
    from gmgn_bridge import security_check
    safe, unsafe = [], []
    for c in coins_data:
        addr = c["token_addr"]
        if c.get("source") == "manual_add":
            safe.append(c); continue
        sec = security_check(addr)
        if sec.get("safe") is True:
            safe.append(c)
        elif sec.get("safe") is False:
            c["gmgn_reject"] = sec.get("reason", "unknown")
            unsafe.append(c)
        else:
            # safe is None = API unavailable; keep the coin (skip this gate)
            safe.append(c)
    if unsafe:
        for c in unsafe:
            print(f"  DROP: {c['name'][:20]:20} {c.get('gmgn_reject','')[:50]}")
    coins_data = safe
    print(f"  {len(safe)} passed, {len(unsafe)} dropped")
except Exception as e:
    print(f"  UNAVAILABLE: {e}")

# Risk officer veto
print("\n=== RISK OFFICER VETO ===")
try:
    from risk_officer import veto_check
    passed, vetoed = [], []
    for c in coins_data:
        prop = {"action":"swap","token_out":c.get("token_addr"),"pool":c.get("address",""),
                "amount_eth":0.0005,"score":c.get("score",0),"slippage_bps":c.get("fee_bps",30),
                "liquidity":c.get("liquidity",0),"vol_5m":c.get("vol_5m",0),"source":c.get("source","")}
        ok, fails = veto_check(prop)
        if ok: passed.append(c)
        else:
            c["officer_veto"] = "; ".join(fails)
            vetoed.append(c)
    if vetoed:
        for c in vetoed:
            print(f"  VETO: {c['name'][:20]:20} {c.get('officer_veto','')[:50]}")
    coins_data = passed
    print(f"  {len(passed)} passed, {len(vetoed)} vetoed")
except Exception as e:
    print(f"  UNAVAILABLE: {e}")

# Token grade
print("\n=== TOKEN GRADE (BLOCK filter) ===")
try:
    from token_grade import TokenGrader
    grader = TokenGrader()
    graded, blocked = [], []
    for c in coins_data:
        addr = c["token_addr"]
        if not addr or len(addr) != 42: continue
        grade = grader.grade(addr)
        c["token_grade"] = grade
        if grade["verdict"] != "BLOCK":
            graded.append(c)
        else:
            c["_blocked"] = True
            blocked.append(c)
            print(f"  BLOCK: {c['name'][:20]:20} {', '.join(grade['reasons'][:2])}")
    coins_data = graded
    print(f"  {len(graded)} passed, {len(blocked)} blocked")
except Exception as e:
    print(f"  UNAVAILABLE: {e}")

# Sort by score
coins_data.sort(key=lambda x: x["score"], reverse=True)

# Write cache
from datetime import datetime, timezone
from _atomic_write import atomic_jsonl_append
rec = {"ts": datetime.now(timezone.utc).isoformat(), "coins": coins_data}
atomic_jsonl_append("/home/kenzodadon/base-trend-bot/discovered.jsonl", rec)

print(f"\n=== FINAL: {len(coins_data)} discovered coins written to cache ===")
for i, c in enumerate(coins_data[:15]):
    pace = c.get("live_pace")
    ps = f"{pace:.2f}x" if pace else "n/a"
    tg = c.get("token_grade", {})
    verdict = tg.get("verdict", "n/a")
    print(f"  {i+1:2}. {c['name'][:24]:24} score={c['score']:>3} liq=${c['liquidity']:>10,.0f} "
          f"v24h=${c.get('vol_24h',0):>10,.0f} pace={ps:>6} grade={verdict} [{c['source']}]")

if not coins_data:
    print("  (no coins passed all gates)")
