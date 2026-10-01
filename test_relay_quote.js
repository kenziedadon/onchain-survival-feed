#!/usr/bin/env node
/**
 * Quick Relay quote sanity check — no execution.
 * Tests:
 *  1. Ordinary Base swap: WETH -> USDC
 *  2. Actual failing case: Surplus -> WETH
 */

const { relaySwapWithFallback } = require('./relayFallback.js');

const WETH = '0x4200000000000000000000000000000000000006';
const USDC = '0x833589fcd6edb6e08f4c7c32d4f71b54bda02913';
const SURPLUS = '0xd9aaab39e305cd1f0c6defd5aad6bb2c0e0d3b5e'; // from risk_state history

async function main() {
  console.log('=== Test 1: WETH -> USDC (ordinary pair) ===');
  try {
    const r1 = await relaySwapWithFallback({
      user: '0xe7a16Be2aF1eCE8922d9d3aD55C03e29004e540F',
      account: { address: '0xe7a16Be2aF1eCE8922d9d3aD55C03e29004e540F' },
      chainId: 8453,
      currencyIn: WETH,
      currencyOut: USDC,
      amount: (1e18).toString(),
      tradeType: 'EXACT_INPUT',
      slippageBps: 100,
      quoteOnly: true,
    });
    console.log('Result:', JSON.stringify(r1, null, 2));
  } catch (e) {
    console.log('Error:', e.message);
    if (e.quote) console.log('Partial quote:', JSON.stringify(e.quote, null, 2).slice(0, 1000));
  }

  console.log('\n=== Test 2: Surplus -> WETH (actual failing case) ===');
  try {
    const r2 = await relaySwapWithFallback({
      user: '0xe7a16Be2aF1eCE8922d9d3aD55C03e29004e540F',
      account: { address: '0xe7a16Be2aF1eCE8922d9d3aD55C03e29004e540F' },
      chainId: 8453,
      currencyIn: SURPLUS,
      currencyOut: WETH,
      amount: (1e18).toString(),
      tradeType: 'EXACT_INPUT',
      slippageBps: 100,
      quoteOnly: true,
    });
    console.log('Result:', JSON.stringify(r2, null, 2));
  } catch (e) {
    console.log('Error:', e.message);
    if (e.quote) console.log('Partial quote:', JSON.stringify(e.quote, null, 2).slice(0, 1000));
  }
}

main().catch(e => {
  console.error('Fatal:', e.message);
  process.exit(1);
});
