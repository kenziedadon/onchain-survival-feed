#!/usr/bin/env node
/**
 * buyPathVerifier.js — verify ETH → tokenOut can execute before we approve/swap.
 *
 * Mirrors sellPathVerifier.js but walks the buy direction.
 * All read-only; no writes, no gas.
 *
 * CLI:
 *   node buyPathVerifier.js <tokenOut> <amountEthWei> <user> → JSON on stdout
 *
 * Return shape:
 *   success: { verified: true, router, minOut }
 *   failure: { verified: false, error, failedRoutes: [{route, errorKind, reason}] }
 *
 * errorKind: "infra_error" | "rejected" | "no_route"
 */
const { http, createPublicClient, encodeFunctionData, parseEther, formatEther, decodeFunctionResult, decodeAbiParameters, getAddress } = require('viem');
const base = (require('viem/chains')).base;
const { getBestClient } = require('./rpcClient');

function emit(obj) {
  if (!process.stdout.isTTY) console.log(JSON.stringify(obj));
  else console.log(JSON.stringify(obj, null, 2));
  process.exit(obj.verified === true ? 0 : 2);
}

async function withRetry(fn, label, retries = 2, delayMs = 500) {
  let lastErr;
  for (let i = 0; i < retries; i++) {
    try { return { ok: true, value: await fn() }; }
    catch (e) {
      lastErr = e;
      const msg = String(e.message || '').toLowerCase();
      const retryable = msg.includes('timeout') || msg.includes('429') || msg.includes('5xx') || msg.includes('econnreset') || msg.includes('network');
      if (i < retries - 1 && retryable) {
        await new Promise(r => setTimeout(r, delayMs * (i + 1)));
        continue;
      }
      return { ok: false, error: lastErr };
    }
  }
  return { ok: false, error: lastErr };
}

async function fetchQuote({ user, originCurrency, destinationCurrency, amount, tradeType = 'EXACT_INPUT', slippageBps = 100 }) {
  const bodies = [
    { user, recipient: user, originChainId: 8453, destinationChainId: 8453, originCurrency, destinationCurrency, amount, tradeType, slippageBps },
  ];
  const urls = ['https://api.relay.link/quote', 'https://api.relay.link/quote/v2'];
  const headers = { 'Accept': 'application/json', 'Content-Type': 'application/json', 'User-Agent': 'base-trend-bot/1.0' };
  let lastErr;

  for (const url of urls) {
    const attempt = async () => {
      const res = await fetch(url, { method: 'POST', headers, body: JSON.stringify(bodies[0]) });
      const text = await res.text();
      let data;
      try { data = JSON.parse(text); } catch { data = { raw: text }; }
      if (!res.ok) {
        const err = new Error(`Relay HTTP ${res.status}: ${text.slice(0, 200)}`);
        err.status = res.status;
        err.relayReason = data?.error || data?.message || text.slice(0, 120);
        err.retryable = res.status === 429 || res.status >= 500;
        throw err;
      }
      if (data?.error || data?.errors) {
        const err = new Error(data.error || JSON.stringify(data.errors).slice(0, 200));
        err.relayReason = data.error || data.errors?.[0] || 'unknown relay error';
        err.retryable = false;
        throw err;
      }
      return data;
    };
    const r = await withRetry(attempt, `relay:${url}`);
    lastErr = r.error;
    if (r.ok) return { ok: true, value: r.value };
    if (r.error && r.error.status >= 400 && r.error.status < 500 && r.error.status !== 429) continue;
    return r;
  }
  return { ok: false, error: lastErr || new Error('Relay: no usable quote endpoint') };
}

