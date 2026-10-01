"""Minimal pure-Python Keccak-256 (for computing Ethereum event topics).

Keccak-f[1600], 24 rounds, rate 1088 bits, 0x01 domain padding (NOT SHA3's 0x06).
Verified against known vectors:
  keccak256("")   = c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470
  keccak256("abc")= 4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45
"""

RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]

ROT = [
    [0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61], [28, 55, 25, 21, 56],
    [27, 20, 39, 8, 14],
]

MASK = (1 << 64) - 1


def _rotl(x, n):
    return ((x << n) | (x >> (64 - n))) & MASK


def _keccak_f(state):
    # state: list of 25 lanes (64-bit ints)
    for rnd in range(24):
        # theta
        C = [state[x] ^ state[x + 5] ^ state[x + 10] ^ state[x + 15] ^ state[x + 20] for x in range(5)]
        D = [C[(x - 1) % 5] ^ _rotl(C[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                state[x + 5 * y] ^= D[x]
        # rho + pi
        B = [0] * 25
        for x in range(5):
            for y in range(5):
                B[y + 5 * ((2 * x + 3 * y) % 5)] = _rotl(state[x + 5 * y], ROT[x][y])
        # chi
        for x in range(5):
            for y in range(5):
                state[x + 5 * y] = B[x + 5 * y] ^ ((~B[((x + 1) % 5) + 5 * y]) & B[((x + 2) % 5) + 5 * y])
        # iota
        state[0] ^= RC[rnd]
    return state


def keccak256(data: bytes) -> bytes:
    rate = 136  # bytes (1088 bits)
    # pad with Keccak pad: append 0x01 then zeros, terminate with 0x80 in last byte
    m = bytearray(data)
    m.append(0x01)
    while len(m) % rate != 0:
        m.append(0x00)
    m[-1] ^= 0x80
    # build state
    state = [0] * 25
    for off in range(0, len(m), rate):
        block = m[off: off + rate]
        for i in range(rate // 8):
            lane = int.from_bytes(block[i * 8: i * 8 + 8], "little")
            state[i] ^= lane
        _keccak_f(state)
    out = bytearray()
    while len(out) < 32:
        for i in range(25):
            if len(out) >= 32:
                break
            out += state[i].to_bytes(8, "little")
    return bytes(out[:32])


def event_topic(signature: str) -> str:
    return "0x" + keccak256(signature.encode()).hex()


if __name__ == "__main__":
    assert keccak256(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    assert keccak256(b"abc").hex() == "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"
    print("keccak self-test passed")
    print("PoolCreated(address,address,bool,address,uint256) =", event_topic("PoolCreated(address,address,bool,address,uint256)"))
    print("PairCreated(address,address,address,uint256)      =", event_topic("PairCreated(address,address,address,uint256)"))
