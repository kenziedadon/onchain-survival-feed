AERODROME — OFFICIAL ADDRESSES + CORRECTED ROOT CAUSE
Source: github.com/aerodrome-finance/contracts + /slipstream README.md (official repos, raw)
All addresses eth_getCode-verified on Base mainnet 2026-08-20.

=============================================================
CORRECTION TO MY OWN PRIOR ANALYSIS — READ THIS FIRST
=============================================================
Earlier I reported the V2 Router 0xcF77a3Ba...874E43 exposes NO swap entrypoints and
concluded swap.js calls a non-existent function. That conclusion was WRONG in its
reasoning, though right that the call reverts.

The real problem is the Route STRUCT ARITY, not a missing function.

  swap.js declares:  swapExactETHForTokens(uint256,(address,address,bool)[],address,uint256)
                     selector 0x67ffb66a  -> ABSENT on the router
  Router actually has: swapExactETHForTokens(uint256,(address,address,bool,address)[],address,uint256)
                     selector 0x903638a4  -> PRESENT

  Same for the quote path:
    getAmountsOut(uint256,(address,address,bool)[])          0x9881fcb4  ABSENT
    getAmountsOut(uint256,(address,address,bool,address)[])  0x5509a1ac  PRESENT

Aerodrome's Route struct is {from, to, stable, FACTORY} — FOUR fields. swap.js omits
the trailing `address factory` field. Wrong arity -> wrong selector -> revert.
My earlier "all selectors absent" probe was testing only 3-field variants and the
Uniswap-V3 shapes, so it missed the 4-field match. Corrected here.

=============================================================
THE V2 ROUTER IS THE CORRECT ROUTER FOR THIS BOT
=============================================================
0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43 is the OFFICIAL Aerodrome Router
(aerodrome-finance/contracts README). It is deployed, 23,582 bytes, and it DOES
support ETH-in swaps natively via swapExactETHForTokens.

Therefore the earlier recommendation to migrate to the Slipstream SwapRouter was
premature. Slipstream is concentrated-liquidity and takes WETH only (no ETH-in
entrypoint), which would ADD a wrap step. The minimal correct fix is to add the
missing 4th struct field to the existing router call.

MINIMAL FIX (preferred): add `address factory` to the Route tuple in swap.js,
pass the V2 PoolFactory 0x420DD381b31aEf6683db6B902084cB0FFECe40Da (or address(0),
which Aerodrome resolves to the default factory — verify behavior before relying on it).

=============================================================
VERIFIED ADDRESSES (all confirmed deployed)
=============================================================
-- Aerodrome V2 (vAMM/sAMM) — ETH-in capable, use this --
Router                       0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43   23,582B
PoolFactory                  0x420DD381b31aEf6683db6B902084cB0FFECe40Da    3,517B
AERO token                   0x940181a94A35A4569E4529A3CDfB74e38FD98631
FactoryRegistry              0x5C3F18F06CC09CA1910767A34a20F771039E37C0

-- Slipstream (concentrated liquidity, WETH-only) --
SwapRouter                   0xBE6D8f0d05cC4be24d5167a3eF062215bE6D18a5    9,909B
PoolFactory (CLFactory)      0x5e7BB104d84c7CB9B682AaC2F3d509f5F406809A    4,959B
NonfungiblePositionManager   0x827922686190790b37229fd06084350E74485b72   24,543B
QuoterV2                     0x254cF9E1E6e233aa1AC962CB9B05b2cfeAaE15b0    6,935B
MixedQuoter                  0x0A5aA5D3a4d28014f967Bf0f29EAA3FF9807D5c6    7,111B
SugarHelper                  0x0AD09A66af0154a84e86F761313d02d0abB6edd5    9,351B

Slipstream SwapRouter selector probe (confirmed):
  PRESENT 0xa026383e exactInputSingle((address,address,int24,address,uint256,uint256,uint256,uint160))
  PRESENT 0xc04b8d59 exactInput((bytes,address,uint256,uint256,uint256))
  absent  0x414bf389 exactInputSingle uint24 (Uniswap V3 shape — NOT this router)
  factory() -> 0x5e7bb104d84c7cb9b682aac2f3d509f5f406809a
  WETH9()   -> 0x4200000000000000000000000000000000000006

=============================================================
CLFACTORY ADDRESS — EARLIER USER-SUPPLIED VALUE WAS WRONG
=============================================================
  BAD  (from a Dune query):  0x5e7BB1040E93aa4b081977ec9A4105B8fe699B5a  -> 0 bytes
  GOOD (official repo):      0x5e7BB104d84c7CB9B682AaC2F3d509f5F406809A  -> 4,959 bytes
Shares only the first 8 hex chars. Confirmed independently by SwapRouter.factory().

=============================================================
REMAINING INDEPENDENT BUGS (unchanged, still real)
=============================================================
1. Pool IDs passed as token addresses — 2 of 16 blocked proposals carry 66-char
   GeckoTerminal pool IDs where a 20-byte token address belongs:
     Surplus / WETH  0xfc25fdd217e288d03a877f0b7d49e0bbe52b2288c88de929125062569fc7eb2a
     GITLAWB / WETH  0xec33256bf1ded407a57fd3c1965e7556e42ac14db09bc4e6fef57d5e2eb0b0b9
   Discovery must resolve pool -> baseToken.address before proposing.

2. Scientific notation upstream — proposals store amount_eth=2.683560496874402e-05.
   executor.py:35 formats with f"{amount_eth:.18f}".rstrip("0") at the call boundary,
   which fixes the swap.js arg, but e-notation still propagates in stored state.
   Historical failure: ERROR: Value `5.9429378317010284e-05` is not a valid decimal number.

3. backtest_results.json is CLOBBERED IN PLACE by the recurring cron. Two reads ~20min
   apart returned 12 rows/21 trades/+7.11% and 11 rows/18 trades/+7.87%. Any evidence
   gate reading this file is judging a moving target. Write timestamped outputs.

=============================================================
SAFE NEXT STEPS — ALL READ-ONLY, ZERO GAS
=============================================================
1. Fix the Route struct arity in swap.js (add address factory field).
2. Fix discovery to emit token addresses, not pool IDs.
3. eth_call getAmountsOut (selector 0x5509a1ac) to confirm a real quote comes back
   for a liquid pair (KTA or cbBTC). Free, no broadcast.
4. eth_call SIMULATE swapExactETHForTokens (0x903638a4) — proves calldata is accepted.
5. Only after 3 and 4 return clean, consider a micro real swap.

GATE STATUS UNCHANGED: kill_switch=true, live_trading=false, edge_evidence.pass=false.
Nothing signed, nothing broadcast, no file in the bot modified.
