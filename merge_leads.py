#!/usr/bin/env python3
"""
MERGE: auto-fill the Base trending-coin intel DM + inbound X post templates
from the lead firehose.

Reads ~/lead_firehose_log.md (real, sourced leads), filters to Base-relevant
or coin/token-adjacent raises, and fills the templates with REAL data:
  - ~/research-service/intel_dms.md   (ready-to-personalize outbound DMs)
  - ~/research-service/intel_posts.md  (inbound X posts, niche-aware)

SAFETY: never fabricates a founder handle. [Name]/[Handle] stay as
verified-by-you placeholders, matching the existing draft_dms.md convention.
Posts lead with insight, no [Name], one consistent CTA ("intel").

Run: python3 merge_leads.py
"""
import re, os, datetime

FIREHOSE = os.path.expanduser("~/lead_firehose_log.md")
OUT = os.path.expanduser("~/research-service/intel_dms.md")
POSTS = os.path.expanduser("~/research-service/intel_posts.md")

# Base-relevance signals (lead is a fit for the live Base coin-intel feed)
BASE_HINTS = ["base", "onchain", "defi", "dex", "token", "stablecoin",
              "agentkit", "x402", "coinbase", "prediction", "perps",
              "launchpad", "ico", "payments", "wallet"]

# Niche -> peer set used when the lead's angle names no explicit competitor.
NICHE_PEERS = {
    "stablecoin": "Circle, Tether, Conduit",
    "payments": "Conduit, Bitso, HoneyCoin",
    "wallet": "Coinbase, AgentKit, Skyfire",
    "defi": "Aerodrome, Uniswap, Morpho",
    "dex": "Aerodrome, Uniswap, Velodrome",
    "agent": "x402, Coinbase AgentKit, Skyfire",
    "ai": "Bittensor, Virtuals, ai16z",
    "prediction": "Kalshi, Polymarket, Limitless",
    "launchpad": "Echo, Sonar, CoinList",
    "ico": "Echo, Sonar, Fjord",
    "depin": "Render, IO, Akash",
    "cross-border": "Conduit, Bitso, HoneyCoin",
}

# Niche -> inbound post hook (lead with insight, not the congrats).
NICHE_HOOK = {
    "stablecoin": "Stablecoin teams on Base are flying blind on where liquidity rotates.",
    "payments": "Cross-border payments on Base move faster than your board deck.",
    "wallet": "AI-wallet teams are about to pile into Base — are you watching the flows?",
    "defi": "Base DeFi TVL is consolidating into a handful of winners.",
    "dex": "New Base DEX launches live or die on first-hour liquidity — the data shows it every 6h.",
    "agent": "Agent-payments is the most crowded Base narrative right now.",
    "ai": "Decentralized-AI tokens on Base pop and fade on pure momentum.",
    "prediction": "Prediction markets on Base are a sentiment gauge, not a graveyard.",
    "launchpad": "Every Base launchpad claims 'fair' — the on-chain flow says otherwise.",
    "ico": "Revived ICOs on Base live or die by first-hour liquidity.",
    "depin": "DePIN on Base is a liquidity story disguised as an infrastructure story.",
    "cross-border": "EM corridors on Base are a pricing-war battlefield week to week.",
}


def parse_leads(path):
    """Parse the '## New leads' section of the firehose into row dicts."""
    if not os.path.exists(path):
        return []
    txt = open(path).read()
    idx = txt.find("## New leads")
    if idx < 0:
        return []
    rows = []
    for line in txt[idx:].splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 7:
            continue
        if cells[0].lower() in ("date",) or "---" in cells[0]:
            continue
        date, company, amount, stage, niche, url, angle = cells[:7]
        if not company or company.lower() == "company":
            continue
        rows.append({"date": date, "company": company, "amount": amount,
                     "stage": stage, "niche": niche, "url": url, "angle": angle})
    return rows


