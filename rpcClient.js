#!/usr/bin/env node
/**
 * rpcClient.js — shared RPC client factory with fallback, caching, gas strategy.
 *
 * Provides:
 *   getBestClient(opts) -> { publicClient, walletClient?, chainId, rpcUrl }
 *   gasStrategy(publicClient, opts) -> { gasPrice, maxFeePerGas, maxPriorityFeePerGas, type }
 *
 * Env:
 *   EXECUTION_RPC_URL            primary
 *   FALLBACK_RPC_URLS            comma-separated fallbacks
 *   GAS_PRICE_MULTIPLIER         float, default 1.1
 *   MAX_PRIORITY_FEE_GWEI        float, default 0.1
 *   MAX_FEE_PER_GAS_GWEI         sanity cap, default 100
 */
const { http, createPublicClient, createWalletClient, formatEther, parseEther, encodeFunctionData } = require('viem');
const base = (require('viem/chains')).base;

let cached = null;

function getEnvList(name, fallback) {
  const raw = process.env[name];
  if (!raw) return fallback;
  return raw.split(',').map(s => s.trim()).filter(Boolean);
}

async function probeClient(url) {
  const transport = http(url);
  const client = createPublicClient({ chain: base, transport });
  // fast health check: getBlockNumber + chainId
  const [block, chainId] = await Promise.all([
    client.getBlockNumber().catch(e => { throw new Error(`block probe failed: ${e.message}`); }),
    client.getChainId().catch(e => { throw new Error(`chainId probe failed: ${e.message}`); }),
  ]);
  if (chainId !== 8453) {
    throw new Error(`wrong chain: got ${chainId}, want 8453 (Base)`);
  }
  return { client, url, chainId, block };
}

/**
 * Get or create a cached working client.
 * Tries EXECUTION_RPC_URL first, then FALLBACK_RPC_URLS, then built-in defaults.
 */
async function getBestClient(opts = {}) {
  if (cached) return cached;

  const primary = process.env.EXECUTION_RPC_URL;
  const fallbacks = getEnvList('FALLBACK_RPC_URLS', [
    'https://1rpc.io/base',
    'https://base-pokt.nodies.app',
    'https://rpc.ankr.com/base',
    'https://mainnet.base.org',
    'https://base.llamarpc.com',
    'https://base.meowrpc.com',
  ]);

  const candidates = [];
  if (primary) candidates.push(primary);
  candidates.push(...fallbacks);

  let lastErr;
  for (const url of candidates) {
    try {
      const result = await probeClient(url);
      const walletClient = opts.privateKey ? createWalletClient({ chain: base, transport: http(url), account: opts.privateKey }) : null;
      const out = { publicClient: result.client, walletClient, chainId: result.chainId, rpcUrl: url, block: result.block };
      cached = out;
      return out;
    } catch (e) {
      lastErr = e;
      // try next
    }
  }
  throw new Error(`No usable Base RPC. Last error: ${lastErr?.message || 'none tried'}`);
}

function clearCache() {
  cached = null;
}

/**
 * Gas pricing strategy.
 * Returns an object with fields compatible with viem's sendTransaction:
 *   - gasPrice (legacy) OR
 *   - maxFeePerGas + maxPriorityFeePerGas (EIP-1559)
 */
async function gasStrategy(publicClient) {
  const multiplier = parseFloat(process.env.GAS_PRICE_MULTIPLIER || '1.1');
  const maxPriorityGwei = parseFloat(process.env.MAX_PRIORITY_FEE_GWEI || '0.1');
  const maxFeeCapGwei = parseFloat(process.env.MAX_FEE_PER_GAS_GWEI || '100');

  try {
    // Try EIP-1559 fee history first (Base supports it)
    const feeHistory = await publicClient.getFeeHistory({ blockCount: 4, newestBlock: 'latest', rewardPercentiles: [50] });
    const baseFeeWei = BigInt(feeHistory.baseFeePerGas?.[feeHistory.baseFeePerGas.length - 1] || 0n);
    const priorityWei = BigInt(Math.round(maxPriorityGwei * 1e9));

    let maxFeePerGas = baseFeeWei + priorityWei;
    // Apply multiplier to base fee component only
    const scaledBase = (baseFeeWei * BigInt(Math.round(multiplier * 1000))) / 1000n;
    maxFeePerGas = scaledBase + priorityWei;

    const cap = BigInt(Math.round(maxFeeCapGwei * 1e9));
    if (maxFeePerGas > cap) maxFeePerGas = cap;

    return {
      type: 'eip1559',
      maxFeePerGas,
      maxPriorityFeePerGas: priorityWei,
      maxFeePerGasGwei: Number(maxFeePerGas) / 1e9,
    };
  } catch (e) {
    // Fallback to legacy gasPrice
    const gp = await publicClient.getGasPrice().catch(() => 0n);
    const scaled = (gp * BigInt(Math.round(multiplier * 1000))) / 1000n;
    const cap = BigInt(Math.round(maxFeeCapGwei * 1e9));
    const final = scaled > cap ? cap : scaled;
    return {
      type: 'legacy',
      gasPrice: final,
      gasPriceGwei: Number(final) / 1e9,
    };
  }
}

module.exports = { getBestClient, clearCache, gasStrategy, getEnvList };
