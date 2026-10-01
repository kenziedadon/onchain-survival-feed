import json, urllib.request

# ---- keccak-256 (Ethereum variant) ----
def rol(x, n):
    n %= 64
    return ((x << n) | (x >> (64 - n))) & ((1<<64)-1)

RC = [0x0000000000000001,0x0000000000008082,0x800000000000808A,0x8000000080008000,
      0x000000000000808B,0x0000000080000001,0x8000000080008081,0x8000000000008009,
      0x000000000000008A,0x0000000000000088,0x0000000080008009,0x000000008000000A,
      0x000000008000808B,0x800000008000008B,0x8000000080008089,0x8000000000008003,
      0x8000000080008002,0x0000000080000080,0x000000000000800A,0x800000008000000A,
      0x8000000080008081,0x8000000080008080,0x0000000080000001,0x8000000080008008]
ROT = [[0,36,3,41,18],[1,44,10,45,2],[62,6,43,15,61],[28,55,25,21,56],[27,20,39,8,14]]

def keccak_f(s):
    for rnd in range(24):
        C=[s[x][0]^s[x][1]^s[x][2]^s[x][3]^s[x][4] for x in range(5)]
        D=[C[(x-1)%5]^rol(C[(x+1)%5],1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                s[x][y]^=D[x]
        B=[[0]*5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                B[y][(2*x+3*y)%5]=rol(s[x][y],ROT[x][y])
        for x in range(5):
            for y in range(5):
                s[x][y]=B[x][y]^((~B[(x+1)%5][y])&B[(x+2)%5][y])
        s[0][0]^=RC[rnd]
    return s

def keccak256(msg):
    rate=136
    s=[[0]*5 for _ in range(5)]
    m=bytearray(msg); m.append(0x01)
    while len(m)%rate!=0: m.append(0x00)
    m[-1]^=0x80
    for off in range(0,len(m),rate):
        blk=m[off:off+rate]
        for i in range(rate//8):
            lane=int.from_bytes(blk[i*8:i*8+8],'little')
            s[i%5][i//5]^=lane
        keccak_f(s)
    out=bytearray()
    while len(out)<32:
        for i in range(rate//8):
            out+=s[i%5][i//5].to_bytes(8,'little')
            if len(out)>=32: break
        if len(out)<32: keccak_f(s)
    return bytes(out[:32])

def sel(sig):
    return "0x"+keccak256(sig.encode()).hex()[:8]

print("VALIDATE keccak impl:")
print("  keccak256('') =", "0x"+keccak256(b"").hex())
print("  transfer()    =", sel("transfer(address,uint256)"), "(expect 0xa9059cbb)")
print()

sigs = {
 "swapExactETHForTokens(uint256,(address,address,bool,address)[],address,uint256)":"0x903638a4 EXPECT PRESENT",
 "swapExactETHForTokens(uint256,(address,address,bool)[],address,uint256)":"0x67ffb66a EXPECT ABSENT",
 "getAmountsOut(uint256,address[])":"candidate A",
 "getAmountsOut(uint256,(address,address,bool)[])":"candidate B (->0x9881fcb4?)",
 "getAmountsOut(uint256,(address,address,bool,address)[])":"candidate C (->0x5509a1ac?)",
}
print("SELECTOR COMPUTATION (keccak):")
for sig,exp in sigs.items():
    print(f"  {sel(sig)}  {sig}   [{exp}]")
print()

def rpc(method, params):
    data=json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}).encode()
    req=urllib.request.Request("https://mainnet.base.org",data=data,
        headers={"Content-Type":"application/json","User-Agent":"Mozilla/5.0"})
    with urllib.request.urlopen(req,timeout=30) as r:
        j=json.loads(r.read().decode())
    if "error" in j: raise RuntimeError(j["error"])
    return j["result"]

router="0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43"
clf_correct="0x5e7BB104d84c7CB9B682AaC2F3d509f5F406809A"
clf_wrong="0x5e7BB1040E93aa4b081977ec9A4105B8fe699B5a"
poolfactory="0x420DD381b31aEf6683db6B902084cB0FFECe40Da"

code_router=rpc("eth_getCode",[router,"latest"])
code_clf_c=rpc("eth_getCode",[clf_correct,"latest"])
code_clf_w=rpc("eth_getCode",[clf_wrong,"latest"])
code_pf=rpc("eth_getCode",[poolfactory,"latest"])

def has(code,selector):
    return code.lower().count(selector.lower())

print("BYTECODE PRESENCE (substring count of 4-byte selector):")
for name,sig in [("swapCorrect","swapExactETHForTokens(uint256,(address,address,bool,address)[],address,uint256)"),
                 ("swapWrong","swapExactETHForTokens(uint256,(address,address,bool)[],address,uint256)"),
                 ("amtOutA","getAmountsOut(uint256,address[])"),
                 ("amtOutB","getAmountsOut(uint256,(address,address,bool)[])"),
                 ("amtOutC","getAmountsOut(uint256,(address,address,bool,address)[])")]:
    s=sel(sig)
    print(f"  {s}  count={has(code_router,s):>3}  {sig}")

print()
print("ROUTER code length (bytes):", (len(code_router)-2)//2)
print("CLF correct 0x5e7BB104d84c... has code:", code_clf_c not in ("0x","0x0"), "len",(len(code_clf_c)-2)//2)
print("CLF wrong   0x5e7BB1040E93... has code:", code_clf_w not in ("0x","0x0"), "len",(len(code_clf_w)-2)//2)
print("PoolFactory 0x420DD381...     has code:", code_pf not in ("0x","0x0"), "len",(len(code_pf)-2)//2)
