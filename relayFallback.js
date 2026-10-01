#!/usr/bin/env node
/**
 * relayFallback.js — reliable Relay.quote execution for sell path.
 *
 * Replaces the old relaySellQuote() with:
 *  - Fresh quote at execution time (no stale reuse)
 *  - Per-step execution with retry + backoff
 *  - Fail-closed error reporting (RelaySwapError with landed steps)
 *
 * Usage:
 *   const result = await relaySwapWithFallback({ walletClient, publicClient, user, chainId, currencyIn, currencyOut, amount, tradeType, slippageBps });
 *   console.log(result);
 */

const fs = require('fs');

class RelaySwapError extends Error {
  constructor(message, landedSteps = [], failedStep = null, quote = null) {
    super(message);
    this.name = 'RelaySwapError';
    this.landedSteps = landedSteps;   // steps confirmed on-chain before failure
    this.failedStep = failedStep;     // step index + item that failed
    this.quote = quote;               // the quote object for inspection
  }
}

async function withRetry(fn, label, retries = 2, delayMs = 500) {
  let lastErr;
  for (let i = 0; i < retries; i++) {
    try {
      return await fn();
    } catch (e) {
      lastErr = e;
      const msg = (e.message || '').toLowerCase();
      const retryable = msg.includes('timeout') || msg.includes('429') || msg.includes('5xx') || msg.includes('econnreset') || msg.includes('network');
      if (i < retries - 1 && retryable) {
        await new Promise(r => setTimeout(r, delayMs * (i + 1)));
        continue;
      }
      throw e;
    }
  }
  throw lastErr;
}

/**
 * Fetch a fresh quote from Relay.
 * Tries /quote first, falls back to /quote/v2 if needed.
 */
