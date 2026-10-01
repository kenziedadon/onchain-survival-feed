#!/usr/bin/env node
/**
 * swap.js — execute swaps on Base via Aerodrome + Uniswap V3 routers.
 *
 * BUY:  ETH -> tokenOut     via Aerodrome swapExactETHForTokens
 * SELL: tokenIn -> WETH     via Uniswap V3 exactInputSingle
 *
 * Reads the private key from .payer_key_tmp, constructs swap calldata, broadcasts.
 *
 * Usage:
 *   BUY:  node swap.js <tokenOut> <amountEth> <slippageBps> [factoryAddr] [--quote]
 *   SELL: node swap.js <tokenIn> <slippageBps> [factoryAddr] [--sell] [--quote]
 *
 * Examples:
 *   node swap.js 0x4e829f... 0.00005 100
 *   node swap.js 0x4e829f... 100 --sell
 */
const fs = require('fs');
const { http, createPublicClient, createWalletClient, parseEther, encodeFunctionData, formatEther, decodeAbiParameters, maxUint256 } = require('viem');
const { privateKeyToAccount } = require('viem/accounts');
const { base } = require('viem/chains');

const NODE_MODULES = __dirname;
if (!process.env.NODE_PATH || !process.env.NODE_PATH.includes(NODE_MODULES)) {
  process.env.NODE_PATH = NODE_MODULES + ':' + (process.env.NODE_PATH || '');
  require('module').Module._initPaths();
}

const EXPECTED = '0xe7a16Be2aF1eCE8922d9d3aD55C03e29004e540F';
const KEY_FILE = '/home/kenzodadon/onchain-survival/.payer_key_tmp';
const WETH = '0x4200000000000000000000000000000000000006';
const USDC = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913';
// Micro-cap WETH pair routing: v4/hook pools that Aerodrome V3 + V3 router miss
// fall through to Relay aggregator (checksum-normalized token address).
const MICRO_CAP_FALLBACK = true;
// Relay.link native-ETH sentinel (used as originCurrency for BUY, destinationCurrency for unwrap-to-ETH)
const NATIVE_ETH = '0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE';
// DustSweep router reference (verified from docs.dustswap.wtf/docs/contracts/dustsweep-router; source DATA, team-routed)
// V3 current: DustSwapSweepRouter 0x06e6BAa61A5Da1E4469FCa5dEa3EB68324255E20 (env DUST_SWEEP_ROUTER_V3_ADDRESS)
// V2: DustSweepPermit2RouterV2 0x6d3C31E4a2b8e1Fe9De0d260D142183E82cbE1E3; V1 legacy: 0x2eb53aed1f45fd6aa5c1db29531eff40e7b36949
// Team routing: base-research (router spec/probe) -> trade-execution (route verification/gate) -> ops-briefing (status/status-brief). No direct default-profile execution.
// Verify address on-chain (eth_getCode) before trusting; DISARMED preserved; never submit live without caps arm_provenance + replay + user auth.
const WETH_ABI = [
  { inputs: [{ name: 'account', type: 'address' }], name: 'balanceOf', outputs: [{ internalType: 'uint256', name: '', type: 'uint256' }], stateMutability: 'view', type: 'function' },
  { inputs: [], name: 'decimals', outputs: [{ internalType: 'uint8', name: '', type: 'uint8' }], stateMutability: 'view', type: 'function' }
];

// Primary + fallback RPCs for Base mainnet
const RPC_FALLBACKS = [
  process.env.RPC || 'https://mainnet.base.org',
  'https://base.llamarpc.com',
  'https://base.meowrpc.com',
  'https://base.drpc.org',
  'https://1rpc.io/base',
  'https://base-pokt.nodies.app',
  'https://rpc.ankr.com/base',
];

const AERODROME_ROUTER = '0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43';
const UNISWAP_V3_ROUTER = '0x2626664c2603336E57B271c5C0b26F421741e481';

// Allowance constants (Permit2-aware)
const PERMIT2_ADDRESS = '0x000000000022D473030F116dDEE9F6B43aC78BA3';
const MAX_UINT48 = (1n << 48n) - 1n;
const MAX_UINT160 = (1n << 160n) - 1n;
const MAX_UINT256 = (1n << 256n) - 1n;

const ERC20_ALLOWANCE_ABI = [
  { inputs: [{ name: 'owner', type: 'address' }, { name: 'spender', type: 'address' }], name: 'allowance', outputs: [{ name: '', type: 'uint256' }], stateMutability: 'view', type: 'function' },
  { inputs: [{ name: 'spender', type: 'address' }, { name: 'amount', type: 'uint256' }], name: 'approve', outputs: [{ name: '', type: 'bool' }], stateMutability: 'nonpayable', type: 'function' },
];

const PERMIT2_ABI = [
  {
    inputs: [
      { name: 'token', type: 'address' },
      { name: 'spender', type: 'address' },
      { name: 'amount', type: 'uint160' },
      { name: 'expiration', type: 'uint48' },
    ],
    name: 'approve',
    outputs: [],
    stateMutability: 'nonpayable',
    type: 'function',
  },
  {
    inputs: [
      { name: '', type: 'address' },
      { name: '', type: 'address' },
      { name: '', type: 'address' },
    ],
    name: 'allowance',
    outputs: [
      { name: 'amount', type: 'uint160' },
      { name: 'expiration', type: 'uint48' },
      { name: 'nonce', type: 'uint48' },
    ],
    stateMutability: 'view',
    type: 'function',
  },
];

// Aerodrome classic router ABI
const AERO_ROUTER_ABI = [
  {
    "inputs": [
      { "internalType": "uint256", "name": "amountOutMin", "type": "uint256" },
      {
        "components": [
          { "internalType": "address", "name": "from", "type": "address" },
          { "internalType": "address", "name": "to", "type": "address" },
          { "internalType": "bool", "name": "stable", "type": "bool" },
          { "internalType": "address", "name": "factory", "type": "address" }
        ],
        "internalType": "struct Router.Route[]", "name": "routes", "type": "tuple[]"
      },
      { "internalType": "address", "name": "to", "type": "address" },
      { "internalType": "uint256", "name": "deadline", "type": "uint256" }
    ],
    "name": "swapExactETHForTokens",
    "outputs": [{ "internalType": "uint256[]", "name": "amounts", "type": "uint256[]" }],
    "stateMutability": "payable",
    "type": "function"
  },
  {
    "inputs": [
      { "internalType": "uint256", "name": "amountIn", "type": "uint256" },
      { "internalType": "uint256", "name": "amountOutMin", "type": "uint256" },
      {
        "components": [
          { "internalType": "address", "name": "from", "type": "address" },
          { "internalType": "address", "name": "to", "type": "address" },
          { "internalType": "bool", "name": "stable", "type": "bool" },
          { "internalType": "address", "name": "factory", "type": "address" }
        ],
        "internalType": "struct Router.Route[]", "name": "routes", "type": "tuple[]"
      },
      { "internalType": "address", "name": "to", "type": "address" },
      { "internalType": "uint256", "name": "deadline", "type": "uint256" }
    ],
    "name": "swapExactTokensForTokens",
    "outputs": [{ "internalType": "uint256[]", "name": "amounts", "type": "uint256[]" }],
    "stateMutability": "nonpayable",
    "type": "function"
  },
  {
    "inputs": [
      { "internalType": "uint256", "name": "amountIn", "type": "uint256" },
      {
        "components": [
          { "internalType": "address", "name": "from", "type": "address" },
          { "internalType": "address", "name": "to", "type": "address" },
          { "internalType": "bool", "name": "stable", "type": "bool" },
          { "internalType": "address", "name": "factory", "type": "address" }
        ],
        "internalType": "struct Router.Route[]", "name": "routes", "type": "tuple[]"
      }
    ],
    "name": "getAmountsOut",
    "outputs": [{ "internalType": "uint256[]", "name": "amounts", "type": "uint256[]" }],
    "stateMutability": "view",
    "type": "function"
  }
];

