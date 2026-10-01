#!/usr/bin/env python3
"""Phase A: OHLCV + EMA for ETH-quoted trending candidates."""
import urllib.request, json, time, os, sys

AUTH = "eyJhbG...7bNg"
NOUS = "https://inference-api.nousresearch.com/v1"
OUT = "/home/kenzodadon/base-trend-bot/nous_signals.json"
os.makedirs("/home/kenzodadon/base-trend-bot", exist_ok=True)

def get(url, tries=4):
    for a in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36',
                'Accept': 'application/json',
            })
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            w = 5*(a+1)+2
            print(f"  HTTP {e.code} wait {w}s", flush=True)
            if e.code == 429:
                time.sleep(w); continue
            return None
        except Exception as e:
            print(f"  Err: {e}", flush=True); return None
    return None

def closes_from(resp):
    d = resp.get("data", {})
    if isinstance(d, list): return None
    attrs = d.get("attributes", {})
    rows = attrs.get("ohlcv_list", [])
    cs = []
    for row in rows:
        if isinstance(row, (list,tuple)) and len(row) >= 5:
            try: cs.append(float(row[4]))
            except: pass
    cs.reverse()
    return cs

def ema(cs, p):
    if len(cs) < p: return None
    s = sum(cs[:p])/p; e = s; k = 2.0/(p+1)
    for i in range(p, len(cs)): e = (cs[i]-e)*k+e
    return e

def main():
    # Step 1: pools
    print("=== POOLS ===", flush=True)
    td = get("https://api.geckoterminal.com/api/v2/networks/base/trending_pools?page=1")
    time.sleep(1.5)
    if not td: print("FAIL"); sys.exit(1)
    pr = td.get("data", []) if isinstance(td, dict) else td
    pools = pr if isinstance(pr, list) else []
    print(f"{len(pools)} pools", flush=True)

    cands = []
    for p in pools:
        if not isinstance(p, dict): continue
        a = p.get("attributes", {})
        addr = a.get("address","")
        name = a.get("name","")
        try: liq = float(a.get("reserve_in_usd","0"))
        except: liq = 0
        parts = name.split(" / ")
        bs = parts[0].strip() if len(parts)>=1 else "?"
        qs = parts[1].strip() if len(parts)>=2 else "?"
        v = a.get("volume_usd",{})
        pc = a.get("price_change_percentage",{})
        try:
            v5=float(v.get("m5","0") or 0); v1=float(v.get("h1","0") or 0); v24=float(v.get("h24","0") or 0)
            pc24=float(pc.get("h24","0") or 0)
        except: v5=v1=v24=pc24=0
        if liq>100000 and qs.upper() in ("ETH","WETH"):
            cands.append({"address":addr,"base_symbol":bs,"quote_symbol":qs,"liquidity_usd":liq,
                           "volume_5m":v5,"volume_1h":v1,"volume_24h":v24,"price_change_24h":pc24})
            print(f"  {bs}: liq=${liq:,.0f} v1h=${v1:,.0f} chg={pc24:.2f}%", flush=True)

    print(f"\n{len(cands)} candidates", flush=True)

    # Step 2: OHLCV + EMA
    print("\n=== OHLCV+EMA ===", flush=True)
    res = []
    for i,c in enumerate(cands):
        addr=c["address"]; sym=c["base_symbol"]
        print(f"[{i+1}/{len(cands)}] {sym}", flush=True)
        o = get(f"https://api.geckoterminal.com/api/v2/networks/base/pools/{addr}/ohlcv/hour?aggregate=1&limit=120")
        time.sleep(1.5)
        if not o: print("  FAIL", flush=True); res.append({**c,"ohlcv_pts":0,"ema9_prev":None,"ema9":None,"ema21_prev":None,"ema21":None,"cross":False,"err":"fetch"}); continue
        cs = closes_from(o)
        if not cs: print("  NO CLOSES", flush=True); res.append({**c,"ohlcv_pts":0,"ema9_prev":None,"ema9":None,"ema21_prev":None,"ema21":None,"cross":False,"err":"no_closes"}); continue
        print(f"  pts={len(cs)} {cs[0]:.6f}->{cs[-1]:.6f}", flush=True)
        if len(cs)<23: print("  too few", flush=True); res.append({**c,"ohlcv_pts":len(cs),"ema9_prev":None,"ema9":None,"ema21_prev":None,"ema21":None,"cross":False,"err":"few"}); continue
        e9=ema(cs,9); e21=ema(cs,21)
        if not e9 or not e21: print("  ema fail", flush=True); res.append({**c,"ohlcv_pts":len(cs),"ema9_prev":None,"ema9":None,"ema21_prev":None,"ema21":None,"cross":False,"err":"ema"}); continue
        e9p=e9[-2]; e9c=e9[-1]; e21p=e21[-2]; e21c=e21[-1]
        cross=(e9p<=e21p) and (e9c>e21c)
        print(f"  EMA9:{e9p:.6f}->{e9c:.6f} EMA21:{e21p:.6f}->{e21c:.6f} CROSS={cross}", flush=True)
        res.append({**c,"ohlcv_pts":len(cs),"ema9_prev":e9p,"ema9":e9c,"ema21_prev":e21p,"ema21":e21c,"cross":cross,"err":None})

    crosses = [r for r in res if r["cross"]]
    print(f"\nCrossovers: {len(crosses)}", flush=True)
    for r in crosses: print(f"  {r['base_symbol']}", flush=True)

    # Save intermediate
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    partial = {"timestamp": ts, "scan_summary": {"total_pools":len(pools),"eth_quoted_candidates":len(cands),"crossovers":len(crosses)}, "signals": res}
    with open(OUT, "w") as f: json.dump(partial, f, indent=2)
    print(f"\nSaved partial: {OUT}", flush=True)

    # Print compact summary
    sm = {"timestamp": ts, "tokens": []}
    for r in res:
        sm["tokens"].append({"symbol":r["base_symbol"],"addr":r["address"][:20]+"...,"liq":r["liquidity_usd"],"v1h":r["volume_1h"],"chg24h":r["price_change_24h"],"cross":r["cross"],"ema9":round(r["ema9"],6) if r.get("ema9") else None,"ema21":round(r["ema21"],6) if r.get("ema21") else None,"score":r.get("nous_score"),"decision":r.get("nous_decision"),"reason":r.get("nous_reason")})
    print(json.dumps(sm, indent=2), flush=True)

if __name__ == "__main__":
    main()
