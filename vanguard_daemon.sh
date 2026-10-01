#!/bin/bash
# =====================================================================
# HERMES VANGUARD DAEMON — autonomous Base watchlist scanner + executor
# =====================================================================
# Zero-cost: DexScreener free API + Base public RPC + swap.js (local key).
#
# SAFETY MODEL (do not bypass):
#   - Default mode is HUNT-ONLY. It scans, scores, and writes vetted signals
#     to vanguard_signals.jsonl. It does NOT broadcast.
#   - Pass --execute to arm execution. Even then, execute_buy.py enforces:
#       * caps.json kill_switch  (refuses if armed)
#       * per-trade cap          (caps.max_trade_eth)
#       * daily loss cap         (caps.max_daily_loss_eth, logged in eoa_spend_log.json)
#       * pre-trade balance check (on-chain ETH >= amount + gas buffer)
#   - The actual broadcast goes through the EXISTING swap.js layer, never a
#     new private-key module.
#
# Requires: python3, jq, node (for swap.js), bc
# Run:  tmux new -s vanguard
#       ./vanguard_daemon.sh            # hunt-only
#       ./vanguard_daemon.sh --execute  # hunt + broadcast (gated by caps)
# =====================================================================

set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCANNER="$HERE/base_token_scan.py"
EXECUTOR="$HERE/execute_buy.py"
WATCHLIST="$HERE/watchlist.txt"
SIGNAL_Q="$HERE/vanguard_signals.jsonl"
LOG_FILE="$HERE/vanguard_execution.log"
STATE="$HERE/vanguard_state.json"

POLL_INTERVAL=45          # base poll (seconds)
RATE_LIMIT_SLEEP=180      # backoff when scanner reports rate_limit
MAX_CONSEC_FAILURES=5     # circuit-breaker: stop daemon after this many bad scans in a row
SLIPPAGE=100              # bps

EXECUTE=0
[[ "${1:-}" == "--execute" ]] && EXECUTE=1

# ---- clean shutdown ----
RUNNING=1
shutdown() {
    RUNNING=0
    log_action "🛑 Caught shutdown signal — finishing current cycle, exiting."
}
trap shutdown SIGTERM SIGINT

log_action() {
    local msg="$1"
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] $msg" | tee -a "$LOG_FILE"
}

# atomic watchlist line removal (exact match, whole line — no fragile sed substring)
remove_from_watchlist() {
    local tok="$1"
    local tmp="$WATCHLIST.tmp.$$"
    grep -vxF "$tok" "$WATCHLIST" > "$tmp" && mv "$tmp" "$WATCHLIST"
}

consec_failures=0

log_action "🟢 Hermes Vanguard Daemon started. mode=$([ $EXECUTE -eq 1 ] && echo EXECUTE || echo HUNT-ONLY)"
touch "$WATCHLIST" "$SIGNAL_Q"

while [ "$RUNNING" -eq 1 ]; do
    # snapshot watchlist (ignore comments + blanks), work on a copy so removals mid-loop are safe
    mapfile -t TOKENS < <(grep -v '^#' "$WATCHLIST" | grep -v '^[[:space:]]*$')

    if [ "${#TOKENS[@]}" -eq 0 ]; then
        log_action "💤 Watchlist empty. Sleeping."
        sleep "$POLL_INTERVAL"
        continue
    fi

    for TARGET_TOKEN in "${TOKENS[@]}"; do
        [ "$RUNNING" -eq 0 ] && break
        log_action "🔍 Scanning target: $TARGET_TOKEN"

        SCAN_RESULT=$(python3 "$SCANNER" "$TARGET_TOKEN" --json 2>/dev/null)
        STATUS=$(echo "$SCAN_RESULT" | jq -r '.risk_assessment.status // "null"')
        SCORE=$(echo "$SCAN_RESULT" | jq -r '.risk_assessment.score // "null"')
        PRICE=$(echo "$SCAN_RESULT" | jq -r '.market_telemetry.price_usd // "null"')
        MAX_REC=$(echo "$SCAN_RESULT" | jq -r '.risk_assessment.max_recommended_trade_eth // "0"')
        ERR=$(echo "$SCAN_RESULT" | jq -r '.error // "null"')

        if [ "$STATUS" == "null" ] || [ -z "$STATUS" ]; then
            consec_failures=$((consec_failures + 1))
            log_action "⚠️ Scan failed/invalid for $TARGET_TOKEN (consec_failures=$consec_failures). Skipping."
            if [ "$consec_failures" -ge "$MAX_CONSEC_FAILURES" ]; then
                log_action "🔴 CIRCUIT BREAKER: $consec_failures consecutive scan failures. Stopping daemon."
                exit 3
            fi
            continue
        fi
        consec_failures=0

        # rate-limit backoff
        if [ "$ERR" == "rate_limited" ]; then
            log_action "⏳ Rate limited. Backing off ${RATE_LIMIT_SLEEP}s."
            sleep "$RATE_LIMIT_SLEEP"
            continue
        fi

        log_action "📊 Result -> Score: $SCORE/100 | Grade: $STATUS | Price: \$$PRICE"

        if [ "$STATUS" == "PASS" ]; then
            # write vetted signal to the queue (always)
            local_sig=$(jq -n --arg t "$TARGET_TOKEN" --argjson s "$SCORE" --argjson m "$MAX_REC" \
                '{ts: now, token: $t, score: $s, max_eth: $m, status: "PASS"}')
            echo "$local_sig" >> "$SIGNAL_Q"
            log_action "✅ VETTED SIGNAL queued: $TARGET_TOKEN (max_eth=$MAX_REC)"

            if [ "$EXECUTE" -eq 1 ]; then
                # execute_buy.py enforces kill_switch + caps + balance; refuses if unsafe
                python3 "$EXECUTOR" "$TARGET_TOKEN" "$MAX_REC" --slippage "$SLIPPAGE" 2>&1 | tee -a "$LOG_FILE"
                rc=${PIPESTATUS[0]}
                if [ "$rc" -eq 0 ]; then
                    log_action "💰 BUY BROADCAST OK: $TARGET_TOKEN"
                    remove_from_watchlist "$TARGET_TOKEN"
                elif [ "$rc" -eq 2 ]; then
                    log_action "🛡️ BUY BLOCKED by circuit-breaker (kill_switch/caps/balance). Holding $TARGET_TOKEN."
                else
                    log_action "❌ BUY FAILED (rc=$rc) for $TARGET_TOKEN. Holding for retry."
                fi
            else
                log_action "📝 HUNT-ONLY: not broadcasting. Remove token manually or run with --execute."
            fi
        else
            log_action "🛡️ Target did not PASS (Status: $STATUS). Holding fire."
        fi
    done

    # respect rate-limit backoff already applied; otherwise base poll
    [ "$RUNNING" -eq 1 ] && sleep "$POLL_INTERVAL"
done

log_action "👋 Daemon exited cleanly."
