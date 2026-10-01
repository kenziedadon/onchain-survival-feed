#!/usr/bin/env python3
import urllib.request, json

url = "https://api.geckoterminal.com/api/v2/networks/base/trending_pools?page=1"
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
with urllib.request.urlopen(req, timeout=30) as resp:
    raw = resp.read()

print(f"RAW ({len(raw)} bytes):")
print(raw[:3000].decode("utf-8", errors="replace"))