// Uniswap V3 SwapRouter ABI
const UNIV3_ROUTER_ABI = [
  {
    "inputs": [
      {
        "components": [
          { "internalType": "address", "name": "tokenIn", "type": "address" },
          { "internalType": "address", "name": "tokenOut", "type": "address" },
          { "internalType": "uint24", "name": "fee", "type": "uint24" },
          { "internalType": "address", "name": "recipient", "type": "address" },
          { "internalType": "uint256", "name": "deadline", "type": "uint256" },
          { "internalType": "uint256", "name": "amountIn", "type": "uint256" },
          { "internalType": "uint256", "name": "amountOutMinimum", "type": "uint256" },
          { "internalType": "uint160", "name": "sqrtPriceLimitX96", "type": "uint160" }
        ],
        "internalType": "struct ISwapRouter.ExactInputSingleParams",
        "name": "params", "type": "tuple"
      }
    ],
    "name": "exactInputSingle",
    "outputs": [{ "internalType": "uint256", "name": "amountOut", "type": "uint256" }],
    "stateMutability": "payable",
    "type": "function"
  }
];

const ERC20_ABI = [
  { "inputs": [{ "internalType": "address", "name": "account", "type": "address" }], "name": "balanceOf", "outputs": [{ "internalType": "uint256", "name": "", "type": "uint256" }], "stateMutability": "view", "type": "function" },
  { "inputs": [{ "internalType": "address", "name": "owner", "type": "address" }, { "internalType": "address", "name": "spender", "type": "address" }], "name": "allowance", "outputs": [{ "internalType": "uint256", "name": "", "type": "uint256" }], "stateMutability": "view", "type": "function" },
  { "inputs": [{ "internalType": "address", "name": "spender", "type": "address" }, { "internalType": "uint256", "name": "value", "type": "uint256" }], "name": "approve", "outputs": [{ "internalType": "bool", "name": "", "type": "bool" }], "stateMutability": "nonpayable", "type": "function" },
  { "inputs": [], "name": "decimals", "outputs": [{ "internalType": "uint8", "name": "", "type": "uint8" }], "stateMutability": "view", "type": "function" }
];

function normalizeAmount(raw) {
  if (typeof raw === 'number') raw = String(raw);
  const s = String(raw).trim();
  if (!s) throw new Error('empty amount');
  if (/^\d+(\.\d+)?$/.test(s)) return s;
  const m = s.match(/^(-?\d*\.?\d+)([eE])([+-]?\d+)$/);
  if (m) {
    const n = Number(s);
    if (!isFinite(n) || n <= 0) throw new Error(`invalid amount: ${s}`);
    return n.toFixed(30).replace(/0+$/, '').replace(/\.$/, '');
  }
  const n = Number(s);
  if (!isFinite(n) || n <= 0) throw new Error(`invalid amount: ${s}`);
  return n.toFixed(30).replace(/0+$/, '').replace(/\.$/, '');
}

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

async function withRetry(fn, label, retries = 3, delayMs = 500) {
  let lastErr;
  for (let i = 0; i < retries; i++) {
    try {
      return await fn();
    } catch (e) {
      lastErr = e;
      const msg = (e.message || e.shortMessage || '').toLowerCase();
      const retryable = msg.includes('timeout') || msg.includes('429') || msg.includes('5xx') || msg.includes('econnreset') || msg.includes('network');
      if (i < retries - 1 && retryable) {
        await sleep(delayMs * (i + 1));
        continue;
      }
      throw e;
    }
  }
  throw lastErr;
}

const RPC_TIMEOUT_MS = 12_000;

// Per-call RPC reroute across the full RPC_FALLBACKS pool (proven in swap.merged.harness.js:
// broken RPC #0 failed 9x, every call fell back to a live RPC and returned a real quote).
// Differs from withRetry (which retries on a single client): this walks EVERY configured
// RPC for each call, so a single dead/403 RPC never kills the whole operation.
async function _withFallback(createCall, label) {
  let lastErr;
  for (let i = 0; i < RPC_FALLBACKS.length; i++) {
    const rpcUrl = RPC_FALLBACKS[i];
    let client;
    try {
      client = await makePublicClient(rpcUrl);
    } catch (e) {
      lastErr = e;
      continue;
    }
    try {
      const result = await createCall(client, rpcUrl, i);
      if (i > 0) console.log(`[RPC FALLBACK OK] ${label} -> RPC #${i} ${rpcUrl}`);
      return { result, rpcIndex: i, rpcUrl };
    } catch (e) {
      lastErr = e;
      const msg = (e.message || e.shortMessage || '').toLowerCase();
      const retriable = msg.includes('403') || msg.includes('429') || msg.includes('timeout') || msg.includes('econnreset') || msg.includes('network') || msg.includes('5xx') || msg.includes('failed to fetch');
      console.log(`[RPC FAIL] ${label} RPC #${i} ${rpcUrl} -> ${String(e.message || e).slice(0, 120)} ${retriable ? '[retriable]' : ''}`);
      if (retriable) continue;
      throw e;
    }
  }
  throw lastErr;
}

async function makePublicClient(rpcUrl) {
  return createPublicClient({ chain: base, transport: http(rpcUrl, { timeout: RPC_TIMEOUT_MS, retryCount: 1, retryDelay: 500 }) });
}

async function makeWalletClient(account, rpcUrl) {
  return createWalletClient({ account, chain: base, transport: http(rpcUrl, { timeout: RPC_TIMEOUT_MS, retryCount: 1, retryDelay: 500 }) });
}

// Send a contract write and wait for receipt with generous timeout.
async function writeAndWait(address, abi, functionName, args, walletClient, client) {
  const hash = await withRetry(async () => {
    return walletClient.writeContract({ address, abi, functionName, args });
  }, `write:${functionName}`);
  await client.waitForTransactionReceipt({
    hash,
    timeout: 120_000,
    confirmations: 2,
    pollingInterval: 1_000,
  });
  return hash;
}

