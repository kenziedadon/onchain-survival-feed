TEAM GROUP CHAT ROUTING (verified, user choice A)
- No separate group chat exists; session = Telegram DM (8662836265)
- Routing: base-research (profile) -> trade-execution (profile) -> ops-briefing (profile)
- Delivery targets: origin (this DM chat), telegram (8662836265 'Home'), local (cron output)
- Active crons routed through pipeline: base-trend-discovery, base-trend-trade-execute, base-trend-monitor, base-trend-status-brief, base-trend-orchestrator, base-trend-nous-sentiment, base-research daily briefing, base-trending-intel, trade-execution opportunity scan, base-opportunity-scan, base-scout-monitor, base-scout-telemetry
- caps (armed-with-gaps, verified unchanged): kill=False, live=True, edge=False, arm=arm_signal_v1, authorization=manual_human_authorization_with_gaps_accepted
- Replay unrecovered (dropped); v8 reconstructed (dropped); harness fixtures PASS; closest-to-live complete; user auth + edge gate required before perfect arm
