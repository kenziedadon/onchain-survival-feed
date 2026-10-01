TRADING TEAM COMMUNICATION PROTOCOL (2026-08-25, approved by lead)
- Topology: A — Head of Desk broadcasts same update to all 3 teammates (base-research, ops-briefing, trade-execution). Each teammate acknowledged (verified messages in session history).
- Trigger: ON EVENT ONLY — edge flip, new discovery coin (score >= min_score from caps.json), gate failure/block, sell-block, kill_switch change, arm evidence change. NO scheduled heartbeat; NO 15m flood.
- Router: Head of Desk (me) sees everything; surfaces the SINGLE arm decision to user (not to team). Team members never self-arm or broadcast trades.
- Evidence: messages delivered via message_agent (async, acknowledgments received); group file NOT used (user selected broadcast over file-based observation).
