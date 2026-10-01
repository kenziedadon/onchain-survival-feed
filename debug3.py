#!/usr/bin/env python3
import urllib.request, json, time

HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    "Origin": "https://www.geckoterminal.com",
    "Referer": "https://www.geckoterminal.com/",
}
GECKO = "https://api.geckoterminal.com/api/v2/networks/base"

def api_get(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())

# Check the mystery token
tid = "0xb2000000000000000000004c27f6523082f41d01"
print(f"=== Token {tid} ===")
try:
    d = api_get(f"{GECKO}/tokens/{tid}")
    print(json.dumps(d.get("data", {}).get("attributes", {}), indent=2))
except Exception as e:
    print(f"Error: {e}")

# Check the mystery pool
print("\n=== Pool 0x215b... ===")
try:
    d = api_get(f"{GECKO}/pools/0x215b2146c3164aba3f5b18e8619b737c7f7dd11e5ba693646dcd9a336c5a30ba")
    attrs = d.get("data", {}).get("attributes", {})
    print(f"Name: {attrs.get('name')}")
    print(f"Reserve: {attrs.get('reserve_in_usd')}")
    rel = d.get("data", {}).get("relationships", {})
    for side in ("base_token", "quote_token"):
        rid = rel.get(side, {}).get("data", {}).get("id", "")
        print(f"{side} id: {rid}")
        # Fetch symbol
        try:
            td = api_get(f"{GECKO}/tokens/{rid.replace('base_', '')}")
            sym = td.get("data", {}).get("attributes", {}).get("symbol", "?")
            name = td.get("data", {}).get("attributes", {}).get("name", "?")
            print(f"  symbol: {sym}, name: {name}")
        except Exception as e2:
            print(f"  symbol fetch error: {e2}")
        time.sleep(1.5)
except Exception as e:
    print(f"Error: {e}")
