import urllib.request, json, time, math
from datetime import datetime, timezone

AUTH_FILE = "/home/kenzodadon/.hermes/shared/nous_auth.json"
OUT_FILE  = "/home/kenzodadon/base-trend-bot/nous_signals.json"
Nous_URL  = "https://inference-api.nousresearch.com/v1/chat/completions"
MODEL     = "upstage/solar-pro4:free"

def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent":"HermesBot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

def ema(series, span):
    k = 2.0 / (span + 1)
    out = [series[0]]
    for v in series[1:]:
        out.append((v - out[-1]) * k + out[-1])
    return out

# ---- load auth ----
with open(AUTH_FILE) as f:
    auth = json.load(f)
token = auth["access_token"]
headers = {"Authorization": f"Bearer {token}", "Content-Type":"application/json"}

# ---- fetch trending pools ----
print("=" * 60)
print("Nous Sentiment Gate — Base Trend Bot")
print(f"Time: {datetime.now(timezone.utc).isoformat()}")
print("=" * 60)

gt = fetch("https://api.geckoterminal.com/api/v2/networks/base/trending_pools?page=1")
pools_raw = gt.get("data", [])
print(f"\nFetched {len(pools_raw)} trending pools from GeckoTerminal")

# ---- inspect structure first ----
a0 = pools_raw[0]["attributes"]
print(f"Pool attrs keys: {list(a0.keys())}")
print(f"Sample reserve_in_usd: {a0.get('reserve_in_usd')} (type: {type(a0.get('reserve_in_usd')).__name__})")
print(f"Sample base_token_price_usd: {a0.get('base_token_price_usd')}")
print(f"Sample name: {a0.get('name')}")
print(f"Sample address: {a0.get('address')}")

# Check if there's a way to get token symbols — maybe from reserve_in_usd being a number
# The trending pools endpoint might not have token symbols; we need pool detail
print("\nGetting pool detail for first pool to check symbol availability...")
first_addr = a0.get("address")
detail = fetch(f"https://api.geckoterminal.com/api/v2/networks/base/pools/{first_addr}")
d_attrs = detail["data"]["attributes"]
print(f"Detail keys: {list(d_attrs.keys())}")
bt = d_attrs.get("base_token", {})
qt = d_attrs.get("quote_token", {})
print(f"Base token: {bt.get('symbol')} ({bt.get('address')})")
print(f"Quote token: {qt.get('symbol')} ({qt.get('address')})")
print(f"Detail reserve_in_usd: {d_attrs.get('reserve_in_usd')}")

# Check OHLCV structure
print("\nGetting OHLCV for first pool...")
ohlcv = fetch(f"https://api.geckoterminal.com/api/v2/networks/base/pools/{first_addr}/ohlcv/hour?aggregate=1&limit=5")
oc_data = ohlcv.get("data", [])
print(f"OHLCV data items: {len(oc_data)}")
if oc_data:
    print(f"First item: {json.dumps(oc_data[0], indent=2)[:500]}")
