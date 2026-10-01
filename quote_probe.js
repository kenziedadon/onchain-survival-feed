#!/usr/bin/env node
/**
 * quote_probe.js — detect which quote token (WETH vs USDC) gives a real route
 * for a given token on Aerodrome. Used by executor.py to pick the right route.
 *
 * Usage: node quote_probe.js <tokenAddr>
 * Prints: WETH | USDC | NONE
 */
const { createPublicClient, http, encodeFunctionData, decodeAbiParameters, parseEther } = require('viem');
const { base } = require('viem/chains');

const AERO_ROUTER = '0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43';
const WETH = '0x4200000000000000000000000000000000000006';
const USDC = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913';
const FACTORY = '0x420DD381b31aEf6683db6B902084cB0FFECe40Da';

const AERO_ABI = [{
  name: 'getAmountsOut', type: 'function', stateMutability: 'view',
  inputs: [
    { name: 'amountIn', type: 'uint256' },
    { name: 'routes', type: 'tuple[]', components: [
      { name: 'from', type: 'address' },
      { name: 'to', type: 'address' },
      { name: 'stable', type: 'bool' },
      { name: 'factory', type: 'address' },
    ]},
  ],
  outputs: [{ name: 'amounts', type: 'uint256[]' }],
}];

const RPCS = [
  'https://mainnet.base.org',
  'https://base.llamarpc.com',
  'https://base.drpc.org',
  'https://1rpc.io/base',
  'https://base-pokt.nodies.app',
];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function probe(client, tokenOut, tokenIn, amountIn) {
  const data = encodeFunctionData({
    abi: AERO_ABI,
    functionName: 'getAmountsOut',
    args: [amountIn, [{ from: tokenIn, to: tokenOut, stable: false, factory: FACTORY }]],
  });
  const res = await client.call({ to: AERO_ROUTER, data });
  if (!res.data || res.data === '0x') return 0n;
  const a = decodeAbiParameters([{ type: 'uint256[]' }], res.data)[0];
  return a[a.length - 1];
}

async function probeTwoHop(client, tokenOut, amountIn) {
  // WETH -> USDC (stable) -> tokenOut (volatile). Covers USDC-quoted pools.
  const data = encodeFunctionData({
    abi: AERO_ABI,
    functionName: 'getAmountsOut',
    args: [amountIn, [
      { from: WETH, to: USDC, stable: true, factory: FACTORY },
      { from: USDC, to: tokenOut, stable: false, factory: FACTORY },
    ]],
  });
  const res = await client.call({ to: AERO_ROUTER, data });
  if (!res.data || res.data === '0x') return 0n;
  const a = decodeAbiParameters([{ type: 'uint256[]' }], res.data)[0];
  return a[a.length - 1];
}

async function withRpc(fn) {
  let last;
  for (const url of RPCS) {
    try {
      const c = createPublicClient({ chain: base, transport: http(url, { timeout: 12_000 }) });
      return await fn(c);
    } catch (e) { last = e; }
  }
  throw last;
}

(async () => {
  const tokenOut = process.argv[2];
  if (!tokenOut) { console.log('NONE'); process.exit(1); }

  const amt = parseEther('0.001');
  try {
    // 1. Direct WETH -> token route
    const weth = await withRpc((c) => probe(c, tokenOut, WETH, amt));
    await sleep(300);
    if (weth > 0n) { console.log('WETH'); process.exit(0); }
    // 2. Direct USDC -> token route (0.001 ETH ≈ few USDC; probe with 1 USDC / 6dp units)
    const usdc = await withRpc((c) => probe(c, tokenOut, USDC, 1000000n));
    if (usdc > 0n) { console.log('USDC'); process.exit(0); }
    await sleep(300);
    // 3. Two-hop WETH -> USDC -> token (USDC-quoted pools with no direct WETH pool)
    const twoHop = await withRpc((c) => probeTwoHop(c, tokenOut, amt));
    if (twoHop > 0n) { console.log('USDC'); process.exit(0); }  // two-hop buys start from ETH, swap.js handles it
    console.log('NONE');
  } catch (e) {
    console.log('NONE');
  }
})();
