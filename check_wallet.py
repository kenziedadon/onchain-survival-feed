import sys, json, urllib.request

WALLET = "0xe7a16Be2aF1eCE8922d9d3aD55C03e29004e540F"
RPCS = ["https://mainnet.base.org", "https://base.llamarpc.com", "https://base.meowrpc.com"]

def _http_post(url, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())

def rpc_call(method, params=None):
    for ep in RPCS:
        try:
            payload = {"jsonrpc": "2.0", "id": 1, "method": method}
            if params: payload["params"] = params
            return _http_post(ep, payload)
        except Exception:
            continue
    return {"error": "all RPCs failed"}

def get_token_info(addr):
    for ep in RPCS:
        try:
            b = '0x70a08231' + WALLET.lower()[2:].rjust(64,'0')
            r1 = rpc_call('eth_call', [{'to':addr,'data':b},'latest'])
            if 'result' not in r1: continue
            raw = int(r1['result'],16)
            d = '0x313ce567'
            r2 = rpc_call('eth_call', [{'to':addr,'data':d},'latest'])
            dec = int(r2.get('result','0x0'),16) if 'result' in r2 else 18
            s = '0x95d89b41'
            r3 = rpc_call('eth_call', [{'to':addr,'data':s},'latest'])
            sym = ''
            if 'result' in r3 and r3['result']!='0x':
                try: sym = bytes.fromhex(r3['result'][2:]).decode('utf-8','ignore').rstrip(chr(0))
                except: pass
            return {'raw':raw,'dec':dec,'sym':sym,'val':raw/(10**dec)}
        except: continue
    return None

print('Wallet:', WALLET)
print('=' * 60)

# ETH
for ep in RPCS:
    try:
        res = rpc_call('eth_getBalance', [WALLET, 'latest'])
        if 'result' in res:
            eth = int(res['result'], 16) / 1e18
            print(f'ETH: {eth:.8f} ETH (about ${eth*2540:.2f})')
            break
    except: pass

print()

tokens = {
    'NOCK':     '0x9b5e262cf9bb04869ab40b19af91d2dc85761722',
    'Basecat':  '0xB2000000000000000000004c27f6523082f41D01',
    'STONKFLY': '0xB20000000000000000000074deAC544E1E9a1f33',
    'MUTATIO':  '0x8b67f2e56139ca052a7ec49cbcd1aa9c83f2752a',
    'BRETT':    '0x4e829f8a5213c42535ab84aa40bd4adcce9cba02',
    'LAPTOP':   '0xb095274743941e953c746f9c228da9c18bb6ec29',
    'MSTRc':    '0xb2000000000000000000004884b426556b92883d',
    'KEYCAT':   '0x9a26F5433671751c3276a065f57e5a02d2817973',
    'MIGGLES':  '0xB1a03EdA8ba3d05a8c3568477c7595217600c491',
    'MORPHO':   '0x653143539872a2907b4a877Ee399Ca0C1B3F7558',
    'USDC':     '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913',
    'WETH':     '0x4200000000000000000000000000000000000006',
}

for name, addr in tokens.items():
    info = get_token_info(addr)
    if info and info['val'] > 0:
        print(f'{name:12s}: {info["val"]:>20.8f} units (dec={info["dec"]}, sym={info["sym"]})')
    elif info:
        print(f'{name:12s}: 0 units')
    else:
        print(f'{name:12s}: ERROR reading')
