#!/usr/bin/env python3
"""Dump full pool detail for one known pool."""
import urllib.request, json

UA = "Mozilla/5.0 (X11; Linux x86_64) Gecko/20100101 Firefox/128.0"
url = "https://api.geckoterminal.com/api/v2/networks/base/pools/0x215b2146c3164aba3f5b18e8619b737c7f7dd11e5ba693646dcd9a336c5a30ba"
req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": UA})
with urllib.request.urlopen(req, timeout=30) as r:
    data = json.loads(r.read())

a = data["data"]["attributes"]
print("ATTRS KEYS:", sorted(a.keys()))
print()
print(json.dumps(a, indent=2)[:4000])
