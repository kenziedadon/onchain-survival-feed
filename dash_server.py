#!/usr/bin/env python3
"""Base trend bot dashboard server. Serves dashboard.html + /api/* from live bot files."""
import json, os, time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse
from _tolerant_read import iter_jsonl_entries

BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE)

def read_json(name, default=None):
    try:
        with open(os.path.join(BASE, name)) as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}

def read_last_jsonl(name):
    try:
        recs = list(iter_jsonl_entries(os.path.join(BASE, name)))
        return recs[-1] if recs else {}
    except Exception:
        return {}

def rpc_eth_balance(address):
    """Fresh eth_getBalance from public Base RPCs."""
    import urllib.request
    for rpc in ("https://base.drpc.org", "https://1rpc.io/base", "https://mainnet.base.org"):
        try:
            req = urllib.request.Request(rpc, data=json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "eth_getBalance",
                "params": [address, "latest"]}).encode(),
                headers={"Content-Type": "application/json",
                         "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/126"})
            with urllib.request.urlopen(req, timeout=6) as r:
                hexbal = json.loads(r.read()).get("result")
            if hexbal:
                return int(hexbal, 16) / 1e18
        except Exception:
            continue
    return None

class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *a): pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/api/state':
            caps = read_json('caps.json')
            state = read_json('risk_state.json')
            holdings = read_json('holdings.json')
            bt = read_json('backtest_results.json', [])
            disc = read_last_jsonl('discovered.jsonl')
            wallet = read_json('wallet.json')
            addr = wallet.get('trading_address')
            live_bal = rpc_eth_balance(addr) if addr else None
            # enrich holdings with live balances via eth_call balanceOf
            holdings = read_json('holdings.json')
            for tok_addr, h in (holdings or {}).items():
                if isinstance(h, dict) and not h.get('amount_token'):
                    h['amount_token_raw'] = None  # token decimals unknown; keep raw null
            out = {
                'ts': time.time(),
                'caps': caps,
                'risk': {
                    'day': state.get('day'),
                    'spent_eth': state.get('spent_eth', 0),
                    'pnl_eth': state.get('pnl_eth', 0),
                    'trades': state.get('trades', [])[-20:],
                    'blocked': state.get('blocked', []),
                },
                'holdings': holdings,
                'wallet': {
                    'address': addr,
                    'type': wallet.get('type'),
                    'balance_eth': live_bal if live_bal is not None else
                                   ((wallet.get('verified_on_chain') or {}).get('balance_eth')),
                    'balance_source': 'rpc-live' if live_bal is not None else 'file-cached',
                    'nonce': (wallet.get('verified_on_chain') or {}).get('nonce'),
                },
                'backtest': bt if isinstance(bt, list) else [],
                'discovery': disc.get('coins', [])[:15],
                'discovery_ts': disc.get('ts'),
            }
            self.send_json(out)
        else:
            super().do_GET()

if __name__ == '__main__':
    port = int(os.environ.get('DASH_PORT', '4022'))
    print(f'Dashboard: http://localhost:{port}/dashboard.html')
    HTTPServer(('0.0.0.0', port), Handler).serve_forever()
