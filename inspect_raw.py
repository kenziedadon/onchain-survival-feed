import json

with open('/home/kenzodadon/base-trend-bot/raw_trending.json') as f:
    data = json.load(f)

print(f"Top-level keys: {list(data.keys())}")
if 'data' in data:
    ds = data['data']
    print(f"Number of pools: {len(ds)}")
    if ds:
        p0 = ds[0]
        print(f"Pool keys: {list(p0.keys())}")
        print(f"Attributes keys: {list(p0.get('attributes', {}).keys())}")
        attrs = p0.get('attributes', {})
        for k, v in attrs.items():
            if isinstance(v, (dict, list)):
                print(f"  {k}: <{type(v).__name__} len={len(v) if hasattr(v, '__len__') else '?'}>")
            else:
                print(f"  {k}: {v}")
        print("\n--- base_token ---")
        print(json.dumps(p0.get('base_token', {}), indent=2))
        print("--- quote_token ---")
        print(json.dumps(p0.get('quote_token', {}), indent=2))
