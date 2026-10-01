Aerodrome Slipstream (Base) — ON-CHAIN VERIFIED ADDRESSES
Verified 2026-08-20 via eth_getCode + eth_call against https://mainnet.base.org

== VERIFIED CORRECT ==

Slipstream SwapRouter   0xbe6d8f0d05cc4be24d5167a3ef062215be6d18a5   9,909 bytes  DEPLOYED
  Selector probe:
    PRESENT  0xa026383e  exactInputSingle((address,address,int24,address,uint256,uint256,uint256,uint160))
    PRESENT  0xc04b8d59  exactInput((bytes,address,uint256,uint256,uint256))
    absent   0x414bf389  exactInputSingle uint24 (Uniswap V3 shape)
    absent   0x67ffb66a  swapExactETHForTokens (V2 shape)
  Self-reported config (eth_call):
    factory() -> 0x5e7bb104d84c7cb9b682aac2f3d509f5f406809a
    WETH9()   -> 0x4200000000000000000000000000000000000006

NonfungiblePositionManager  0x827922686190790b37229fd06084350e74485b72  24,543 bytes  DEPLOYED

== CORRECTION: CLFactory address in the provided list is WRONG ==

  PROVIDED (bad):  0x5e7BB1040E93aa4b081977ec9A4105B8fe699B5a  -> 0 bytes, NO CONTRACT
  ACTUAL (good):   0x5e7bb104d84c7cb9b682aac2f3d509f5f406809a  <- returned by SwapRouter.factory()

  The two share only the first 6 hex chars ("5e7bb104"). The provided value appears to be a
  corrupted/mistranscribed variant. Source was a Dune query, not official docs. Do not use it.

== THE ACTUAL FIX FOR swap.js ==

Current swap.js is wrong on BOTH the address and the ABI:
  Router hardcoded: 0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43  (23,582 bytes, deployed)
    -> probed for exactInputSingle(int24), exactInputSingle(uint24), exactInput,
       swapExactETHForTokens, multicall: ALL ABSENT.
    -> This contract exposes none of the swap entrypoints swap.js calls. Every swap
       reverts. This is the true root cause, NOT "no accessible liquidity".
  ABI declared: swapExactETHForTokens(uint256,(address,address,bool)[],address,uint256)
    -> V2 vAMM/sAMM shape. Slipstream is concentrated-liquidity and does not expose it.

Required changes:
  1. Router -> 0xbe6d8f0d05cc4be24d5167a3ef062215be6d18a5
  2. ABI    -> exactInputSingle with an int24 tickSpacing field (NOT uint24 fee).
               Struct: (tokenIn, tokenOut, tickSpacing, recipient, amountIn,
                        amountOutMinimum, deadline?, sqrtPriceLimitX96)
               NOTE: the int24 tickSpacing is the key difference from Uniswap V3.
               Pool identity in Slipstream is (token0, token1, tickSpacing),
               so the bot must supply tickSpacing, not a fee tier.
  3. ETH input -> Slipstream router takes WETH, not raw ETH. There is no
                  swapExactETHForTokens equivalent, so the bot must either
                  wrap ETH -> WETH first, or use a multicall/peripheral path.
                  multicall(uint256,bytes[]) probed ABSENT on this router, so
                  confirm the real multicall signature before relying on it.

== TWO ADDITIONAL BUGS FOUND IN risk_state.json (independent of the router) ==

  1. Malformed token_out: 2 of 16 blocked proposals pass 66-char values where a
     20-byte address belongs -- GeckoTerminal POOL IDs, not token contracts:
       Surplus / WETH  -> 0xfc25fdd217e288d03a877f0b7d49e0bbe52b2288c88de929125062569fc7eb2a
       GITLAWB / WETH  -> 0xec33256bf1ded407a57fd3c1965e7556e42ac14db09bc4e6fef57d5e2eb0b0b9
     Discovery must resolve pool -> baseToken.address before proposing a swap.

  2. Scientific notation at proposal time: blocked proposals carry
     amount_eth = 2.683560496874402e-05. executor.py:35 now formats with
     f"{amount_eth:.18f}".rstrip("0"), which fixes it at the call boundary, but the
     value is still stored/propagated in e-notation upstream. Confirmed prior failure:
       swap.js failed: ERROR: Value `5.9429378317010284e-05` is not a valid decimal number.

== SAFE NEXT STEPS (all read-only, zero gas) ==

  1. Pull the verified Slipstream SwapRouter ABI from Basescan for
     0xbe6d8f0d05cc4be24d5167a3ef062215be6d18a5
  2. Fix discovery to emit token addresses, not pool IDs
  3. Determine correct tickSpacing per target pool via CLFactory
     0x5e7bb104d84c7cb9b682aac2f3d509f5f406809a
  4. eth_call SIMULATE the swap calldata -- proves acceptance with no broadcast, no gas
  5. Only after a clean simulation, consider a micro real swap

DO NOT run a real "tiny test swap" against the old router. It would revert and waste gas.

GATE STATUS UNCHANGED: kill_switch=true, live_trading=false, edge_evidence.pass=false.
Nothing signed, nothing broadcast, caps.json untouched.
