#!/usr/bin/env python3
"""Debug v4: inspect full pool response structure."""
import json, time, urllib.request, urllib.error

BASE = "https://api.geckoterminal.com/api/v2"

def get(url, retries=3):
    headers = {"Accept": "application/json", "User-Agent": "Debug/1.0"}
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            if e.code == 429:
                print(f"  429, retry {i+1}/{retries}")
                time.sleep(5)
                continue
            return {"error": e.code, "body": body}
        except Exception as e:
            return {"error": str(e)}
    return {"error": "exhausted"}

# First get a pool_id from trending (retry until success)
print("Fetching trending (may take a few retries due to 429)...")
trending = None
for attempt in range(5):
    trending = get(f"{BASE}/networks/base/trending_pools?page=1")
    if "data" in trending and len(trending["data"]) > 0:
        print(f"  Success on attempt {attempt+1}")
        break
    print(f"  Attempt {attempt+1} failed, retrying...")
    time.sleep(3)

if not trending or "data" not in trending:
    print("FAILED to get trending pools")
    exit(1)

pool_id = trending["data"][0]["id"]
print(f"\nFirst trending pool ID: {pool_id}")
print(f"Full entry from trending: {json.dumps(trending['data'][0], indent=2)[:800]}")

# Now fetch pool details
print(f"\nFetching pool details for {pool_id}...")
pd = None
for attempt in range(5):
    pd = get(f"{BASE}/networks/base/pools/{pool_id}")
    if "data" in pd:
        print(f"  Success on attempt {attempt+1}")
        break
    print(f"  Attempt {attempt+1} failed, retrying...")
    time.sleep(3)

if pd and "data" in pd:
    # Inspect full structure
    entry = pd["data"]
    print(f"\nFull pool data keys: {list(entry.keys())}")
    print(f"\n--- Top-level ---")
    print(json.dumps(entry, indent=2, default=str)[:2000])