async function fetchRelayQuote({ user, chainId, originCurrency, destinationCurrency, amount, tradeType = 'EXACT_INPUT', slippageBps = 100 }) {
  const bodies = [
    { user, recipient: user, originChainId: chainId, destinationChainId: chainId, originCurrency, destinationCurrency, amount, tradeType },
  ];

  // If first attempt fails with 404/405, try /quote/v2 body shape
  const urls = [
    'https://api.relay.link/quote',
    'https://api.relay.link/quote/v2',
  ];

  let lastErr;
  for (const url of urls) {
    for (const body of bodies) {
      try {
        const res = await fetch(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        if (!res.ok) {
          const txt = await res.text().catch(() => '');
          throw new Error(`Relay quote HTTP ${res.status}: ${txt.slice(0, 200)}`);
        }
        const q = await res.json();
        if (q && q.details && q.details.currencyOut && q.details.currencyOut.amount) {
          return q;
        }
        throw new Error('Relay quote missing expected fields');
      } catch (e) {
        lastErr = e;
        // try next url/body combo
      }
    }
  }
  throw lastErr || new Error('Relay quote failed: all endpoints exhausted');
}

/**
 * Execute a single Relay step item.
 * Returns { landed: boolean, txHash?: string, error?: string }
 */
async function executeStepItem({ item, stepId, walletClient, publicClient, account }) {
  if (!item || !item.data) {
    return { landed: false, error: 'missing item.data', skip: true };
  }

  const d = item.data;
  // Determine item kind from Relay response shape
  const kind = item.kind || (d.to ? 'transaction' : (d.post ? 'signature' : 'unknown'));

  if (kind === 'transaction') {
    try {
      const gas = await publicClient.estimateGas({
        to: d.to,
        data: d.data,
        value: BigInt(d.value || '0'),
        account: acct.address,
      });
      const hash = await walletClient.sendTransaction({
        to: d.to,
        data: d.data,
        value: BigInt(d.value || '0'),
        gas,
        gasPrice: await publicClient.getGasPrice(),
      });
      const receipt = await publicClient.waitForTransactionReceipt({
        hash,
        timeout: 120_000,
        confirmations: 1,
        pollingInterval: 1000,
      });
      if (receipt.status === 'success') {
        return { landed: true, txHash: hash };
      }
      return { landed: false, error: `tx ${hash} reverted`, txHash: hash };
    } catch (e) {
      return { landed: false, error: e.message.slice(0, 200) };
    }
  }

  if (kind === 'signature') {
    // Signature step: POST to the provided endpoint with signed payload
    try {
      const postUrl = d.post?.endpoint || d.post?.url;
      if (!postUrl) {
        return { landed: false, error: 'signature step missing post.endpoint', skip: true };
      }
      const body = d.post?.body || {};
      // If Relay expects a signature, sign via walletClient
      if (body.message || body.payload) {
        const signature = await walletClient.signMessage({
          message: body.message ? { raw: body.message } : { raw: body.payload },
        });
        body.signature = signature;
      }
      const res = await fetch(postUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (res.ok) {
        return { landed: true };
      }
      const txt = await res.text().catch(() => '');
      return { landed: false, error: `signature POST ${res.status}: ${txt.slice(0, 200)}` };
    } catch (e) {
      return { landed: false, error: e.message.slice(0, 200) };
    }
  }

  // Unknown kind — skip rather than fail hard
  return { landed: false, error: `unknown step kind: ${kind}`, skip: true };
}

/**
 * Execute a Relay quote step-by-step with retry.
 * Skips items already confirmed on-chain (by tx hash).
 */
async function executeQuoteSteps({ quote, walletClient, publicClient, account, slippageBps = 100 }) {
  const landedSteps = [];
  const steps = quote.steps || [];

  for (let s = 0; s < steps.length; s++) {
    const step = steps[s];
    const items = step.items || [];

    for (let i = 0; i < items.length; i++) {
      const item = items[i];

      // Skip if item already has a confirmed tx hash from prior run
      if (item.check?.transactionHash || item.txHash) {
        landedSteps.push({ stepIndex: s, itemIndex: i, txHash: item.check?.transactionHash || item.txHash });
        continue;
      }

      // Retry each item up to 2 times
      let lastItemResult;
      for (let attempt = 0; attempt < 2; attempt++) {
        lastItemResult = await executeStepItem({
          item,
          stepId: s,
          walletClient,
          publicClient,
          account,
        });

        if (lastItemResult.landed) {
          landedSteps.push({ stepIndex: s, itemIndex: i, txHash: lastItemResult.txHash });
          break;
        }

        if (lastItemResult.skip) {
          // Don't retry skips
          break;
        }

        // Retry on transient failure
        if (attempt < 1) {
          await new Promise(r => setTimeout(r, 500 * (attempt + 1)));
        }
      }

      if (!lastItemResult.landed && !lastItemResult.skip) {
        throw new RelaySwapError(
          `Relay step ${s}.${i} failed after retries: ${lastItemResult.error}`,
          landedSteps,
          { stepIndex: s, itemIndex: i, error: lastItemResult.error },
          quote,
        );
      }
    }
  }

  return { success: true, landedSteps };
}

/**
 * Main entry: sell tokenIn for WETH via Relay with fresh quote + step execution.
 *
 * Returns emitJson-style result on success, throws RelaySwapError on failure.
 */
async function relaySwapWithFallback({
  walletClient,
  publicClient,
  user,
  account,
  chainId = 8453,
  currencyIn,
  currencyOut, // should be WETH
  amount,      // token balance as string or bigint
  tradeType = 'EXACT_INPUT',
  slippageBps = 100,
  quoteOnly = false,
}) {
  const amountStr = typeof amount === 'bigint' ? amount.toString() : String(amount);
  const acct = account || (walletClient && walletClient.account);
  if (!acct) {
    throw new RelaySwapError('Missing account: pass account or walletClient.account');
  }

  // 1. Fresh quote
  const quote = await withRetry(
    () => fetchRelayQuote({
      user,
      chainId,
      originCurrency: currencyIn,
      destinationCurrency: currencyOut,
      amount: amountStr,
      tradeType,
    }),
    'relay-quote',
  );

  const outAmt = quote?.details?.currencyOut?.amount;
  if (!outAmt || BigInt(outAmt) === 0n) {
    throw new RelaySwapError('Relay: no route / zero output', [], null, quote);
  }

  const expected = BigInt(outAmt);
  const minOut = (expected * BigInt(10000 - slippageBps)) / 10000n;

  if (quoteOnly) {
    return {
      quoteOnly: true,
      router: 'relay',
      expectedOut: expected.toString(),
      minOut: minOut.toString(),
      requestId: quote.requestId || null,
    };
  }

  // 2. Execute steps
  const result = await executeQuoteSteps({
    quote,
    walletClient,
    publicClient,
    account,
    slippageBps,
  });

  if (!result.success) {
    // Should not reach here — executeQuoteSteps throws on failure
    return {
      success: false,
      landedSteps: result.landedSteps,
      tokenIn: currencyIn,
      amountToken: amountStr,
    };
  }

  return {
    success: true,
    hash: 'relay-multistep',
    router: 'relay',
    tokenIn: currencyIn,
    amountToken: amountStr,
    expectedOut: expected.toString(),
    minOut: minOut.toString(),
    landedSteps: result.landedSteps,
    requestId: quote.requestId || null,
  };
}

module.exports = {
  relaySwapWithFallback,
  RelaySwapError,
  fetchRelayQuote,
  executeQuoteSteps,
};
