#!/usr/bin/env python3
"""Test Nous API connectivity with different endpoints."""

import json
import urllib.request
import urllib.error

with open("/home/kenzodadon/.hermes/shared/nous_auth.json") as f:
    auth = json.load(f)

token = auth["access_token"]
base_urls = [
    "https://inference-api.nousresearch.com/v1",
    "https://inference.nousresearch.com/v1",
]

payload = json.dumps({
    "model": "upstage/solar-pro4:free",
    "messages": [{"role": "user", "content": "Say hi"}],
    "max_tokens": 10,
}).encode()

for base in base_urls:
    print(f"Trying {base}...")
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"  OK: {resp.status}")
            data = json.loads(resp.read().decode())
            print(f"  {json.dumps(data, indent=2)[:300]}")
            break
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code}: {e.reason}")
        body = e.read().decode()[:200]
        print(f"  Body: {body}")
    except Exception as e:
        print(f"  Error: {e}")
    print()
