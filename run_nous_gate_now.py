#!/usr/bin/env python3
"""
Nous sentiment gate — resumable, rate-limit aware.
Pulls base/quote addresses from relationships (not attributes).
"""
import urllib.request, json, time, os, sys
from datetime import datetime, timezone

HOME = '/home/kenzodadon'
BOT = os.path.join(HOME, 'base-trend-bot')
STATE = os.path.join(BOT, '.nous_state.json')
OUT = os.path.join(BOT, 'nous_signals.json')

WETH = '0xC02AAa39b223FE8D0A0e5c4F27eAD9083C756Cc2'
GT = 'https://api.geckoterminal.com/api/v2/networks/base'
NOUS_MODEL = 'upstage/solar-pro4:free'
RATE = 2.0

def fetch(url, timeout=30, retries=3):
    last = None
    for attempt in range(1, retries+1):
        try:
            req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            last = f'HTTP {e.code}'
            if e.code == 429:
                wait = 15 * attempt
                print(f'    429, backing off {wait}s (attempt {attempt}/{retries})')
                time.sleep(wait)
                continue
            raise
        except Exception as e:
            last = str(e)
            if attempt < retries:
                time.sleep(5)
                continue
            raise
    raise RuntimeError(f'failed after {retries} retries: {last}')

def ema(series, period):
    out = []
    k = 2.0/(period+1)
    if len(series) < period:
        seed = sum(series)/len(series)
        return [seed]*len(series)
    sma = sum(series[:period])/period
    out.append(sma)
    for i in range(period, len(series)):
        out.append(series[i]*k + out[-1]*(1-k))
    while len(out) < len(series):
        out.insert(0, out[0])
    return out

def rel_id(rel):
    """Extract address from a relationships.*.data object."""
    if isinstance(rel, dict):
        data = rel.get('data')
        if isinstance(data, dict):
            rid = data.get('id', '')
        else:
            rid = str(data)
    else:
        rid = str(rel)
    return rid.replace('base_','') if rid.startswith('base_') else rid

def load_state():
    if os.path.exists(STATE):
        with open(STATE) as f:
            return json.load(f)
    return {'stage': 0, 'pools': [], 'cands': [], 'ohlcv_done': [], 'crossovers': [], 'scored': [], 'errors': []}

def save_state(st):
    with open(STATE,'w') as f:
        json.dump(st, f, indent=2)

