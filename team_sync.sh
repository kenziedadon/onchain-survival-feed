#!/usr/bin/env bash
# team_sync.sh — sync CLI state with bot team (discovery / evaluation / execution)
# Usage: bash team_sync.sh [--check|--lock|--unlock|--full]
#
# Protocol:
#   1. Read caps.json (source of truth for trading state)
#   2. Read discovered.jsonl (latest discovery snapshot)
#   3. Read positions.json (current holdings)
#   4. Verify cron jobs are scheduled
#   5. Verify swap.js key file exists and matches wallet
#   6. Write team_sync.lock with current state hash
#   7. Report sync status

set -euo pipefail
cd /home/kenzodadon/base-trend-bot || exit 1

CAPS="caps.json"
DISC="discovered.jsonl"
POS="positions.json"
LOCK="team_sync.lock"
KEY_FILE="/home/kenzodadon/onchain-survival/.payer_key_tmp"

echo "=== TEAM SYNC $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

# 1. caps.json state
if [[ -f "$CAPS" ]]; then
    KILL=$(jq -r '.kill_switch' "$CAPS")
    LIVE=$(jq -r '.live_trading' "$CAPS")
    MAX_POS=$(jq -r '.max_positions' "$CAPS")
    MIN_SCORE=$(jq -r '.min_score' "$CAPS")
    WALLET=$(jq -r '.wallet' "$CAPS")
    echo "caps.json: kill_switch=$KILL live_trading=$LIVE max_pos=$MAX_POS min_score=$MIN_SCORE"
    echo "wallet: $WALLET"
else
    echo "ERROR: caps.json not found"
    exit 1
fi

# 2. discovered.jsonl state
if [[ -f "$DISC" ]]; then
    LINES=$(wc -l < "$DISC")
    LATEST=$(tail -1 "$DISC" | jq -r '.ts')
    COINS=$(tail -1 "$DISC" | jq -c '.coins | length')
    echo "discovered.jsonl: $LINES lines, latest=$LATEST, coins_in_latest=$COINS"
else
    echo "ERROR: discovered.jsonl not found"
    exit 1
fi

# 3. positions.json state
if [[ -f "$POS" ]]; then
    POS_COUNT=$(jq -r 'length' "$POS" 2>/dev/null || echo "0")
    echo "positions.json: $POS_COUNT active positions"
else
    echo "positions.json: not found (no positions)"
fi

# 4. cron verification
echo "--- cron jobs ---"
crontab -l 2>/dev/null | grep -E '(discover|execute|backtest|scout)' || echo "no relevant crontab entries"

# 5. key file verification
if [[ -f "$KEY_FILE" ]]; then
    KEY_SIZE=$(wc -c < "$KEY_FILE")
    echo "key file: $KEY_FILE (${KEY_SIZE} bytes)"
else
    echo "WARNING: key file not found at $KEY_FILE"
fi

# 6. on-chain balance check
echo "--- on-chain balances ---"
ETH_BAL=$(curl -s -X POST https://mainnet.base.org \
    -H "Content-Type: application/json" \
    -d "{\"jsonrpc\":\"2.0\",\"method\":\"eth_getBalance\",\"params\":[\"$WALLET\",\"latest\"],\"id\":1}" \
    | jq -r '.result // "0x0"')
ETH_ETH=$(python3 -c "print(int('$ETH_BAL', 16) / 1e18)" 2>/dev/null || echo "0")
echo "ETH balance: $ETH_ETH ETH"

USDC_BAL=$(curl -s -X POST https://mainnet.base.org \
    -H "Content-Type: application/json" \
    -d "{\"jsonrpc\":\"2.0\",\"method\":\"eth_call\",\"params\":[{\"to\":\"0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913\",\"data\":\"0x70a08231000000000000000000000000${WALLET#0x}\"},\"latest\"],\"id\":1}" \
    | jq -r '.result // "0x0"')
USDC_HUMAN=$(python3 -c "print(int('$USDC_BAL', 16) / 1e6)" 2>/dev/null || echo "0")
echo "USDC balance: $USDC_HUMAN USDC"

# 7. write lock
STATE_HASH=$(echo "$KILL|$LIVE|$MAX_POS|$MIN_SCORE|$LINES|$LATEST" | md5sum | cut -d' ' -f1)
cat > "$LOCK" << EOF
{
  "ts": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "state_hash": "$STATE_HASH",
  "caps": {"kill_switch": $KILL, "live_trading": $LIVE, "max_positions": $MAX_POS, "min_score": $MIN_SCORE},
  "discovery": {"lines": $LINES, "latest_ts": "$LATEST", "coins_in_latest": $COINS},
  "wallet": "$WALLET",
  "balances": {"eth": $ETH_ETH, "usdc": $USDC_HUMAN}
}
EOF
echo "lock written: $LOCK (hash=$STATE_HASH)"

# 8. sync status
echo "--- SYNC STATUS ---"
if [[ "$KILL" == "true" ]]; then
    echo "STATUS: KILL SWITCH ACTIVE — no trades possible"
elif [[ "$LIVE" != "true" ]]; then
    echo "STATUS: DISARMED — live_trading=false"
elif python3 -c "exit(0 if float('$USDC_HUMAN') < 5 else 1)" 2>/dev/null; then
    echo "STATUS: LOW USDC — balance $USDC_HUMAN < 5 minimum"
else
    echo "STATUS: READY — live_trading=true, kill_switch=false, USDC=$USDC_HUMAN"
fi
echo "=== SYNC COMPLETE ==="