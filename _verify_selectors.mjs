// Read-only selector verification using viem (keccak256 is authoritative).
// NO broadcast, NO signing, NO swap. Only eth_getCode + selector math.
import { keccak256, stringToBytes, createPublicClient, http, encodeFunctionData } from '/home/kenzodadon/onchain-survival/node_modules/viem/index.js';
import { base } from '/home/kenzodadon/onchain-survival/node_modules/viem/chains/index.js';

const sel = (sig) => keccak256(stringToBytes(sig)).slice(0, 10);

// Cross-check viem keccak against known constants.
console.log('SANITY keccak(transfer(address,uint256)) =', sel('transfer(address,uint256)'), '(expect 0xa9059cbb)');
console.log('SANITY keccak(getAmountsOut(uint256,address[])) =', sel('getAmountsOut(uint256,address[])'), '(expect 0x054d50d4)');
console.log();

const ROUTER = '0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43';
const CLF_CORRECT = '0x5e7BB104d84c7CB9B682AaC2F3d509f5F406809A';
const CLF_WRONG   = '0x5e7BB1040E93aa4b081977ec9A4105B8fe699B5a';
const POOLFACTORY = '0x420DD381b31aEf6683db6B902084cB0FFECe40Da';

const client = createPublicClient({ chain: base, transport: http('https://mainnet.base.org') });

const [codeRouter, codeCLF_c, codeCLF_w, codePF] = await Promise.all([
  client.getCode({ address: ROUTER }),
  client.getCode({ address: CLF_CORRECT }),
  client.getCode({ address: CLF_WRONG }),
  client.getCode({ address: POOLFACTORY }),
]);

const countIn = (code, selector) => (code.toLowerCase().split(selector.toLowerCase()).length - 1);

const sigs = [
  ['swapExactETHForTokens 4-field (CLAIMED 0x903638a4)', 'swapExactETHForTokens(uint256,(address,address,bool,address)[],address,uint256)'],
  ['swapExactETHForTokens 3-field (CLAIMED 0x67ffb66a)',  'swapExactETHForTokens(uint256,(address,address,bool)[],address,uint256)'],
  ['getAmountsOut 4-field (CLAIMED 0x5509a1ac)',         'getAmountsOut(uint256,(address,address,bool,address)[])'],
  ['getAmountsOut 3-field (CLAIMED 0x9881fcb4)',         'getAmountsOut(uint256,(address,address,bool)[])'],
];

console.log('ROUTER 0xcF77... bytecode bytes:', (codeRouter.length - 2) / 2);
console.log('BYTECODE PRESENCE (substring count of 4-byte selector):');
for (const [label, sig] of sigs) {
  const s = sel(sig);
  console.log(`  ${s}  count=${countIn(codeRouter, s)}  ${label}`);
}

console.log();
console.log('CLF correct 0x5e7BB104d84c... :', codeCLF_c === '0x' ? 'NO CODE (0x)' : `code ${ (codeCLF_c.length-2)/2 }B`);
console.log('CLF wrong   0x5e7BB1040E93... :', codeCLF_w === '0x' ? 'NO CODE (0x)' : `code ${ (codeCLF_w.length-2)/2 }B`);
console.log('PoolFactory 0x420DD381...     :', codePF === '0x' ? 'NO CODE (0x)' : `code ${ (codePF.length-2)/2 }B`);

// Direct behavioral proof via eth_call (read-only): 4-field getAmountsOut must RETURN,
// 3-field getAmountsOut must REVERT (function not present -> fallback/selector revert).
const WETH = '0x4200000000000000000000000000000000000006';
const cbBTC = '0xcbB7C0000aB88B473b1f5aFd9efCfEDd5868a36'; // liquid Aerodrome V2/wrap pair
const abi4 = [{ type:'function', name:'getAmountsOut', stateMutability:'view',
  inputs:[{type:'uint256',name:'amountIn'},{type:'tuple[]',name:'routes',components:[
    {name:'from',type:'address'},{name:'to',type:'address'},{name:'stable',type:'bool'},{name:'factory',type:'address'}]}],
  outputs:[{type:'uint256[]',name:'amounts'}] }];
const abi3 = [{ type:'function', name:'getAmountsOut', stateMutability:'view',
  inputs:[{type:'uint256',name:'amountIn'},{type:'tuple[]',name:'routes',components:[
    {name:'from',type:'address'},{name:'to',type:'address'},{name:'stable',type:'bool'}]}],
  outputs:[{type:'uint256[]',name:'amounts'}] }];

const amountIn = 100000000000000n; // 1e14 wei, tiny, read-only quote
const routes4 = [{ from: WETH, to: cbBTC, stable: false, factory: POOLFACTORY }];
const routes3 = [{ from: WETH, to: cbBTC, stable: false }];

async function tryQuote(abi, routes, tag) {
  try {
    const data = encodeFunctionData({ abi, functionName: 'getAmountsOut', args: [amountIn, routes] });
    const res = await client.call({ to: ROUTER, data });
    console.log(`  [${tag}] selector ${data.slice(0,10)} -> RETURN ${res.data.slice(0,42)}...`);
  } catch (e) {
    console.log(`  [${tag}] selector ${sel('getAmountsOut'+abi[0].inputs.map(i=>'('+i.type+')').join(',')).slice ? '' : ''}-> REVERT ${String(e.message).slice(0,90)}`);
  }
}
console.log();
console.log('BEHAVIORAL eth_call (read-only quote):');
await tryQuote(abi4, routes4, '4-field 0x5509a1ac');
await tryQuote(abi3, routes3, '3-field 0x9881fcb4');
