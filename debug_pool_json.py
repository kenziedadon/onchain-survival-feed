#!/usr/bin/env python3
import urllib.request
import json

GECKO = "https://api.geckoterminal.com/api/v2/networks/base"

req = urllib.request.Request(
    f"{GECKO}/trending_pools?page=1",
    headers={
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
        "Origin": "https://www.geckoterminal.com",
        "Referer": "https://www.geckoterminal.com/",
    }
)
with urllib.request.urlopen(req, timeout=30) as resp:
    data = json.loads(resp.read())

# Dump first pool full JSON
print(json.dumps(data["data"][0], indent=2))
