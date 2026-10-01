import json, urllib.request

def get(url, t=30):
    req = urllib.request.Request(url, headers={"User-Agent":"HermesBT/1.0"})
    with urllib.request.urlopen(req, timeout=t) as r:
        return json.loads(r.read())

base = "https://api.geckoterminal.com/api/v2/networks/base"
trending = get(f"{base}/trending_pools?page=1")
pools = trending.get("data", [])
print(f"Total: {len(pools)}")
# Dump first pool raw
import pprint
print("\n=== FIRST POOL RAW ===")
pprint.pprint(pools[0])
