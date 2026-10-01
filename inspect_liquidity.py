import json, urllib.request

url = "https://api.geckoterminal.com/api/v2/networks/base/trending_pools?page=1"
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
with urllib.request.urlopen(req, timeout=30) as r:
    data = json.loads(r.read())

for p in data["data"][:3]:
    a = p["attributes"]
    print(a.get("name"), "| reserve_in_usd:", repr(a.get("reserve_in_usd")), "| pool_tvl:", repr(a.get("pool_total_value_locked")))