def is_base_relevant(lead):
    blob = (lead["niche"] + " " + lead["angle"] + " " + lead["company"]).lower()
    return any(h in blob for h in BASE_HINTS)


def extract_competitor(lead):
    """Named competitor from the angle if present, else a niche peer set."""
    found = re.findall(r"\(([^)]+)\)", lead["angle"])
    for f in found:
        names = re.findall(r"[A-Z][A-Za-z0-9]+", f)
        if names and len(names) <= 4:
            return ", ".join(names[:3])
    blob = (lead["niche"] + " " + lead["angle"]).lower()
    for key, peers in NICHE_PEERS.items():
        if key in blob:
            return peers
    return "top 3 Base competitors"


def fill_dm(lead):
    comp = extract_competitor(lead)
    return f"""────────────────────────────────────────────────
{lead['date']} — {lead['company']} ({lead['niche']}, {lead['amount']} {lead['stage']})
────────────────────────────────────────────────
Send via: find {lead['company']} on X or LinkedIn — locate a founder/BD lead
from the round announcement post. Handle NOT invented; confirm before sending.
---
Hi [Name] at {lead['company']} — congrats on the {lead['amount']} {lead['stage']}.
I run a live Base intel feed that ranks trending + early Base coins by liquidity,
5m volume, and fee tier, refreshed every 6h — not a screener you configure, a
report that lands in your inbox. For a {lead['company']} raise the obvious first
use is tracking where capital is rotating on Base in real time.

$500 pilot: I'll send you a branded teardown of one competitor you name
({comp}). If it's useful, $400-800/mo white-label for the recurring feed.
USDC or fiat. Want me to run {comp}?
---
"""


def fill_post(lead):
    """Inbound X post: lead with insight, no [Name], consistent CTA 'intel'."""
    comp = extract_competitor(lead)
    blob = (lead["niche"] + " " + lead["angle"]).lower()
    hook = None
    for key, h in NICHE_HOOK.items():
        if key in blob:
            hook = h
            break
    if not hook:
        hook = "Base builders are guessing which coins are moving — stop guessing."
    return (f"{hook}\n\n"
            f"I ship a ranked feed of trending + early Base coins — liquidity, "
            f"5m volume, fee tier — every 6h, white-label ready. $500 pilot teardown "
            f"of {comp}. DM \"intel\" for a sample.\n")


def main():
    leads = parse_leads(FIREHOSE)
    base = [l for l in leads if is_base_relevant(l)]
    print(f"Parsed {len(leads)} leads from firehose; {len(base)} are Base/coin-relevant.")
    if not base:
        print("No Base-relevant leads to fill right now.")
        return
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

    dm_header = (f"# Base Trending-Coin Intel — filled DM batch\n"
                 f"Generated: {today}  |  Source: lead_firehose_log.md\n"
                 f"Manual-send kit: Premium X / LinkedIn only (non-Premium X DMs dropped).\n"
                 f"Personalize [Name]; confirm handle. No links in DM #1.\n\n")
    with open(OUT, "w") as f:
        f.write(dm_header + "".join(fill_dm(l) for l in base))
    print(f"Wrote {len(base)} filled DMs to {OUT}")

    post_header = (f"# Base Trending-Coin Intel — inbound X posts\n"
                   f"Generated: {today}  |  Source: lead_firehose_log.md\n"
                   f"Post free from the X app (no API credits). One consistent CTA: \"intel\".\n"
                   f"1-2/day spread across the week; do not blast all at once.\n\n")
    posts = "".join(f"### {l['company']} ({l['niche']})\n{fill_post(l)}\n" for l in base)
    with open(POSTS, "w") as f:
        f.write(post_header + posts)
    print(f"Wrote {len(base)} inbound posts to {POSTS}")
    print("Sample DM (first lead):")
    print(fill_dm(base[0]).split('---')[0].strip())
    print("\nSample post (first lead):")
    print(fill_post(base[0]).strip())


if __name__ == "__main__":
    main()
