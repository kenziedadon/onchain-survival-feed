#!/usr/bin/env python3
import json, urllib.request, time

def fetch(url, retries=3):
    for a in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent":"HermesBot/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except Exception as e:
            if a < retries-1:
                time.sleep(3)
    return None

time.sleep(3)
d = fetch("https://api.geckoterminal.com/api/v2/networks/base/trending_pools?page=1")
if not d:
    print("FAILED"); exit(1)

print("=== RAW STRUCTURE (first pool) ===")
p0 = d["data"][0]
print(json.dumps(p0, indent=2)[:3000])