def main():
    st = load_state()
    timestamp = datetime.now(timezone.utc).isoformat()
    print(f'[GATE] start {timestamp} | resume stage {st["stage"]}')

    # ── STAGE 1: trending pools ──
    if st['stage'] < 1:
        print('[1/6] trending pools')
        try:
            raw = fetch(f'{GT}/trending_pools?page=1')
            data = raw.get('data', [])
            st['pools'] = []
            for p in data:
                rels = p.get('relationships', {}) or {}
                base_id = rel_id(rels.get('base_token'))
                quote_id = rel_id(rels.get('quote_token'))
                st['pools'].append({
                    'pool_id': p['id'].replace('base_',''),
                    'name': p['attributes'].get('name','?'),
                    'reserve_usd': float(p['attributes'].get('reserve_in_usd', 0)),
                    'base_id': base_id,
                    'quote_id': quote_id,
                    'price_change_h24': p['attributes'].get('price_change_percentage',{}).get('h24',0),
                    'volume_m5': p['attributes'].get('volume_usd',{}).get('m5',0),
                    'volume_h1': p['attributes'].get('volume_usd',{}).get('h1',0),
                })
            st['stage'] = 1
            save_state(st)
            print(f'  {len(st["pools"])} pools fetched')
            for p in st['pools']:
                print(f'    {p["pool_id"][:20]}... {p["name"][:35]} liq=${p["reserve_usd"]:,.0f} base={p["base_id"][:12]} quote={p["quote_id"][:12]}')
        except Exception as e:
            st['errors'].append({'stage':1, 'error': str(e)})
            save_state(st); print(f'  FAIL: {e}'); return

    # ── STAGE 2: filter WETH >$100k ──
    if st['stage'] < 2:
        print('[2/6] filter WETH pairs >$100k')
        cands = [p for p in st['pools']
                 if p['reserve_usd'] > 100_000
                 and (p['base_id'].upper()==WETH.upper() or p['quote_id'].upper()==WETH.upper())]
        st['cands'] = cands
        st['stage'] = 2
        save_state(st)
        print(f'  {len(cands)} candidates')
        if not cands:
            print('  (none)')
        for c in cands:
            print(f'    {c["pool_id"][:20]}... {c["name"]} liq=${c["reserve_usd"]:,.0f} base={c["base_id"][:12]} quote={c["quote_id"][:12]}')

    if not st['cands']:
        print('  no WETH candidates >$100k — writing empty report')
        out = {'timestamp': timestamp, 'pools_scanned': len(st['pools']),
               'weth_candidates': 0, 'crossovers_found': 0, 'signals': [], 'high_conviction_buys': []}
        with open(OUT,'w') as f: json.dump(out, f, indent=2)
        print(f'  wrote {OUT}'); return

    # ── STAGE 3: OHLCV + EMA ──
    if st['stage'] < 3:
        print(f'[3/6] OHLCV+EMA for {len(st["cands"])} candidates')
        done = st.get('ohlcv_done', [])
        for idx, c in enumerate(st['cands']):
            if any(o['pool_id']==c['pool_id'] for o in done):
                print(f'  [{idx+1}/{len(st["cands"])}] {c["pool_id"][:20]}... (cached)')
                continue
            print(f'  [{idx+1}/{len(st["cands"])}] {c["pool_id"][:20]}... ', end='', flush=True)
            try:
                oc = fetch(f'{GT}/pools/{c["pool_id"]}/ohlcv/hour?aggregate=1&limit=120')
                closes = []
                for item in oc.get('data',[]):
                    v = item.get('attributes',{}).get('close')
                    if v is not None:
                        try: closes.append(float(v))
                        except: pass
                closes.reverse()
                if len(closes) < 21:
                    print(f'skip ({len(closes)} bars)')
                    time.sleep(RATE); continue
                e9 = ema(closes, 9)
                e21 = ema(closes, 21)
                pf, cf = e9[-2], e9[-1]
                ps, cs = e21[-2], e21[-1]
                cross = (pf <= ps) and (cf > cs)
                entry = {**c, 'ohlcv_bars': len(closes),
                         'ema9_prev': pf, 'ema9_curr': cf,
                         'ema21_prev': ps, 'ema21_curr': cs,
                         'crossover': bool(cross)}
                done.append(entry)
                print(f'OK bars={len(closes)} e9={cf:.6f} e21={cs:.6f} cross={cross}')
                time.sleep(RATE)
            except Exception as e:
                print(f'FAIL: {e}')
                st['errors'].append({'stage':3, 'pool': c['pool_id'], 'error': str(e)})
                time.sleep(RATE)
        st['ohlcv_done'] = done
        st['crossovers'] = [o for o in done if o['crossover']]
        st['stage'] = 3
        save_state(st)
        print(f'  done: {len(done)} OHLCV, {len(st["crossovers"])} crossovers')

    # ── STAGE 4: Nous scoring ──
    if st['stage'] < 4:
        print('[4/6] loading Nous auth')
        auth_path = os.path.join(HOME, '.hermes/shared/nous_auth.json')
        try:
            with open(auth_path) as f:
                auth = json.load(f)
            token = auth['access_token']
            nb = auth.get('inference_base_url','https://inference-api.nousresearch.com/v1')
            if not nb.endswith('/v1'): nb = nb.rstrip('/')+'/v1'
            nous_url = nb + '/chat/completions'
            print(f'  endpoint={nous_url}')
        except Exception as e:
            st['errors'].append({'stage':4, 'error': f'auth: {e}'})
            save_state(st); print(f'  FAIL: {e}'); return

        print(f'[4/6] scoring {len(st["crossovers"])} crossovers via Nous')
        scored = st.get('scored', [])
        for idx, c in enumerate(st['crossovers']):
            if any(s['pool_id']==c['pool_id'] for s in scored):
                print(f'  [N{idx+1}/{len(st["crossovers"])}] {c["pool_id"][:20]}... (cached)')
                continue
            print(f'  [N{idx+1}/{len(st["crossovers"])}] {c["pool_id"][:20]}... ', end='', flush=True)
            body = {
                'model': NOUS_MODEL,
                'messages': [{
                    'role': 'user',
                    'content': (
                        'You are a crypto sentiment analyst for Base-chain WETH-paired pools. '
                        'Return ONLY a JSON object with keys: "score" (int 0-100), '
                        '"decision" ("BUY" or "NO_GO"), "reason" (string).\n\n'
                        f'Token: {c["pool_id"]}\n'
                        f'Name: {c["name"]}\n'
                        f'Liquidity USD: ${c["reserve_usd"]:,.0f}\n'
                        f'Volume 5m: ${float(c["volume_m5"]):,.0f}\n'
                        f'Volume 1h: ${float(c["volume_h1"]):,.0f}\n'
                        f'Price change 24h: {c["price_change_h24"]}%\n'
                        f'EMA(9): {c["ema9_curr"]:.6f} (prev {c["ema9_prev"]:.6f})\n'
                        f'EMA(21): {c["ema21_curr"]:.6f} (prev {c["ema21_prev"]:.6f})\n'
                        f'EMA bullish crossover: YES\n\n'
                        'Evaluate: liquidity depth, volume momentum, price trend, crossover freshness. '
                        'High-conviction BUY (80+) needs: liquidity >$500k, volume accelerating, '
                        'price not deeply crashed, crossover just triggered. Be conservative. '
                        'Return ONLY the JSON object.'
                    )
                }],
                'max_tokens': 200,
                'temperature': 0.2,
                'response_format': {'type': 'json_object'}
            }
            try:
                req = urllib.request.Request(nous_url, data=json.dumps(body).encode(),
                    headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}, method='POST')
                with urllib.request.urlopen(req, timeout=60) as resp:
                    rd = json.loads(resp.read())
                msg = rd['choices'][0]['message']['content']
                sc = json.loads(msg)
                c['nous_score'] = sc.get('score')
                c['nous_decision'] = sc.get('decision')
                c['nous_reason'] = sc.get('reason','')
                scored.append(c)
                print(f'score={sc.get("score")} {sc.get("decision")}')
            except Exception as e:
                print(f'FAIL: {e}')
                c['nous_error'] = str(e)
                scored.append(c)
            time.sleep(RATE)
        st['scored'] = scored
        st['stage'] = 4
        save_state(st)
        print(f'  scored {len(scored)}/{len(st["crossovers"])}')

    # ── STAGE 5: write output ──
    print('[5/6] writing output')
    high = [s for s in st.get('scored',[]) if s.get('nous_decision')=='BUY' and s.get('nous_score',0)>=80]
    out = {
        'timestamp': timestamp,
        'pools_scanned': len(st.get('pools',[])),
        'weth_pairs_scanned': len(st.get('cands',[])),
        'crossovers_found': len(st.get('crossovers',[])),
        'signals_scored': len(st.get('scored',[])),
        'signals': st.get('scored',[]),
        'high_conviction_buys': high,
        'errors': st.get('errors',[]),
    }
    with open(OUT,'w') as f:
        json.dump(out, f, indent=2)
    print(f'  wrote {OUT} ({os.path.getsize(OUT)} bytes)')

    # ── STAGE 6: report ──
    print('\n[6/6] === RESULT ===')
    print(f'  trending pools scanned: {out["pools_scanned"]}')
    print(f'  WETH pairs >$100k: {out["weth_pairs_scanned"]}')
    print(f'  EMA crossovers: {out["crossovers_found"]}')
    print(f'  Nous-scored: {out["signals_scored"]}')
    print(f'  HIGH-CONVICTION BUY (>=80): {len(high)}')
    if high:
        print('\n  *** ALERT — BULLISH SIGNALS ***')
        for s in high:
            print(f'    {s["pool_id"]} ({s["name"]})')
            print(f'      score={s["nous_score"]} decision={s["nous_decision"]}')
            print(f'      reason: {s["nous_reason"]}')
            print(f'      liq=${s["reserve_usd"]:,.0f} vol1h=${float(s["volume_h1"]):,.0f} h24={s["price_change_h24"]}%')
    else:
        if out['crossovers_found'] == 0:
            print('  No EMA crossovers detected this cycle.')
        else:
            print('  Crossovers scored below threshold:')
            for s in out['signals']:
                print(f'    {s["pool_id"][:20]}... {s["name"]} score={s.get("nous_score")} {s.get("nous_decision","ERR")}')
    print('\n[DONE]')
    if os.path.exists(STATE):
        os.remove(STATE)

if __name__ == '__main__':
    main()
