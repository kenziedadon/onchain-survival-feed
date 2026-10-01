#!/usr/bin/env python3
"""Nous Sentiment Gate — run with: python3 nous_sentiment_gate_now.py"""
import urllib.request, json, time, os, sys
from datetime import datetime, timezone

GECKO = 'https://api.geckoterminal.com/api/v2/networks/base'
NOUS_URL = 'https://inference-api.nousresearch.com/v1/chat/completions'
NOUS_MODEL = 'upstage/solar-pro4:free'
OUT = '/home/kenzodadon/base-trend-bot/nous_signals.json'
AUTH = '/home/kenzodadon/.hermes/shared/nous_auth.json'

def log(*a):
    print(' '.join(str(x) for x in a), flush=True)

def gecko(path, tries=5):
    url = f'{GECKO}/{path}'
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except Exception as e:
            log(f'  gecko {path[:40]}... try{i+1}: {e}')
            time.sleep(5 + i*4)
    return None

def ema(prices, period):
    if len(prices) < period: return None
    k = 2.0/(period+1); e = prices[0]
    for p in prices[1:]: e = p*k + e*(1-k)
    return e

def cross(closes):
    if len(closes) < 24: return False, None, None
    px = [float(c) for c in closes[-24:]]
    if len(px) < 22: return False, None, None
    f = [ema(px[:i+1],9) for i in range(len(px))]
    s = [ema(px[:i+1],21) for i in range(len(px))]
    v = [(a,b) for a,b in zip(f,s) if a and b]
    if len(v)<3: return False,None,None
    pf,ps = v[-2]; cf,cs = v[-1]
    return pf<=ps and cf>cs, cf, cs

# Auth
with open(AUTH) as f: token = json.load(f)['access_token']

# Step 1: pools
log('=== 1. Trending pools ===')
d = gecko('trending_pools?page=1')
if not d: log('FAIL pools'); sys.exit(1)
pools = d.get('data',[])
log(f'{len(pools)} pools fetched')

cands = []
for p in pools:
    a = p['attributes']
    res = float(a.get('reserve_in_usd',0) or 0)
    if res <= 100000: continue
    rel = p.get('relationships',{})
    qtid = rel.get('quote_token',{}).get('data',{}).get('id','')
    nm = a.get('name','')
    if 'WETH' not in qtid and 'WETH' not in nm.upper(): continue
    bid = rel.get('base_token',{}).get('data',{}).get('id','')
    tid = bid.replace('base_','') if bid.startswith('base_') else bid
    cands.append({'pa':a['address'],'ta':tid,'nm':nm,'liq':res,
        'v1h':float(a.get('volume_usd',{}).get('h1',0) or 0),
        'v24':float(a.get('volume_usd',{}).get('h24',0) or 0),
        'chg':a.get('price_change_percentage',{}).get('h24','0'),
        'b5':a.get('transactions',{}).get('m5',{}).get('buys',0),
        's5':a.get('transactions',{}).get('m5',{}).get('sells',0)})
log(f'{len(cands)} WETH candidates >$100k')

# Step 2: OHLCV
log('\n=== 2. OHLCV + crossover ===')
for i,c in enumerate(cands):
    log(f'[{i+1}/{len(cands)}] {c["nm"][:20]}')
    od = gecko(f'pools/{c["pa"]}/ohlcv/hour?aggregate=1&limit=120')
    closes = []
    if od:
        # The response is {'data': {'attributes': {'ohlcv_list': [...]}}}
        da = od.get('data', {})
        if isinstance(da, dict):
            oa = da.get('attributes', {}).get('ohlcv_list', [])
        elif isinstance(da, list) and da:
            oa = da[0].get('attributes', {}).get('ohlcv_list', [])
        else:
            oa = []
        closes = [o[4] for o in oa if len(o) >= 5] if oa else []
    x,f,s = cross(closes)
    c['cross']=x; c['ema9']=round(f,6) if f else None; c['ema21']=round(s,6) if s else None
    log(f'  {len(closes)}c | {"CROSS ✅" if x else f"no ({f}, {s})"}')
    time.sleep(1.5)

# Step 3: Nous
log('\n=== 3. Nous scoring ===')
signals=[]; alerts=[]
for c in cands:
    if not c.get('cross'):
        c['score']=0; c['dec']='NO_GO'; c['reason']='No crossover'
        signals.append(c); continue
    prt = f"""Score 0-100, BUY or NO_GO. JSON ONLY: {{"score":N,"decision":"BUY"|"NO_GO","reason":"..."}}
TOKEN: {c['nm']}
POOL: {c['pa'][:20]}... | LIQ: ${c['liq']:,.0f}
VOL1H: ${c['v1h']:,.0f} | VOL24H: ${c['v24']:,.0f} | 24H%: {c['chg']}%
5M: {c['b5']}B/{c['s5']}S | EMA: BULLISH CROSSOVER"""

    payload={'model':NOUS_MODEL,'messages':[{'role':'user','content':prt}],'max_tokens':120,'temperature':0.3}
    req=urllib.request.Request(NOUS_URL,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','Authorization':f'Bearer {token}'})
    try:
        with urllib.request.urlopen(req,timeout=20) as resp:
            rd=json.loads(resp.read())
        txt=rd['choices'][0]['message']['content']
        s=txt.find('{');e=txt.rfind('}')+1
        r=json.loads(txt[s:e]) if s>=0 and e>s else {'score':0,'decision':'NO_GO','reason':'fail'}
        c['score']=r.get('score',0); c['dec']=r.get('decision','NO_GO'); c['reason']=r.get('reason','N/A')
        al = ' 🟢' if (c['score']>=80 and c['dec']=='BUY') else ''
        log(f'  {c["nm"][:18]:18s} {c["score"]:3d} {c["dec"]:5s} {c["reason"][:40]}{al}')
        if c['score']>=80 and c['dec']=='BUY': alerts.append(c)
    except Exception as e:
        log(f'  Nous FAIL: {e}')
        c['score']=0; c['dec']='NO_GO'; c['reason']=str(e)
    signals.append(c)
    time.sleep(0.8)

# Write
out={'ts':datetime.now(timezone.utc).isoformat(),'scanned':len(cands),
     'crosses':sum(1 for c in cands if c.get('cross')),'signals':signals,
     'alerts':[{'name':a['nm'],'addr':a['ta'],'pool':a['pa'],'score':a['score'],
                'reason':a['reason'],'liq':a['liq']} for a in alerts]}
os.makedirs(os.path.dirname(OUT),exist_ok=True)
with open(OUT,'w') as f: json.dump(out,f,indent=2)

log(f'\n{"="*55}')
log(f'NOUS GATE | {datetime.now(timezone.utc).strftime("%H:%M UTC")}')
log(f'Scanned:{len(cands)} Crosses:{out["crosses"]} Alerts:{len(alerts)}')
log(f'{"="*55}')
if alerts:
    log('\n🔴 ALERTS:')
    for a in alerts: log(f'  {a["name"]} score={a["score"]} | {a["reason"]} | ${a["liq"]:,.0f}')
else:
    log('\nNo alerts. Scored tokens:')
    for s in signals:
        log(f'  {s["nm"][:20]:20s} cross={"Y" if s.get("cross") else "N"} score={s.get("score",0)} {s.get("dec","?")}')
log(f'\n-> {OUT}')
