#!/usr/bin/env python3
"""Diagnose Nous token and API connectivity."""

import json
import urllib.request
import urllib.error
import time
from datetime import datetime, timezone

with open("/home/kenzodadon/.hermes/shared/nous_auth.json") as f:
    auth = json.load(f)

token = auth["access_token"]
base = "https://inference-api.nousresearch.com/v1"

print("=== 1. Models endpoint ===")
req = urllib.request.Request(f"{base}/models", headers={"Authorization": f"Bearer {token}"})
try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
        print(f"Status: {resp.status}")
        print(f"First 5: {json.dumps(data.get('data', data)[:5], indent=2)[:400]}")
except urllib.error.HTTPError as e:
    print(f"HTTP {e.code}: {e.read().decode()[:300]}")
except Exception as e:
    print(f"Error: {e}")

print("\n=== 2. Try different model IDs ===")
models_to_try = [
    "upstage/solar-pro4:free",
    "upstage/solar-pro4",
    "solar-pro4:free",
]

for m in models_to_try:
    payload = json.dumps({
        "model": m,
        "messages": [{"role": "user", "content": "Say hi"}],
        "max_tokens": 10,
    }).encode()
    
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
            data = json.loads(resp.read().decode())
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            print(f"  {m}: OK - {content[:80]}")
            break
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:200]
        print(f"  {m}: HTTP {e.code} - {body}")
    except Exception as e:
        print(f"  {m}: Error - {e}")
    time.sleep(0.5)

print("\n=== 3. Token validity check ===")
req = urllib.request.Request(f"{base}/models", headers={"Authorization": f"Bearer {token}"})
try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        print(f"Token valid for /models: HTTP {resp.status}")
except urllib.error.HTTPError as e:
    print(f"Token issue: HTTP {e.code} - {e.read().decode()[:200]}")

print(f"\nToken expires_at: {auth.get('expires_at')}")
print(f"Current UTC: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
exp = datetime.fromisoformat(auth["expires_at"].replace("Z", "+00:00"))
now = datetime.now(timezone.utc)
print(f"Seconds until expiry: {(exp - now).total_seconds():.0f}")