async function checkAerodrome(publicClient, tokenOut, amountEthWei) {
  try {
    const WETH = '0x4200000000000000000000000000000000000006';
    const NATIVE = '0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE'; // not used directly; router accepts msg.value
    const ROUTER = '0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43';
    const FACTORY = '0x420DD381b31aEf6683db6B902084cB0FFECe40Da';
    const abi = [{ name: 'getAmountsOut', type: 'function', stateMutability: 'view',
      inputs: [{ type: 'uint256' }, { components: [{ name: 'from', type: 'address' }, { name: 'to', type: 'address' }, { name: 'stable', type: 'bool' }, { name: 'factory', type: 'address' }], type: 'tuple[]' }],
      outputs: [{ type: 'uint256[]' }] }];
    const data = encodeFunctionData({ abi, functionName: 'getAmountsOut', args: [amountEthWei, [{ from: WETH, to: tokenOut, stable: false, factory: FACTORY }]] });
    // Try direct WETH -> token, then two-hop WETH -> USDC -> token
    const USDC = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913';
    const routeSets = [
      [{ from: WETH, to: tokenOut, stable: false, factory: FACTORY }],
      [{ from: WETH, to: USDC, stable: true, factory: FACTORY },
       { from: USDC, to: tokenOut, stable: false, factory: FACTORY }],
    ];
    for (const routes of routeSets) {
      const qData = encodeFunctionData({ abi, functionName: 'getAmountsOut', args: [amountEthWei, routes] });
      let r;
      try {
        r = await publicClient.call({ to: ROUTER, data: qData });
      } catch (e) {
        continue;
      }
      if (!r || !r.data || r.data === '0x') continue;
      let amounts;
      try {
        const out = decodeAbiParameters([{ type: 'uint256[]' }], r.data);
        amounts = out[0];
      } catch (e) {
        continue;
      }
      if (!amounts || amounts.length === 0) continue;
      const outAmt = amounts[amounts.length - 1];
      if (outAmt === 0n) continue;
      return { ok: true, minOut: outAmt };
    }
    return { ok: false, error: new Error('no aerodrome route (direct or two-hop via USDC)') };
  } catch (e) {
    return { ok: false, error: e };
  }
}

async function checkUniswapV3(publicClient, tokenOut, amountEthWei) {
  try {
    const WETH = '0x4200000000000000000000000000000000000006';
    const QUOTER = '0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a';
    const abi = [{ name: 'quoteExactInputSingle', type: 'function', stateMutability: 'view',
      inputs: [{ components: [{ name: 'tokenIn', type: 'address' }, { name: 'tokenOut', type: 'address' }, { name: 'amountIn', type: 'uint256' }, { name: 'amountOutMinimum', type: 'uint256' }, { name: 'fee', type: 'uint24' }, { name: 'sqrtPriceLimitX96', type: 'uint160' }], type: 'tuple' }],
      outputs: [{ type: 'uint256' }] }];
    // Try common Uniswap V3 fee tiers: 10000 (1%), 3000 (0.3%), 500 (0.05%), 100 (0.01%), and 2500 (2.5% for micro-caps)
    for (const fee of [10000, 3000, 500, 100, 2500]) {
      try {
        const data = encodeFunctionData({ abi, functionName: 'quoteExactInputSingle', args: [{ tokenIn: WETH, tokenOut, amountIn: amountEthWei, amountOutMinimum: 0n, fee, sqrtPriceLimitX96: 0n }] });
        const r = await publicClient.call({ to: QUOTER, data });
        const out = decodeFunctionResult({ abi, functionName: 'quoteExactInputSingle' }, r.data);
        if (out[0] === 0n) continue;
        return { ok: true, minOut: out[0] };
      } catch (e) {
        continue;
      }
    }
    return { ok: false, error: new Error('no Uniswap V3 pool on tiers 10000/3000/500/100/2500') };
  } catch (e) {
    return { ok: false, error: e };
  }
}

async function checkAerodromeSlipstream(publicClient, tokenOut, amountEthWei) {
  try {
    const WETH = '0x4200000000000000000000000000000000000006';
    // Aerodrome Slipstream (V3) uses Uniswap V3 style QuoterV2 / Quoter on Base
    const QUOTER = '0x3F1c54b68e053531b790d9842F61601a07011984'; // Slipstream Quoter V2
    const abi = [{ name: 'quoteExactInputSingle', type: 'function', stateMutability: 'view',
      inputs: [{ components: [{ name: 'tokenIn', type: 'address' }, { name: 'tokenOut', type: 'address' }, { name: 'amountIn', type: 'uint256' }, { name: 'amountOutMinimum', type: 'uint256' }, { name: 'fee', type: 'uint24' }, { name: 'sqrtPriceLimitX96', type: 'uint160' }], type: 'tuple' }],
      outputs: [{ type: 'uint256' }] }];
    // Slipstream common ticks / fee tiers: 100, 500, 3000, 10000
    for (const fee of [100, 500, 3000, 10000]) {
      try {
        const data = encodeFunctionData({ abi, functionName: 'quoteExactInputSingle', args: [{ tokenIn: WETH, tokenOut, amountIn: amountEthWei, amountOutMinimum: 0n, fee, sqrtPriceLimitX96: 0n }] });
        const r = await publicClient.call({ to: QUOTER, data });
        const out = decodeFunctionResult({ abi, functionName: 'quoteExactInputSingle' }, r.data);
        if (out[0] === 0n) continue;
        return { ok: true, minOut: out[0] };
      } catch (e) {
        continue;
      }
    }
    return { ok: false, error: new Error('no Slipstream pool on tiers 100/500/3000/10000') };
  } catch (e) {
    return { ok: false, error: e };
  }
}

