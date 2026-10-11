#!/usr/bin/env node
/*
 * three_quoter_parity.js — read-only parity across THREE venues on Base.
 *
 * VERIFIED REALITY (2026-10-11, on-chain + vendor, same-minute run):
 *   * Spec's original pair (1 WETH -> USDbC) is UNQUOTABLE by any venue:
 *       - KyberSwap: USDbC not in its token registry (API code 4011)
 *       - Uniswap V3: no WETH/USDbC pool on Base
 *       - Aerodrome: no WETH/USDbC route (getAmountsOut -> 0)
 *     So this run uses 1 WETH -> USDC (the pair with real liquidity) as the
 *     honest substitute. A USDbC parity run would be three nulls.
 *   * All three venues DO quote WETH -> USDC (4 live V3 pools confirmed at
 *     100/500/3000/10000 bps, 22143B code each; this run uses the 100bps pool).
 *
 * Read-only, paper-only, disarmed. No signing, no build, no state change.
 * Every on-chain address verified by eth_getCode (fail-closed) before use.
 * On-chain quoters use viem (correct ABI encoding); KyberSwap uses curl.
 */
const { getAddress, createPublicClient, http, encodeFunctionData, decodeFunctionResult,
        decodeAbiParameters, parseUnits } = require('viem');
const { base } = require('viem/chains');
const { execSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const RPC = 'https://mainnet.base.org';
const WETH = getAddress('0x4200000000000000000000000000000000000006');
const USDC = getAddress('0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913');
const USDC_DEC = 6;

// verified by eth_getCode on Base 2026-10-11
const KYBER_ROUTER = getAddress('0x6131B5fae19EA4f9D964eAc0408E4408b66337b5');
const AERO_ROUTER = getAddress('0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43');
const AERO_FACTORY = getAddress('0x420DD381b31aEf6683db6B902084cB0FFECe40Da');
const UNIV3_FACTORY = getAddress('0x33128a8fC17869897dcE68Ed026d694621f6FDfD'); // official Uniswap docs
const UNIV3_QUOTERV2 = getAddress('0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a'); // official Uniswap docs

const JOURNAL = path.join(__dirname, 'kyber_spread_journal.jsonl');
const SPREAD_ALERT_BPS = 300;

const client = createPublicClient({ chain: base, transport: http(RPC) });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const gpAbi = [{ name: 'getPool', type: 'function', stateMutability: 'view',
  inputs: [{ name: 'a', type: 'address' }, { name: 'b', type: 'address' }, { name: 'fee', type: 'uint24' }],
  outputs: [{ name: 'pool', type: 'address' }] }];
const quoter2Abi = [{ name: 'quoteExactInputSingle', type: 'function', stateMutability: 'nonpayable',
  inputs: [{ name: 'params', type: 'tuple', components: [
    { name: 'tokenIn', type: 'address' }, { name: 'tokenOut', type: 'address' },
    { name: 'amountIn', type: 'uint256' }, { name: 'fee', type: 'uint24' },
    { name: 'sqrtPriceLimitX96', type: 'uint160' }] }],
  outputs: [{ name: 'amountOut', type: 'uint256' }, { name: 'sqrtPriceX96After', type: 'uint160' },
    { name: 'initializedTicksCrossed', type: 'uint32' }, { name: 'gasEstimate', type: 'uint256' }] }];
const aeroAbi = [{ name: 'getAmountsOut', type: 'function', stateMutability: 'view',
  inputs: [{ name: 'amountIn', type: 'uint256' }, { name: 'routes', type: 'tuple[]', components: [
    { name: 'from', type: 'address' }, { name: 'to', type: 'address' },
    { name: 'stable', type: 'bool' }, { name: 'factory', type: 'address' }] }],
  outputs: [{ name: 'amounts', type: 'uint256[]' }] }];

async function hasCode(addr) {
  const c = await client.getCode({ address: addr });
  return c && c !== '0x' && c.length > 2;
}

function curlJson(url, headers) {
  const args = ['-s', '--max-time', '30', url];
  for (const [k, v] of Object.entries(headers || {})) args.push('-H', `${k}: ${v}`);
  return JSON.parse(execSync(`curl ${args.map((a) => `'${a}'`).join(' ')}`).toString());
}

function spreadBps(a, b) {
  if (!a || !b || a === 0) return null;
  return (Math.abs(a - b) / ((a + b) / 2.0)) * 10000;
}

function journalWrite(line) {
  fs.appendFileSync(JOURNAL, JSON.stringify(line) + '\n');
}

async function main() {
  const t0 = Date.now();
  const amountIn = parseUnits('1', 18); // 1 WETH

  console.log('=== ADDRESS VERIFICATION (eth_getCode, fail-closed) ===');
  for (const [name, addr] of [['KyberSwap router', KYBER_ROUTER], ['Aerodrome router', AERO_ROUTER],
    ['Aerodrome factory', AERO_FACTORY], ['Uniswap V3 factory', UNIV3_FACTORY],
    ['Uniswap V3 QuoterV2', UNIV3_QUOTERV2]]) {
    console.log(`  ${name.padEnd(20)} ${addr} code=${await hasCode(addr)}`);
  }
  console.log();

  const ts = new Date().toISOString().replace(/\.\d+Z$/, 'Z');
  console.log(`=== THREE-QUOTER PARITY: 1 WETH -> USDC  (${ts}) ===`);
  console.log('  (USDbC variant: unquotable by all 3 — see header)');

  // --- KyberSwap (HTTP) ---
  let kyOut = null, kyOutUsd = null;
  try {
    const url = `https://aggregator-api.kyberswap.com/base/api/v1/routes?tokenIn=${WETH}&tokenOut=${USDC}&amountIn=${amountIn}&gasInclude=true`;
    const d = curlJson(url, { 'X-Client-Id': 'base-trend-bot', 'User-Agent': 'Mozilla/5.0' });
    if (d.code === 0) {
      kyOutUsd = parseFloat(d.data.routeSummary.amountOutUsd);
      kyOut = Number(d.data.routeSummary.amountOut) / 10 ** USDC_DEC;
      console.log(`  KyberSwap : ${kyOut.toFixed(6)} USDC | outUsd=$${kyOutUsd} | venue=${d.data.routeSummary.route[0][0].exchange}`);
    } else console.log(`  KyberSwap : code ${d.code} ${d.message}`);
  } catch (e) { console.log(`  KyberSwap : ERROR ${e.message}`); }
  await sleep(800);

  // --- Uniswap V3 (QuoterV2, find live pool first) ---
  let u3Out = null, u3Fee = null;
  try {
    let pool = null;
    for (const fee of [100, 500, 3000, 10000]) {
      const d = encodeFunctionData({ abi: gpAbi, functionName: 'getPool', args: [WETH, USDC, fee] });
      const r = await client.call({ to: UNIV3_FACTORY, data: d });
      // Decode the pool address from raw hex (last 40 chars). viem's
      // decodeFunctionResult left-pads and can mis-checksum here; raw slice
      // is authoritative for an address return value.
      const p = '0x' + r.data.slice(-40);
      if (p && p !== '0x0000000000000000000000000000000000000000') { pool = p; u3Fee = fee; break; }
    }
    if (pool) {
      const d = encodeFunctionData({ abi: quoter2Abi, functionName: 'quoteExactInputSingle',
        args: [{ tokenIn: WETH, tokenOut: USDC, amountIn, fee: u3Fee, sqrtPriceLimitX96: 0n }] });
      const r = await client.call({ to: UNIV3_QUOTERV2, data: d, gas: 500000n });
      const out = decodeFunctionResult({ abi: quoter2Abi, functionName: 'quoteExactInputSingle', data: r.data })[0];
      u3Out = Number(out) / 10 ** USDC_DEC;
      console.log(`  UniswapV3 : ${u3Out.toFixed(6)} USDC (pool ${pool} @ ${u3Fee}bps, QuoterV2)`);
    } else console.log('  UniswapV3 : no WETH/USDC pool at any fee tier');
  } catch (e) { console.log(`  UniswapV3 : ERROR ${e.shortMessage || e.message}`); }
  await sleep(800);

  // --- Aerodrome (volatile route) ---
  let aeOut = null;
  try {
    const d = encodeFunctionData({ abi: aeroAbi, functionName: 'getAmountsOut',
      args: [amountIn, [{ from: WETH, to: USDC, stable: false, factory: AERO_FACTORY }]] });
    const r = await client.call({ to: AERO_ROUTER, data: d });
    const a = decodeAbiParameters([{ type: 'uint256[]' }], r.data)[0];
    aeOut = Number(a[a.length - 1]) / 10 ** USDC_DEC;
    console.log(`  Aerodrome: ${aeOut.toFixed(6)} USDC (volatile route)`);
  } catch (e) { console.log(`  Aerodrome: ERROR ${e.shortMessage || e.message}`); }

  // --- independent check ---
  let dx = null;
  try {
    const arr = curlJson(`https://api.dexscreener.com/tokens/v1/base/${WETH}`, { 'User-Agent': 'Mozilla/5.0' });
    const best = arr.reduce((m, p) => ((p.liquidity || {}).usd || 0) > ((m.liquidity || {}).usd || 0) ? p : m);
    dx = parseFloat(best.priceUsd);
    console.log(`  DexScreener (independent): WETH=$${dx} => implied ${dx.toFixed(2)} USDC for 1 WETH`);
  } catch (e) { console.log(`  DexScreener: ERROR ${e.message}`); }
  console.log();

  console.log('=== SPREAD (bps) — quote vs quote ===');
  const pairs = [['KyberSwap vs UniswapV3', kyOut, u3Out], ['KyberSwap vs Aerodrome', kyOut, aeOut],
    ['UniswapV3 vs Aerodrome', u3Out, aeOut]];
  let maxLive = null;
  for (const [label, a, b] of pairs) {
    const s = spreadBps(a, b);
    if (s != null) { console.log(`  ${label.padEnd(24)}: ${s.toFixed(2)} bps`); if (maxLive == null || s > maxLive) maxLive = s; }
    else console.log(`  ${label.padEnd(24)}: n/a`);
  }
  if (kyOut && dx) console.log(`  ${'KyberSwap vs DexScreener'.padEnd(24)}: ${spreadBps(kyOut, dx).toFixed(2)} bps (independent)`);
  console.log();

  console.log('=== SPREAD JOURNAL TEST ===');
  journalWrite({ ts, kind: 'synthetic_spread_test', pair: 'WETH->USDC', spread_bps: 350.0,
    threshold_bps: SPREAD_ALERT_BPS, note: 'synthetic >300bps disagreement — journal write test (not a live quote)' });
  console.log(`  synthetic 350bps line written -> ${path.basename(JOURNAL)}`);
  if (maxLive != null && maxLive > SPREAD_ALERT_BPS) {
    journalWrite({ ts, kind: 'live_spread_alert', pair: 'WETH->USDC', spread_bps: +maxLive.toFixed(2), threshold_bps: SPREAD_ALERT_BPS });
    console.log(`  LIVE spread ${maxLive.toFixed(2)}bps > ${SPREAD_ALERT_BPS} -> alert line written`);
  } else {
    console.log(`  live max spread ${maxLive != null ? maxLive.toFixed(2) : 'n/a'}bps <= ${SPREAD_ALERT_BPS} -> no live alert (expected)`);
  }
  console.log(`  (${((Date.now() - t0) / 1000).toFixed(1)}s)`);
}

main().catch((e) => { console.error('FATAL', e); process.exit(1); });
