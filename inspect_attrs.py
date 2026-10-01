import json

with open('/home/kenzodadon/base-trend-bot/trending_raw.json') as f:
    data = json.load(f)

for i, pool in enumerate(data['data'][:3]):
    attrs = pool.get('attributes', {})
    print(f"\n=== Pool {i+1} ===")
    print(f"  name: {attrs.get('name', '?')}")
    # print all attribute keys
    for k in sorted(attrs.keys()):
        v = attrs[k]
        if isinstance(v, dict):
            print(f"  {k}: dict with keys {list(v.keys())}")
            for nk, nv in v.items():
                if not isinstance(nv, (dict, list)):
                    print(f"    {nk}: {nv}")
        elif isinstance(v, list):
            print(f"  {k}: list[{len(v)}]")
        else:
            print(f"  {k}: {v}")
