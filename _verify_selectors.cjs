// Read-only verification. NO broadcast/sign/swap. Only eth_getCode + eth_call (view) reads.
const NODE_MODULES = '/home/kenzodadon/onchain-survival/node_modules';
if (!process.env.NODE_PATH || !process.env.NODE_PATH.includes(NODE_MODULES)) {
  process.env.NODE_PATH = NODE_MODULES + ':' + (process.env.NODE_PATH || '');
  require('module').Module._initPaths();
}
const { keccak256, stringToBytes, createPublicClient, http, encodeFunctionData, decodeAbiParameters, getAddress } = require('viem');
const { base } = require('viem/chains');

const sel = (sig) => keccak256(stringToBytes(sig)).slice(0, 10);
const RAW = (s) => s.toLowerCase().replace(/^0x/, ''); // bare hex for bytecode substring search

console.log('SANITY keccak(transfer(address,uint256)) =', sel('transfer(address,uint256)'), '(expect 0xa9059cbb)');
console.log();

const ROUTER = '0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43';
const CLF_CORRECT = '0x5e7BB104d84c7CB9B682AaC2F3d509f5F406809A';
const CLF_WRONG   = '0x5e7BB1040E93aa4b081977ec9A4105B8fe699B5a';
const POOLFACTORY = '0x420DD381b31aEf6683db6B902084cB0FFECe40Da';

const client = createPublicClient({ chain: base, transport: http('https://mainnet.base.org') });
const lenOf = (c) => (c && c !== '0x' && c !== undefined) ? (c.length - 2) / 2 : 0;
const hasSel = (code, selector) => code.toLowerCase().split(RAW(selector)).length - 1;

(async () => {
  const [codeRouter, codeCLF_c, codeCLF_w, codePF] = await Promise.all([
    client.getCode({ address: ROUTER }).catch(() => undefined),
    client.getCode({ address: CLF_CORRECT }).catch(() => undefined),
    client.getCode({ address: CLF_WRONG }).catch(() => undefined),
    client.getCode({ address: POOLFACTORY }).catch(() => undefined),
  ]);

  console.log('ROUTER 0xcF77... bytecode bytes:', lenOf(codeRouter));
  console.log('CLF correct 0x5e7BB104d84c... :', lenOf(codeCLF_c) ? `code ${lenOf(codeCLF_c)}B` : 'NO CODE');
  console.log('CLF wrong   0x5e7BB1040E93... :', lenOf(codeCLF_w) ? `code ${lenOf(codeCLF_w)}B` : 'NO CODE');
  console.log('PoolFactory 0x420DD381...     :', lenOf(codePF) ? `code ${lenOf(codePF)}B` : 'NO CODE');
  console.log();

  const sigs = [
    ['swapExactETHForTokens 4-field (CLAIM present)   ', 'swapExactETHForTokens(uint256,(address,address,bool,address)[],address,uint256)'],
    ['swapExactETHForTokens 3-field (CLAIM ABSENT)     ', 'swapExactETHForTokens(uint256,(address,address,bool)[],address,uint256)'],
    ['getAmountsOut 4-field (CLAIM present)            ', 'getAmountsOut(uint256,(address,address,bool,address)[])'],
    ['getAmountsOut 3-field (CLAIM ABSENT)             ', 'getAmountsOut(uint256,(address,address,bool)[])'],
  ];
  console.log('COMPUTED SELECTORS (viem keccak; transfer() sanity above must = 0xa9059cbb):');
  for (const [label, sig] of sigs) console.log(`  ${sel(sig)}  ${label}`);
  console.log();
  console.log('BYTECODE PRESENCE (count of bare 4-byte selector in router code):');
  for (const [label, sig] of sigs) {
    const s = sel(sig);
    console.log(`  ${s}  count=${hasSel(codeRouter, s)}  ${label}`);
  }
  console.log();

  // Behavioral proof: 4-field getAmountsOut on liquid AERO/WETH must RETURN a real quote.
  const WETH = getAddress('0x4200000000000000000000000000000000000006');
  const AERO = getAddress('0x940181a94A35A4569E4529A3CDfB74e38FD98631');
  const abi4 = [{ type:'function', name:'getAmountsOut', stateMutability:'view',
    inputs:[{type:'uint256',name:'amountIn'},{type:'tuple[]',name:'routes',components:[
      {name:'from',type:'address'},{name:'to',type:'address'},{name:'stable',type:'bool'},{name:'factory',type:'address'}]}],
    outputs:[{type:'uint256[]',name:'amounts'}] }];
  const amountIn = 100000000000000n; // 1e14 wei, tiny read-only probe
  const routes4 = [{ from: WETH, to: AERO, stable: false, factory: getAddress(POOLFACTORY) }];

  const data = encodeFunctionData({ abi: abi4, functionName: 'getAmountsOut', args: [amountIn, routes4] });
  try {
    const res = await client.call({ to: ROUTER, data });
    const a = decodeAbiParameters([{ type: 'uint256[]' }], res.data);
    console.log(`BEHAVIORAL eth_call 4-field getAmountsOut (selector ${data.slice(0,10)}):`);
    console.log(`  amountIn = 1e14 wei WETH -> amounts[] = [${a[0].map(x=>x.toString()).join(', ')}]`);
    console.log('  => 4-field selector is LIVE and returns a quote (router accepts the corrected Route struct).');
  } catch (e) {
    console.log(`BEHAVIORAL eth_call 4-field getAmountsOut -> REVERT: ${String(e.shortMessage||e.message).slice(0,120)}`);
  }
})().catch(e => { console.error('FATAL', e); process.exit(1); });