async function checkRelayBuy(user, tokenOut, amountEthWei) {
  // Relay on Base requires WETH as originCurrency for same-chain buys —
  // the native-ETH sentinel is rejected with INVALID_INPUT_CURRENCY.
  const WETH_ORIGIN = '0x4200000000000000000000000000000000000006';
  const r = await fetchQuote({ user, originCurrency: WETH_ORIGIN, destinationCurrency: tokenOut, amount: amountEthWei.toString(), tradeType: 'EXACT_INPUT', slippageBps: 100 });
  if (!r.ok) {
    const e = r.error;
    let errorKind = 'infra_error';
    if (e && e.status && e.status >= 400 && e.status < 500 && e.status !== 429) errorKind = 'rejected';
    else if (e && /no route|invalid input|invalid output|unsupported/i.test(String(e.relayReason || e.message))) errorKind = 'no_route';
    else if (e && e.retryable) errorKind = 'infra_error';
    return { ok: false, error: e, errorKind, reason: e.relayReason || e.message || 'Relay quote failed' };
  }
  try {
    const steps = r.value?.steps?.[0];
    const outMin = BigInt(String(steps?.execute?.targetAmount || 0));
    return { ok: true, minOut: outMin };
  } catch (e) {
    return { ok: false, error: e, errorKind: 'infra_error', reason: 'Relay parse failed' };
  }
}

async function verifyBuyPath({ publicClient, tokenOut, amountEthWei, user }) {
  const routes = [
    { name: 'aerodrome', run: () => checkAerodrome(publicClient, tokenOut, amountEthWei) },
    { name: 'aerodromeSlipstream', run: () => checkAerodromeSlipstream(publicClient, tokenOut, amountEthWei) },
    { name: 'uniswapV3', run: () => checkUniswapV3(publicClient, tokenOut, amountEthWei) },
    { name: 'relay', run: () => checkRelayBuy(user, tokenOut, amountEthWei) },
  ];

  const results = [];
  const failedRoutes = [];
  for (const route of routes) {
    try {
      const r = await route.run();
      if (!r.ok) {
        failedRoutes.push({ route: route.name, errorKind: r.errorKind || 'no_route', reason: r.reason || r.error?.message || 'no route' });
      } else {
        results.push({ route: route.name, minOut: r.minOut?.toString() });
      }
    } catch (e) {
      failedRoutes.push({ route: route.name, errorKind: 'infra_error', reason: String(e.message).slice(0, 200) });
    }
  }

  if (failedRoutes.length === routes.length) {
    emit({ verified: false, error: 'all buy routes failed', failedRoutes });
  }
  const best = results.sort((a, b) => (b.minOut > a.minOut ? 1 : -1))[0];
  emit({ verified: true, router: best.route, minOut: best.minOut, results });
}

async function main() {
  const [rawTokenOut, amountEthWei, user, maybeRpc] = process.argv.slice(2);
  if (!rawTokenOut || !amountEthWei) { console.error('Usage: node buyPathVerifier.js <tokenOut> <amountEthWei> <user> [--rpc <url>]'); process.exit(1); }
  // Normalize to EIP-55 checksum so viem's strict address check accepts
  // lowercase / mixed-case input (matches swap.js getAddress() behavior).
  const tokenOut = getAddress(rawTokenOut);
  if (maybeRpc === '--rpc' && process.argv[4]) process.env.EXECUTION_RPC_URL = process.argv[4];
  try {
    const { publicClient } = await getBestClient();
    await verifyBuyPath({ publicClient, tokenOut, amountEthWei: BigInt(amountEthWei), user: user || '0x0000000000000000000000000000000000000000' });
  } catch (e) {
    emit({ verified: false, error: `fatal: ${String(e.message).slice(0, 200)}`, failedRoutes: [] });
  }
}
main();