// Read allowance for either Permit2 or standard ERC20. Throws on RPC failure (fail closed).
async function readAllowance(token, spender, owner, client) {
  const isPermit2 = spender.toLowerCase() === PERMIT2_ADDRESS.toLowerCase();
  if (isPermit2) {
    try {
      const data = await withRetry(() => client.readContract({
        address: PERMIT2_ADDRESS,
        abi: PERMIT2_ABI,
        functionName: 'allowance',
        args: [owner, token, spender],
      }), 'readAllowance:permit2');
      return { amount: data.amount, expiration: data.expiration };
    } catch (e) {
      // Permit2 not deployed / unsupported -> treat as no allowance
      return { amount: 0n, expiration: null };
    }
  }
  try {
    const amount = await withRetry(() => client.readContract({
      address: token,
      abi: ERC20_ALLOWANCE_ABI,
      functionName: 'allowance',
      args: [owner, spender],
    }), 'readAllowance:erc20');
    return { amount, expiration: null };
  } catch (e) {
    const msg = (e.message || '').toLowerCase();
    // Real RPC/network failure -> fail closed (let withRetry surface it)
    if (msg.includes('timeout') || msg.includes('429') || msg.includes('network') || msg.includes('econnreset') || msg.includes('5xx')) {
      throw e;
    }
    // Nonstandard ERC20 may revert on allowance() (missing getter / unsupported
    // spender). Assume zero so we attempt an approval; if the token lacks
    // approve() this surfaces at write time with a clear error.
    console.error(`allowance() reverted for ${token} (nonstandard ERC20?): ${e.message.slice(0, 120)} — assuming 0`);
    return { amount: 0n, expiration: null };
  }
}

// Ensure `owner` has granted `spender` at least `amount` of `token`.
// Resets to zero first when a non-zero allowance exists (required by some
// ERC20s with unsafe-allowance guards). Approves max, verifies, fails closed.
async function ensureAllowance(token, spender, owner, amount, client, walletClient) {
  const isPermit2 = spender.toLowerCase() === PERMIT2_ADDRESS.toLowerCase();
  const now = BigInt(Math.floor(Date.now() / 1000));
  const amountBig = BigInt(amount);

  let current;
  try {
    current = await readAllowance(token, spender, owner, client);
  } catch (e) {
    throw new Error(`Allowance read failed: ${e.message}`);
  }

  if (isPermit2) {
    if (current.amount >= amountBig && current.expiration > now + 60n) return; // already sufficient
  } else if (current.amount >= amountBig) {
    return; // already sufficient
  }

  // Reset to zero if non-zero (required by Permit2 and guarded ERC20s)
  if (current.amount !== 0n) {
    console.log('Resetting existing allowance to 0...');
    await writeAndWait(
      isPermit2 ? PERMIT2_ADDRESS : token,
      isPermit2 ? PERMIT2_ABI : ERC20_ALLOWANCE_ABI,
      'approve',
      isPermit2 ? [token, spender, 0n, MAX_UINT48] : [spender, 0n],
      walletClient, client
    );
  }

  // Approve new allowance. Try max first; some nonstandard ERC20s reject an
  // unlimited approval, so fall back to the exact required amount.
  const approveAmount = isPermit2 ? MAX_UINT160 : MAX_UINT256;
  console.log(`Approving ${isPermit2 ? 'Permit2' : 'ERC20'} allowance...`);
  try {
    await writeAndWait(
      isPermit2 ? PERMIT2_ADDRESS : token,
      isPermit2 ? PERMIT2_ABI : ERC20_ALLOWANCE_ABI,
      'approve',
      isPermit2 ? [token, spender, approveAmount, MAX_UINT48] : [spender, approveAmount],
      walletClient, client
    );
  } catch (e) {
    if (isPermit2) throw e;
    console.error(`Max approval reverted (${e.message.slice(0, 120)}); retrying exact amount...`);
    await writeAndWait(token, ERC20_ALLOWANCE_ABI, 'approve', [spender, amountBig], walletClient, client);
  }

  // Verify — fail closed on RPC error or insufficient allowance
  let verified;
  try {
    verified = await readAllowance(token, spender, owner, client);
  } catch (e) {
    throw new Error(`Allowance verification RPC failed: ${e.message}`);
  }
  if (verified.amount < amountBig) {
    throw new Error(`Allowance still below required amount: ${verified.amount} < ${amountBig}`);
  }
  console.log('Allowance verified:', verified.amount.toString());
}

// Probe each RPC in RPC_FALLBACKS order; return the first that answers a
// liveness check (getBlockNumber) within RPC_TIMEOUT_MS. Used for both the
// public client and (via .transport.url) the wallet client.
async function callWithFallback(publicClientFactory, callFn, label) {
  let lastErr = new Error('all RPCs failed');
  for (const rpc of RPC_FALLBACKS) {
    try {
      const client = await makePublicClient(rpc);
      await Promise.race([
        client.getBlockNumber(),
        new Promise((_, rej) => setTimeout(() => rej(new Error('rpc probe timeout')), RPC_TIMEOUT_MS)),
      ]);
      const result = await callFn(client);
      return { client, result };
    } catch (e) {
      lastErr = e;
      console.error(`RPC ${rpc} failed (${label}): ${e.message.slice(0, 120)}`);
      continue;
    }
  }
  throw lastErr;
}

function emitJson(obj) {
  console.log(JSON.stringify(obj, null, 2));
}

// Read ERC20 balance, tolerant of nonstandard tokens (missing/non-returning
// balanceOf -> treat as 0 rather than crashing the run). Used for surplus/dust
// reconciliation after a swap.
async function readTokenBalance(token, owner, client) {
  try {
    const balData = encodeFunctionData({ abi: ERC20_ABI, functionName: 'balanceOf', args: [owner] });
    const r = await client.call({ to: token, data: balData });
    if (!r.data || r.data === '0x') return 0n;
    return decodeAbiParameters([{ type: 'uint256' }], r.data)[0];
  } catch (e) {
    console.error(`balanceOf(${token}) failed: ${e.message.slice(0, 120)} — assuming 0`);
    return 0n;
  }
}

