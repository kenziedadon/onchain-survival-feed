#!/usr/bin/env python3
"""Refresh Nous token via refresh_token flow."""

import json
import urllib.request
import urllib.parse

AUTH_PATH = "/home/kenzodadon/.hermes/shared/nous_auth.json"

with open(AUTH_PATH) as f:
    auth = json.load(f)

refresh_token = auth.get("refresh_token", "")
portal_url = auth.get("portal_base_url", "https://portal.nousresearch.com")

print(f"Refreshing token via {portal_url}...")
print(f"Refresh token: {refresh_token[:20]}...")

# Correct OAuth token endpoint (Hermes auth_nous.py reference)
url = f"{portal_url}/api/oauth/token"

data = urllib.parse.urlencode({"grant_type": "refresh_token", "client_id": "hermes-cli"}).encode()

req = urllib.request.Request(
    url,
    data=data,
    headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "x-nous-refresh-token": refresh_token,
        "User-Agent": "HermesBaseTrendBot/1.0",
    },
    method="POST",
)

try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
    print("Refresh successful!")
    print(json.dumps(result, indent=2)[:500])
    
    # Update auth file (payload: access_token + expires_in TTL; refresh_token rotates single-use)
    from datetime import datetime, timedelta, timezone as tz
    now = datetime.now(tz.utc)
    ttl = int(result.get("expires_in", 3600))
    auth["access_token"] = result.get("access_token", auth.get("access_token"))
    auth["refresh_token"] = result.get("refresh_token") or refresh_token
    auth["token_type"] = result.get("token_type", auth.get("token_type", "Bearer"))
    auth["expires_in"] = ttl
    auth["expires_at"] = (now + timedelta(seconds=ttl)).isoformat()
    auth["obtained_at"] = now.isoformat()
    auth["updated_at"] = now.isoformat()
    
    with open(AUTH_PATH, "w") as f:
        json.dump(auth, f, indent=2)
    print(f"\nAuth file updated: {AUTH_PATH}")
except urllib.error.HTTPError as e:
    print(f"HTTP Error: {e.code} {e.reason}")
    print(e.read().decode()[:500])
except Exception as e:
    print(f"Error: {e}")
