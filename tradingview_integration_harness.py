# Verified: free clone of atilaahmettaner/tradingview-mcp (v0.9.0, 37 tools, no TV account)
# Verified: server.json points to mcp.cryptosieve.com (streamable-http remote)
# NOT verified: no endpoint call executed; NO live backtest; DISARMED preserved
# Team intermediated: base-research (verify endpoint) -> trade-execution (run backtest) -> ops-briefing (report)
# Gaps preserved: replay unrecovered (dropped per user); v8 BLOCKED; WETH revert; USDC 0 discrepancy; edge 56% false
# New strategy: tradingview_ema_v1 (new caps at /tmp/tradingview-mcp/new_caps_...)
# This file is the BUILD artifact — NOT a live submit mechanism.
verified_state = {"endpoint_tested": False, "backtest_run": False, "live_submit": False, "kill_switch": True, "live_trading": False, "disarmed_preserved": True}
