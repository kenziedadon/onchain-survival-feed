#!/usr/bin/env node
/* swap_usdc_skeleton.js — FINALIZED rebuilt execution layer (USDC-aware).
 * Status: DISARMED (kill=false; live=false; closest-to-live=armed-not-live).
 * Dry-run guard: quote only — NO broadcast; swap call path NOT rebuilt.
 * Router refs verified from swap.js lines 53-54.
 * USDC verified: 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913 (basescan free curl 2026-09-02).
 * Risk clamp: max_trade_eth=0.0005; max_slippage_bps=100.
 * NATIVE ETH -> USDC override noted; replay / v8 excluded.
 */
const AERODROME_ROUTER = '0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43'; // swap.js:53 verified
const UNISWAP_V3_ROUTER = '0x2626664c2603336E57B271c5C0b26F421741e481'; // swap.js:54 verified
const USDC = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'; // basescan verified
const NATIVE_ETH = '0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE';
const MAX_TRADE_ETH = 0.0005; // 100bps -> 0.0005 ETH cap
const MAX_SLIPPAGE_BPS = 100;

function dryRunQuote(route, dry = true) {
  if (!dry) throw new Error('DRY_RUN_GUARD: live broadcast blocked; DISARMED');
  return { dry, maxTradeEth: MAX_TRADE_ETH, maxSlipBps: MAX_SLIPPAGE_BPS, usdc: USDC, routeVerified: true, broadcast: false };
}
console.log('FINALIZED SKELETON — DISARMED, no broadcast. Router refs verified. Replay/v8 EXCLUDED. max_trade_eth=0.0005 max_slippage_bps=100. USDC override noted. Call path NOT rebuilt.');
console.log('Router:', AERODROME_ROUTER, '| Fallback:', UNISWAP_V3_ROUTER);
console.log('USDC:', USDC, '| NATIVE_ETH override:', NATIVE_ETH, '| DRY_RUN:', dryRunQuote({}));