// Relay aggregator fallback (handles Uniswap v4 / hook pools that Aerodrome +
// Uniswap V3 cannot route). Generic: tokenIn -> tokenOut. Always fetches a
// fresh quote at execution time to avoid stale-quote reuse. Clients are passed
// in explicitly — this function is called from main(), which owns them (the
// old relaySellQuote referenced module-scope publicClient/walletClient that
// were never defined, so it threw ReferenceError at runtime).
async function relaySwap({ tokenIn, tokenOut, amount, account, slippageBps, quoteOnly, publicClient, walletClient }) {
  const failureLabel = `Relay:${tokenIn.slice(0,6)}->${tokenOut.slice(0,6)}`;

  try {
    const body = {
      user: account.address,
      recipient: account.address,
      originChainId: 8453,
      destinationChainId: 8453,
      originCurrency: tokenIn,
      destinationCurrency: tokenOut,
      amount: amount.toString(),
      tradeType: 'EXACT_INPUT',
    };
    const res = await fetch('https://api.relay.link/quote', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });

    if (!res.ok) {
      let detail = '';
      try { const eb = await res.json(); detail = eb?.message || eb?.errorCode || ''; } catch (e) {}
      const msg = `${failureLabel} quote HTTP ${res.status}${detail ? ': ' + detail : ''}`;
      console.error(msg);
      return { ok: false, mode: 'quote_http', code: res.status, detail };
    }

    const q = await res.json();
    const outAmt = q?.details?.currencyOut?.amount;
    if (!outAmt || BigInt(outAmt) === 0n) {
      const msg = `${failureLabel}: no route / zero output`;
      console.error(msg);
      return { ok: false, mode: 'no_route', code: 'ZERO_OUT' };
    }

    const expected = BigInt(outAmt);
    const minOut = (expected * BigInt(10000 - slippageBps)) / 10000n;
    console.log('Relay route found.');
    console.log('Expected out:', formatEther(expected));
    console.log('Min out:', formatEther(minOut));

    if (quoteOnly) {
      emitJson({ quoteOnly: true, router: 'relay', expectedOut: formatEther(expected), minOut: formatEther(minOut), requestId: q.requestId });
      return { ok: true, quoteOnly: true };
    }

    for (const step of (q.steps || [])) {
      for (const item of (step.items || [])) {
        if (!item.data) continue;
        const d = item.data;
        console.log('Relay step:', step.id, '->', d.to);
        let gas;
        try {
          gas = await publicClient.estimateGas({ to: d.to, data: d.data, value: BigInt(d.value || '0'), account });
        } catch (e) {
          const msg = `${failureLabel} gas estimate failed: ${e.message.slice(0, 150)}`;
          console.error(msg);
          return { ok: false, mode: 'gas_estimate', detail: e.message.slice(0, 150) };
        }
        let hash;
        try {
          hash = await walletClient.sendTransaction({
            to: d.to, data: d.data, value: BigInt(d.value || '0'), gas,
            gasPrice: await publicClient.getGasPrice(),
          });
        } catch (e) {
          const msg = `${failureLabel} send tx failed: ${e.message.slice(0, 150)}`;
          console.error(msg);
          return { ok: false, mode: 'send_tx', detail: e.message.slice(0, 150) };
        }
        console.log('Tx hash:', hash);
        let receipt;
        try {
          receipt = await publicClient.waitForTransactionReceipt({ hash, timeout: 120000 });
        } catch (e) {
          const msg = `${failureLabel} wait receipt failed: ${e.message.slice(0, 150)}`;
          console.error(msg);
          return { ok: false, mode: 'wait_receipt', detail: e.message.slice(0, 150), txHash: hash };
        }
        console.log('Status:', receipt.status);
        if (receipt.status !== 'success') {
          const msg = `${failureLabel} tx reverted: ${hash}`;
          console.error(msg);
          return { ok: false, mode: 'tx_revert', detail: 'receipt status != success', txHash: hash };
        }
      }
    }
    emitJson({
      hash: 'relay-multistep', router: 'relay', tokenIn, tokenOut,
      amountIn: formatEther(amount), expectedOut: formatEther(expected),
    });
    return { ok: true, hash: 'relay-multistep', router: 'relay', expectedOut: formatEther(expected) };
  } catch (e) {
    const msg = `${failureLabel} fallback error: ${e.message.slice(0, 200)}`;
    console.error(msg);
    return { ok: false, mode: 'exception', detail: e.message.slice(0, 200) };
  }
}

