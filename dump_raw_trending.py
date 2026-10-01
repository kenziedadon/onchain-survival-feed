#!/usr/bin/env python3
import urllib.request, json, time

GECKO = "https://api.geckoterminal.com/api/v2/networks/base"

def fetch(url):
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "hermes/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(6 * (2 ** attempt))
            else:
                raise
    raise Exception("Failed")

data = fetch(f"{GECKO}/trending_pools?page=1")
print(json.dumps(data, indent=2)[:5000])
