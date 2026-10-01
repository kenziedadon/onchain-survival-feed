#!/usr/bin/env node
/**
 * signer.js — raw transaction signer for the Base trend bot.
 * Reads the private key from .payer_key_tmp, derives the address,
 * verifies it matches the expected wallet, and can:
 *   - getBalance: returns ETH balance
 *   - getNonce: returns current nonce
 *   - signAndBroadcast: signs a raw tx and broadcasts via Base RPC
 *
 * Usage (JSON over stdio):
 *   echo '{"cmd":"getBalance"}' | node signer.js
 *   echo '{"cmd":"getNonce"}' | node signer.js
 *   echo '{"cmd":"signAndBroadcast","tx":{"to":"0x...","value":"0x...","data":"0x...","gas":"0x...","gasPrice":"0x..."}}' | node signer.js
 */
const fs = require('fs');
const { http, createPublicClient, createWalletClient, formatEther, parseEther } = require('viem');
const { privateKeyToAccount } = require('viem/accounts');
const { base } = require('viem/chains');

const EXPECTED = '0xe7a16Be2aF1eCE8922d9d3aD55C03e29004e540F';
const KEY_FILE = '/home/kenzodadon/onchain-survival/.payer_key_tmp';
const RPC = 'https://mainnet.base.org';

let account = null;
let publicClient = null;
let walletClient = null;

function ensureInitialized() {
  if (account) return; // already initialized
  const key = fs.readFileSync(KEY_FILE, 'utf8').trim();
  account = privateKeyToAccount(key);
  if (account.address.toLowerCase() !== EXPECTED.toLowerCase()) {
    console.error(JSON.stringify({ error: 'KEY_MISMATCH', derived: account.address, expected: EXPECTED }));
    process.exit(2);
  }
  publicClient = createPublicClient({ chain: base, transport: http(RPC) });
  walletClient = createWalletClient({ account, chain: base, transport: http(RPC) });
}

async function getBalance() {
  ensureInitialized();
  const bal = await publicClient.getBalance({ address: account.address });
  return { address: account.address, balance: bal.toString(), balanceEth: formatEther(bal) };
}

async function getNonce() {
  ensureInitialized();
  const nonce = await publicClient.getTransactionCount({ address: account.address });
  return { address: account.address, nonce };
}

async function signAndBroadcast(tx) {
  ensureInitialized();
  // Build raw transaction
  const request = {
    to: tx.to,
    value: tx.value ? BigInt(tx.value) : 0n,
    data: tx.data || '0x',
    gas: tx.gas ? BigInt(tx.gas) : undefined,
    gasPrice: tx.gasPrice ? BigInt(tx.gasPrice) : undefined,
  };
  
  // Estimate gas if not provided
  if (!request.gas) {
    request.gas = await publicClient.estimateGas({ account: account.address, ...request });
  }
  
  // Get gas price if not provided
  if (!request.gasPrice) {
    request.gasPrice = await publicClient.getGasPrice();
  }
  
  // Sign and send
  const hash = await walletClient.sendTransaction(request);
  
  // Wait for receipt (with timeout)
  const receipt = await publicClient.waitForTransactionReceipt({ hash, timeout: 60000 });
  
  return {
    hash,
    blockNumber: receipt.blockNumber?.toString(),
    gasUsed: receipt.gasUsed?.toString(),
    status: receipt.status,
    from: receipt.from,
    to: receipt.to,
  };
}

async function signSwap(tokenOut, amountEth, slippageBps = 100) {
  // For Aerodrome V2-style swap, we'd need to interact with the router
  // For now, this is a placeholder that returns what we'd need to build
  // The actual swap requires interacting with the DEX router contract
  return { error: 'SWAP_NOT_IMPLEMENTED', tokenOut, amountEth, slippageBps };
}

async function handle(req) {
  const { cmd } = req;
  switch (cmd) {
    case 'getBalance': return await getBalance();
    case 'getNonce': return await getNonce();
    case 'signAndBroadcast': return await signAndBroadcast(req.tx);
    case 'signSwap': return await signSwap(req.tokenOut, req.amountEth, req.slippageBps);
    default: return { error: 'UNKNOWN_CMD', cmd };
  }
}

ensureInitialized();

// Read JSON from stdin
let data = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => data += chunk);
process.stdin.on('end', async () => {
  try {
    const req = JSON.parse(data);
    const result = await handle(req);
    console.log(JSON.stringify(result, null, 2));
  } catch (e) {
    console.log(JSON.stringify({ error: e.message, stack: e.stack }));
    process.exit(1);
  }
});