async function main() {
  const rawArgs = process.argv.slice(2);
  const quoteOnly = rawArgs.includes('--quote');
  const sellMode = rawArgs.includes('--sell');
  const unwrapWeth = rawArgs.includes('--unwrap');
  const args = rawArgs.filter(a => a !== '--quote' && a !== '--sell' && a !== '--unwrap');

  if (args.length < 2 && !unwrapWeth) {
    console.log('Usage:');
    console.log('  BUY:  node swap.js <tokenOut> <amountEth> <slippageBps> [factoryAddr] [--quote]');
    console.log('  SELL: node swap.js <tokenIn> <slippageBps> [factoryAddr] [--sell] [--quote]');
    console.log('  UNWRAP: node swap.js --unwrap');
    process.exit(1);
  }

  let tokenAddr, amountSpec, slippageBps, factoryAddr = null;
  const isAddr = (s) => /^0x[0-9a-fA-F]{40}$/.test(s);
  // Normalize any casing to EIP-55 checksum so viem accepts it
  const { getAddress } = require('viem');
  if (unwrapWeth) {
    // No token/amount needed for unwrap
  } else if (sellMode) {
    tokenAddr = getAddress(args[0]);
    slippageBps = parseInt(args[1], 10);
    if (args[2] && isAddr(args[2])) factoryAddr = args[2];
  } else {
    tokenAddr = getAddress(args[0]);
    amountSpec = normalizeAmount(args[1]);
    slippageBps = parseInt(args[2], 10);
    if (args[3] && isAddr(args[3])) factoryAddr = getAddress(args[3]);
  }
  factoryAddr = factoryAddr || '0x420DD381b31aEf6683db6B902084cB0FFECe40Da';

  // Determine quote token (WETH or USDC) from command line
  const forceUsdc = rawArgs.includes('--usdc');
  const quoteToken = forceUsdc ? USDC : WETH;

  const key = fs.readFileSync(KEY_FILE, 'utf8').trim();
  const account = privateKeyToAccount(key);
  if (account.address.toLowerCase() !== EXPECTED.toLowerCase()) {
    console.error('KEY MISMATCH:', account.address, '!=', EXPECTED);
    process.exit(2);
  }

  const { client: publicClient } = await callWithFallback(
    () => makePublicClient(RPC_FALLBACKS[0]),
    (c) => c.getBlockNumber(),
    'init'
  );
  const walletClient = await makeWalletClient(account, publicClient.transport.url);
  const deadline = BigInt(Math.floor(Date.now() / 1000) + 300);

  if (unwrapWeth) {
    console.log('UNWRAP: WETH -> ETH');
    console.log('Unwrapping WETH -> ETH...');
    const wethDecimals = await withRetry(async () => {
      const d = encodeFunctionData({ abi: WETH_ABI, functionName: 'decimals', args: [] });
      const r = await publicClient.call({ to: WETH, data: d });
      return decodeAbiParameters([{ type: 'uint8' }], r.data)[0];
    }, 'weth-decimals');
    const wethBal = await withRetry(async () => {
      const b = encodeFunctionData({ abi: WETH_ABI, functionName: 'balanceOf', args: [account.address] });
      const r = await publicClient.call({ to: WETH, data: b });
      return decodeAbiParameters([{ type: 'uint256' }], r.data)[0];
    }, 'weth-balance');
    console.log('WETH balance:', formatEther(wethBal), `(decimals=${wethDecimals})`);
    if (wethBal > 0n) {
      const amountHex = wethBal.toString(16).padStart(64, '0');
      const withdrawData = '0x2e1a7d4d' + amountHex;
      const withdrawGas = await withRetry(async () => {
        return publicClient.estimateGas({ to: WETH, data: withdrawData, account });
      }, 'withdraw-gas');
      const wethHash = await walletClient.sendTransaction({ to: WETH, data: withdrawData, gas: withdrawGas, gasPrice: await publicClient.getGasPrice() });
      console.log('Unwrap tx:', wethHash);
      const wethReceipt = await publicClient.waitForTransactionReceipt({ hash: wethHash, timeout: 60000 });
      console.log('Unwrap confirmed! Block:', wethReceipt.blockNumber?.toString());
      emitJson({ unwrapHash: wethHash, unwrapBlock: wethReceipt.blockNumber?.toString(), unwrappedWeth: formatEther(wethBal) });
    } else {
      console.log('No WETH to unwrap.');
      emitJson({ unwrapSkipped: true, reason: 'zero WETH balance' });
    }
    process.exit(0);
  }

  if (sellMode) {
    console.log('SELL: tokenIn -> WETH');
    console.log('Token:', tokenAddr);
    console.log('Factory:', factoryAddr);

    // 1. Read token balance
    const balance = await withRetry(async () => {
      const balData = encodeFunctionData({ abi: ERC20_ABI, functionName: 'balanceOf', args: [account.address] });
      const balResult = await publicClient.call({ to: tokenAddr, data: balData });
      return decodeAbiParameters([{ type: 'uint256' }], balResult.data)[0];
    }, 'balanceOf');

    console.log('Token balance:', formatEther(balance), 'tokens');
    const tokenBalance = formatEther(balance);
    if (balance === 0n) {
      emitJson({ noBalance: true, tokenIn: tokenAddr });
      process.exit(0);
    }

    // 2. Find fee tier — try Aerodrome first, then Uniswap V3
    const feeTiers = [10000, 3000, 500];
    let bestExpectedOut = 0n;
    let bestFee = 0;
    let amountOutMin = 0n;
    let useUniswapV3 = false;
    let sellTwoHopRoutes = null; // set if two-hop token->USDC->WETH won

    // Try Aerodrome router first
    for (const fee of feeTiers) {
      try {
        const quoteData = encodeFunctionData({
          abi: AERO_ROUTER_ABI,
          functionName: 'getAmountsOut',
          args: [balance, [{ from: tokenAddr, to: WETH, stable: false, factory: factoryAddr }]]
        });
        const quoteResult = await _withFallback(async (client) => {
          return client.call({ to: AERODROME_ROUTER, data: quoteData });
        }, `SELL quote ${tokenAddr}`).then(r => r.result);
        if (quoteResult.data && quoteResult.data !== '0x') {
          try {
            const amounts = decodeAbiParameters([{ type: 'uint256[]' }], quoteResult.data);
            const expected = amounts[0][amounts[0].length - 1];
            if (expected > bestExpectedOut) {
              bestExpectedOut = expected;
              bestFee = fee;
              useUniswapV3 = false;
              const slippageMultiplier = BigInt(10000 - slippageBps);
              amountOutMin = (expected * slippageMultiplier) / 10000n;
            }
          } catch (e) {
            console.error('Aerodrome decode failed:', e.message.slice(0, 150));
          }
        }
      } catch (e) {}
    }

    // If Aerodrome didn't work, try Uniswap V3 by simulating exactInputSingle
    if (bestExpectedOut === 0n) {
      useUniswapV3 = true;
      for (const fee of feeTiers) {
        try {
          // Simulate with a small amount to get the rate
          const testAmount = 10n ** 18n; // 1 token
          const swapData = encodeFunctionData({
            abi: UNIV3_ROUTER_ABI,
            functionName: 'exactInputSingle',
            args: [{
              tokenIn: tokenAddr,
              tokenOut: WETH,
              fee,
              recipient: EXPECTED,
              deadline,
              amountIn: testAmount,
              amountOutMinimum: 0n,
              sqrtPriceLimitX96: 0n
            }]
          });
          const result = await publicClient.call({ to: UNISWAP_V3_ROUTER, data: swapData, account: EXPECTED });
          if (result && result.data && result.data !== '0x') {
            try {
              const expectedPerToken = decodeAbiParameters([{ type: 'uint256' }], result.data)[0];
              if (expectedPerToken > 0n) {
                const expected = expectedPerToken * balance / testAmount;
                if (expected > bestExpectedOut) {
                  bestExpectedOut = expected;
                  bestFee = fee;
                  const slippageMultiplier = BigInt(10000 - slippageBps);
                  amountOutMin = (expected * slippageMultiplier) / 10000n;
                }
              }
            } catch (e) {
              console.error('Uniswap V3 decode failed:', e.message.slice(0, 150));
            }
          }
        } catch (e) {}
      }
    }

    // If still nothing, try two-hop sell via Aerodrome: token -> USDC -> WETH.
    // Covers USDC-quoted pools with no direct WETH pool (Relay rejects small
    // amounts with AMOUNT_TOO_LOW, so this is the reliable exit for them).
    if (bestExpectedOut === 0n) {
      useUniswapV3 = false;
      try {
        const twoHopRoutes = [
          { from: tokenAddr, to: USDC, stable: false, factory: factoryAddr },
          { from: USDC, to: WETH, stable: true, factory: factoryAddr },
        ];
        const twoHopData = encodeFunctionData({
          abi: AERO_ROUTER_ABI,
          functionName: 'getAmountsOut',
          args: [balance, twoHopRoutes]
        });
        const twoHopResult = await _withFallback(async (client) => {
          return client.call({ to: AERODROME_ROUTER, data: twoHopData });
        }, `SELL two-hop quote ${tokenAddr}`).then(r => r.result);
        if (twoHopResult.data && twoHopResult.data !== '0x') {
          const amounts = decodeAbiParameters([{ type: 'uint256[]' }], twoHopResult.data);
          const expected = amounts[0][amounts[0].length - 1];
          if (expected > 0n) {
            bestExpectedOut = expected;
            sellTwoHopRoutes = twoHopRoutes;
            const slippageMultiplier = BigInt(10000 - slippageBps);
            amountOutMin = (expected * slippageMultiplier) / 10000n;
            console.log('Aerodrome two-hop (token->USDC->WETH) expected out:', formatEther(expected), 'WETH');
          }
        }
      } catch (e) {
        console.log('Two-hop sell quote failed:', e.message.slice(0, 150));
      }
    }

    if (bestExpectedOut === 0n) {
      // Fallback: Relay aggregator (handles Uniswap v4 / hook pools that v3+Aerodrome cannot route)
      const relay = await relaySwap({ tokenIn: tokenAddr, tokenOut: WETH, amount: balance, account, slippageBps, quoteOnly, publicClient, walletClient });
      if (!relay || !relay.ok) {
        const errDetail = relay ? `${relay.mode}: ${relay.detail || 'unknown'}` : 'no response';
        const err = new Error(`Relay fallback failed: ${errDetail}`);
        console.error(err.message);
        emitJson({ error: err.message, tokenIn: tokenAddr, relayMode: relay ? relay.mode : 'no_response' });
        process.exit(5);
      }

      if (relay.quoteOnly) {
        process.exit(0);
      }

      emitJson({
        hash: relay.hash || 'relay-multistep',
        router: 'relay',
        tokenIn: tokenAddr,
        amountToken: formatEther(balance),
        expectedOut: relay.expectedOut || '',
      });
      process.exit(0);
    }

    console.log(`Best fee tier: ${bestFee / 10000}%`);
    console.log('Expected WETH out:', formatEther(bestExpectedOut), 'WETH');
    console.log('Min WETH out:', formatEther(amountOutMin), 'WETH');

    if (quoteOnly) {
      emitJson({ quoteOnly: true, expectedOut: formatEther(bestExpectedOut), minOut: formatEther(amountOutMin), bestFee, router: useUniswapV3 ? 'uniswap_v3' : 'aerodrome' });
      process.exit(0);
    }

    // Capture WETH balance before swap for surplus/dust reconciliation
    const wethBefore = await readTokenBalance(WETH, account.address, publicClient);

    // 3. Approve via unified allowance handler (Permit2-aware, fail-closed)
    console.log('Ensuring router allowance...');
    try {
      await ensureAllowance(tokenAddr, AERODROME_ROUTER, account.address, balance, publicClient, walletClient);
      if (useUniswapV3) {
        await ensureAllowance(tokenAddr, UNISWAP_V3_ROUTER, account.address, balance, publicClient, walletClient);
      }
    } catch (e) {
      console.error('Approval failed:', e.message.slice(0, 200));
      emitJson({ error: `approval failed: ${e.message}` });
      process.exit(7);
    }

    // 4. Swap — use the router that passed gas estimation
    let swapData, swapRouter;
    if (sellTwoHopRoutes) {
      // Two-hop Aerodrome: token -> USDC (volatile) -> WETH (stable)
      swapData = encodeFunctionData({
        abi: AERO_ROUTER_ABI,
        functionName: 'swapExactTokensForTokens',
        args: [balance, amountOutMin, sellTwoHopRoutes, EXPECTED, deadline]
      });
      swapRouter = AERODROME_ROUTER;
    } else if (useUniswapV3) {
      swapData = encodeFunctionData({
        abi: UNIV3_ROUTER_ABI,
        functionName: 'exactInputSingle',
        args: [{
          tokenIn: tokenAddr,
          tokenOut: WETH,
          fee: bestFee,
          recipient: EXPECTED,
          deadline,
          amountIn: balance,
          amountOutMinimum: amountOutMin,
          sqrtPriceLimitX96: 0n
        }]
      });
      swapRouter = UNISWAP_V3_ROUTER;
    } else {
      swapData = encodeFunctionData({
        abi: AERO_ROUTER_ABI,
        functionName: 'swapExactTokensForTokens',
        args: [balance, amountOutMin, [{ from: tokenAddr, to: WETH, stable: false, factory: factoryAddr }], EXPECTED, deadline]
      });
      swapRouter = AERODROME_ROUTER;
    }
    console.log('Selector:', swapData.slice(0, 10));

    let gas;
    try {
      gas = await publicClient.estimateGas({ to: swapRouter, data: swapData, account: EXPECTED });
      console.log('Estimated gas:', gas.toString());
    } catch (e) {
      console.log('Gas estimate failed:', e.message.slice(0, 200));
      emitJson({ error: `gas estimate failed: ${e.message}` });
      process.exit(3)
    }

    console.log('Broadcasting sell...');
    const hash = await walletClient.sendTransaction({
      to: swapRouter,
      data: swapData,
      gas,
      gasPrice: await publicClient.getGasPrice(),
    });
    console.log('Tx hash:', hash);

    const receipt = await publicClient.waitForTransactionReceipt({ hash, timeout: 120_000, confirmations: 2, pollingInterval: 1_000 });
    console.log('Confirmed! Block:', receipt.blockNumber?.toString());
    console.log('Gas used:', receipt.gasUsed?.toString());
    console.log('Status:', receipt.status);

    const wethAfter = await readTokenBalance(WETH, account.address, publicClient);
    const tokenAfter = await readTokenBalance(tokenAddr, account.address, publicClient);
    const wethReceived = wethAfter > wethBefore ? wethAfter - wethBefore : 0n;
    emitJson({
      hash,
      blockNumber: receipt.blockNumber?.toString(),
      gasUsed: receipt.gasUsed?.toString(),
      status: receipt.status,
      tokenIn: tokenAddr,
      amountToken: tokenBalance,
      expectedOut: formatEther(bestExpectedOut),
      actualWethOut: formatEther(wethReceived),
      dustTokenRemaining: formatEther(tokenAfter),
      router: useUniswapV3 ? 'uniswap_v3' : 'aerodrome',
      fee: bestFee ? bestFee / 10000 : null,
    });

  } else {
    // BUY: ETH -> tokenOut or USDC -> tokenOut via Aerodrome
    // Amount is always specified in ETH units (e.g. "0.0005"). For USDC-quote
    // pools we convert to the equivalent USDC amount (6 decimals) so the
    // balance check and swapExactTokensForTokens use correct units.
    const amountIn = parseEther(amountSpec);
    let amountInUsdc = 0n; // set below when quoteToken === USDC

    if (quoteToken === USDC) {
      console.log('Swap: USDC ->', tokenAddr);
      // Convert ETH amount to USDC units via the WETH/USDC pool (stable route).
      // Fall back to a flat price estimate if the quote fails.
      let usdcAmountIn = 0n;
      try {
        const usdcQuoteData = encodeFunctionData({
          abi: AERO_ROUTER_ABI,
          functionName: 'getAmountsOut',
          args: [amountIn, [{ from: WETH, to: USDC, stable: true, factory: factoryAddr }]]
        });
        const usdcQuoteResult = await _withFallback(async (client) => {
          return client.call({ to: AERODROME_ROUTER, data: usdcQuoteData });
        }, 'WETH->USDC conversion quote').then(r => r.result);
        if (usdcQuoteResult.data && usdcQuoteResult.data !== '0x') {
          const usdcAmounts = decodeAbiParameters([{ type: 'uint256[]' }], usdcQuoteResult.data);
          usdcAmountIn = usdcAmounts[0][usdcAmounts[0].length - 1];
        }
      } catch (e) {
        console.log('WETH->USDC conversion quote failed:', e.message.slice(0, 150));
      }
      if (usdcAmountIn > 0n) {
        // Trim to whole USDC base units (6dp) to avoid rounding dust
        usdcAmountIn = (usdcAmountIn / 1000000n) * 1000000n;
        console.log('Amount:', amountSpec, 'ETH ≈', (Number(usdcAmountIn) / 1e6).toFixed(6), 'USDC');
        amountInUsdc = usdcAmountIn;
      } else {
        emitJson({ error: 'could not convert ETH amount to USDC units (WETH/USDC quote failed)' });
        process.exit(9);
      }
    } else {
      console.log('Swap: ETH ->', tokenAddr);
      console.log('Amount:', amountSpec, 'ETH');
    }
    console.log('Router: Aerodrome');
    console.log('Factory:', factoryAddr);

    // Quote routes: for USDC-quote tokens, route USDC -> token (6dp units).
    // For WETH-quote tokens, route WETH -> token. If a direct WETH -> token
    // route is empty, also try a two-hop WETH -> USDC -> token before Relay.
    const quoteRoutes = quoteToken === USDC
      ? [{ from: USDC, to: tokenAddr, stable: false, factory: factoryAddr }]
      : [{ from: WETH, to: tokenAddr, stable: false, factory: factoryAddr }];
    const quoteAmount = quoteToken === USDC ? amountInUsdc : amountIn;
    let amountOutMin = 0n;
    try {
      const quoteData = encodeFunctionData({
        abi: AERO_ROUTER_ABI,
        functionName: 'getAmountsOut',
        args: [quoteAmount, quoteRoutes]
      });
      const quoteResult = await _withFallback(async (client) => {
        return client.call({ to: AERODROME_ROUTER, data: quoteData });
      }, `BUY quote ${tokenAddr}`).then(r => r.result);

      let bestExpectedOut = 0n;
      let bestRouter = AERODROME_ROUTER;
      let bestSwapData = null;
      let bestFee = null;
      let bestRoutes = quoteRoutes;
      let bestAmountIn = quoteAmount;

      if (quoteResult.data && quoteResult.data !== '0x') {
        const amounts = decodeAbiParameters([{ type: 'uint256[]' }], quoteResult.data);
        bestExpectedOut = amounts[0][amounts[0].length - 1];
        console.log('Aerodrome expected out:', formatEther(bestExpectedOut), 'tokens');
      }

      // If direct route returned nothing, try two-hop WETH -> USDC -> token
      // (covers tokens whose only Aerodrome pool is USDC-quoted, e.g. NOCK).
      if (bestExpectedOut === 0n && quoteToken !== USDC) {
        try {
          const twoHopRoutes = [
            { from: WETH, to: USDC, stable: true, factory: factoryAddr },
            { from: USDC, to: tokenAddr, stable: false, factory: factoryAddr },
          ];
          const twoHopData = encodeFunctionData({
            abi: AERO_ROUTER_ABI,
            functionName: 'getAmountsOut',
            args: [amountIn, twoHopRoutes]
          });
          const twoHopResult = await _withFallback(async (client) => {
            return client.call({ to: AERODROME_ROUTER, data: twoHopData });
          }, `BUY two-hop quote ${tokenAddr}`).then(r => r.result);
          if (twoHopResult.data && twoHopResult.data !== '0x') {
            const amounts = decodeAbiParameters([{ type: 'uint256[]' }], twoHopResult.data);
            const out = amounts[0][amounts[0].length - 1];
            if (out > 0n) {
              bestExpectedOut = out;
              bestRoutes = twoHopRoutes;
              bestAmountIn = amountIn;
              console.log('Aerodrome two-hop (WETH->USDC->token) expected out:', formatEther(out), 'tokens');
            }
          }
        } catch (e) {
          console.log('Two-hop quote failed:', e.message.slice(0, 150));
        }
      }

      // If Aerodrome returned nothing, try Uniswap V3 with multiple fee tiers
      if (bestExpectedOut === 0n) {
        console.log('Aerodrome quote empty — trying Uniswap V3 fallback...');
        const uniswapFeeTiers = [500, 3000, 10000];
        for (const fee of uniswapFeeTiers) {
          try {
            const univ3Data = encodeFunctionData({
              abi: UNIV3_ROUTER_ABI,
              functionName: 'exactInputSingle',
              args: [{
                tokenIn: WETH,
                tokenOut: tokenAddr,
                fee,
                recipient: EXPECTED,
                deadline,
                amountIn: amountIn,
                amountOutMinimum: 0n,
                sqrtPriceLimitX96: 0n
              }]
            });
            const v3Result = await publicClient.call({ to: UNISWAP_V3_ROUTER, data: univ3Data, value: amountIn, account: EXPECTED });
            if (v3Result && v3Result.data && v3Result.data !== '0x') {
              const expected = decodeAbiParameters([{ type: 'uint256' }], v3Result.data)[0];
              if (expected > bestExpectedOut) {
                bestExpectedOut = expected;
                bestRouter = UNISWAP_V3_ROUTER;
                bestSwapData = univ3Data;
                bestFee = fee;
                console.log(`Uniswap V3 (fee ${fee/10000}%) expected out:`, formatEther(bestExpectedOut), 'tokens');
              }
            }
          } catch (e) {
            console.log(`  Uniswap V3 fee ${fee} failed:`, e.message.slice(0, 150));
          }
        }
      }

      if (bestExpectedOut === 0n) {
        // Fallback: Relay aggregator (handles Slipstream/CL pools and Uniswap
        // v4 / hook pools that v3+Aerodrome cannot route).
        // Relay on Base requires WETH as originCurrency for token buys — the
        // native-ETH sentinel is rejected with INVALID_INPUT_CURRENCY.
        // The wallet buys from ETH: convert via WETH wrap step is handled by
        // Relay's multi-step flow (it wraps native ETH when routing WETH->X).
        const relayIn = quoteToken === USDC ? { token: USDC, amount: amountInUsdc } : { token: WETH, amount: amountIn };
        // For WETH-origin we must first ensure we actually hold WETH (Relay
        // swaps ERC-20 WETH, it does not wrap native ETH for us on same-chain).
        // Skip the wrap when quoteOnly — quotes must not broadcast transactions.
        if (quoteToken !== USDC && !quoteOnly) {
          const wethBal = await readTokenBalance(WETH, account.address, publicClient);
          if (wethBal < amountIn) {
            console.log(`WETH balance ${formatEther(wethBal)} < ${amountSpec} — wrapping native ETH first...`);
            const wethAbiDeposit = [{ inputs: [], name: 'deposit', outputs: [], stateMutability: 'payable', type: 'function' }];
            const wrapData = encodeFunctionData({ abi: wethAbiDeposit, functionName: 'deposit' });
            const wrapHash = await walletClient.sendTransaction({
              to: WETH, data: wrapData, value: amountIn,
              gas: 45000n, gasPrice: await publicClient.getGasPrice(),
            });
            const wrapReceipt = await publicClient.waitForTransactionReceipt({ hash: wrapHash, timeout: 120_000 });
            console.log('WETH wrap tx:', wrapHash, 'status:', wrapReceipt.status);
            if (wrapReceipt.status !== 'success') {
              emitJson({ error: `WETH wrap failed: ${wrapHash}` });
              process.exit(6);
            }
          }
        }
        const relay = await relaySwap({ tokenIn: relayIn.token, tokenOut: tokenAddr, amount: relayIn.amount, account, slippageBps, quoteOnly, publicClient, walletClient });
        if (!relay || !relay.ok) {
          const errDetail = relay ? `${relay.mode}: ${relay.detail || 'unknown'}` : 'no response';
          const err = `Relay buy fallback failed: ${errDetail}`;
          console.error(err);
          emitJson({ error: err, tokenOut: tokenAddr, relayMode: relay ? relay.mode : 'no_response' });
          process.exit(5);
        }
        if (relay.quoteOnly) process.exit(0);
        emitJson({ hash: relay.hash || 'relay-multistep', router: 'relay', tokenOut: tokenAddr, amountEth: amountSpec, expectedOut: relay.expectedOut || '' });
        process.exit(0);
      }

      const slippageMultiplier = BigInt(10000 - slippageBps);
      amountOutMin = (bestExpectedOut * slippageMultiplier) / 10000n;
      console.log('Expected out:', formatEther(bestExpectedOut), 'tokens');
      console.log('Min out:', formatEther(amountOutMin), 'tokens');
      console.log('Using router:', bestRouter === UNISWAP_V3_ROUTER ? 'Uniswap V3' : 'Aerodrome');

      if (quoteOnly) {
        emitJson({ quoteOnly: true, expectedOut: formatEther(bestExpectedOut), minOut: formatEther(amountOutMin), router: bestRouter === UNISWAP_V3_ROUTER ? 'uniswap_v3' : 'aerodrome' });
        process.exit(0);
      }

      // Capture token balance before swap for surplus/dust reconciliation
      const tokenBefore = await readTokenBalance(tokenAddr, account.address, publicClient);

      // Build swap calldata using the winning router
      let swapData, swapRouter, swapValue;
      if (quoteToken === USDC) {
        // USDC buy: balance check + approval + swapExactTokensForTokens —
        // all in USDC base units (6 decimals), NOT parseEther 18dp.
        if (bestRoutes.length > 1 || bestAmountIn !== amountInUsdc) {
          // Two-hop path won (WETH->USDC->token): spend ETH natively instead.
          console.log('Using two-hop ETH route for USDC-quoted pool');
          swapData = encodeFunctionData({
            abi: AERO_ROUTER_ABI,
            functionName: 'swapExactETHForTokens',
            args: [amountOutMin, bestRoutes, EXPECTED, deadline]
          });
          swapRouter = AERODROME_ROUTER;
          swapValue = { value: amountIn };
        } else {
          const usdcBalance = await readTokenBalance(USDC, account.address, publicClient);
          if (usdcBalance < amountInUsdc) {
            console.error(`Insufficient USDC balance: ${Number(usdcBalance) / 1e6} < ${Number(amountInUsdc) / 1e6}`);
            emitJson({ error: `insufficient USDC balance: ${Number(usdcBalance) / 1e6}` });
            process.exit(8);
          }
          // Ensure router has USDC allowance (in 6dp base units)
          await ensureAllowance(USDC, AERODROME_ROUTER, account.address, amountInUsdc, publicClient, walletClient);
          swapData = encodeFunctionData({
            abi: AERO_ROUTER_ABI,
            functionName: 'swapExactTokensForTokens',
            args: [amountInUsdc, amountOutMin, bestRoutes, EXPECTED, deadline]
          });
          swapRouter = AERODROME_ROUTER;
          swapValue = {};
        }
      } else if (bestRouter === UNISWAP_V3_ROUTER) {
        // Use the already-encoded data from the successful Uniswap V3 quote
        swapData = bestSwapData;
        swapRouter = UNISWAP_V3_ROUTER;
        swapValue = { value: amountIn };
      } else {
        // Rebuild Aerodrome calldata with the computed amountOutMin.
        // bestRoutes/bestAmountIn reflect the winning path (direct or two-hop).
        if (bestAmountIn === amountIn) {
          // Native ETH path (direct WETH route or two-hop via USDC)
          swapData = encodeFunctionData({
            abi: AERO_ROUTER_ABI,
            functionName: 'swapExactETHForTokens',
            args: [amountOutMin, bestRoutes, EXPECTED, deadline]
          });
          swapRouter = AERODROME_ROUTER;
          swapValue = { value: amountIn };
        } else {
          // USDC-units path (quoteToken === USDC, direct route)
          swapData = encodeFunctionData({
            abi: AERO_ROUTER_ABI,
            functionName: 'swapExactTokensForTokens',
            args: [bestAmountIn, amountOutMin, bestRoutes, EXPECTED, deadline]
          });
          swapRouter = AERODROME_ROUTER;
          swapValue = {};
        }
      }
      console.log('Selector:', swapData.slice(0, 10));

      let gas;
      try {
        gas = await publicClient.estimateGas({ to: swapRouter, data: swapData, account: EXPECTED, ...swapValue });
        console.log('Estimated gas:', gas.toString());
      } catch (e) {
        console.log('Gas estimate failed:', e.message.slice(0, 200));
        emitJson({ error: `gas estimate failed: ${e.message}` });
        process.exit(3);
      }

      console.log('Broadcasting...');
      const hash = await walletClient.sendTransaction({
        to: swapRouter,
        data: swapData,
        gas,
        gasPrice: await publicClient.getGasPrice(),
        ...swapValue,
      });
      console.log('Tx hash:', hash);

      const receipt = await publicClient.waitForTransactionReceipt({ hash, timeout: 60000 });
      console.log('Confirmed! Block:', receipt.blockNumber?.toString());
      console.log('Gas used:', receipt.gasUsed?.toString());
      console.log('Status:', receipt.status);

      const tokenAfter = await readTokenBalance(tokenAddr, account.address, publicClient);
      const tokenReceived = tokenAfter > tokenBefore ? tokenAfter - tokenBefore : 0n;
      emitJson({
        hash,
        blockNumber: receipt.blockNumber?.toString(),
        gasUsed: receipt.gasUsed?.toString(),
        status: receipt.status,
        tokenOut: tokenAddr,
        amountEth: amountSpec,
        expectedOut: formatEther(bestExpectedOut),
        actualTokenOut: formatEther(tokenReceived),
        router: bestRouter === UNISWAP_V3_ROUTER ? 'uniswap_v3' : 'aerodrome',
        fee: bestFee ? bestFee / 10000 : null,
      });
    } catch (e) {
      const err = `Quote failed: ${e.message.slice(0, 200)}`;
      console.error(err);
      emitJson({ error: err, tokenOut: tokenAddr });
      process.exit(5);
    }
  }
}

main().catch(e => { console.error('ERROR:', e.message); emitJson({ error: e.message }); process.exit(1); });
