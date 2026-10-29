"""Solver for sha256_ext — hand-rolled SHA-256 with injected state.

hashlib cannot resume from a mid-stream state, which is exactly what the
forgery needs. This is the reference "no special tools" path a worker can
take: the compress function, the padding rule, and the recovered state.
"""

from __future__ import annotations

import hashlib  # only to cross-check the captured mac
import re
from pathlib import Path

K = [
    0x428A2F98, 0x71374491, 0xB5C0FBCF, 0xE9B5DBA5, 0x3956C25B, 0x59F111F1, 0x923F82A4, 0xAB1C5ED5,
    0xD807AA98, 0x12835B01, 0x243185BE, 0x550C7DC3, 0x72BE5D74, 0x80DEB1FE, 0x9BDC06A7, 0xC19BF174,
    0xE49B69C1, 0xEFBE4786, 0x0FC19DC6, 0x240CA1CC, 0x2DE92C6F, 0x4A7484AA, 0x5CB0A9DC, 0x76F988DA,
    0x983E5152, 0xA831C66D, 0xB00327C8, 0xBF597FC7, 0xC6E00BF3, 0xD5A79147, 0x06CA6351, 0x14292967,
    0x27B70A85, 0x2E1B2138, 0x4D2C6DFC, 0x53380D13, 0x650A7354, 0x766A0ABB, 0x81C2C92E, 0x92722C85,
    0xA2BFE8A1, 0xA81A664B, 0xC24B8B70, 0xC76C51A3, 0xD192E819, 0xD6990624, 0xF40E3585, 0x106AA070,
    0x19A4C116, 0x1E376C08, 0x2748774C, 0x34B0BCB5, 0x391C0CB3, 0x4ED8AA4A, 0x5B9CCA4F, 0x682E6FF3,
    0x748F82EE, 0x78A5636F, 0x84C87814, 0x8CC70208, 0x90BEFFFA, 0xA4506CEB, 0xBEF9A3F7, 0xC67178F2,
]
H0 = [0x6A09E667, 0xBB67AE85, 0x3C6EF372, 0xA54FF53A, 0x510E527F, 0x9B05688C, 0x1F83D9AB, 0x5BE0CD19]
M32 = 0xFFFFFFFF


def _rotr(x: int, n: int) -> int:
    return ((x >> n) | (x << (32 - n))) & M32


def compress(state: list[int], block: bytes) -> list[int]:
    w = [int.from_bytes(block[i : i + 4], "big") for i in range(0, 64, 4)]
    for t in range(16, 64):
        s0 = _rotr(w[t - 15], 7) ^ _rotr(w[t - 15], 18) ^ (w[t - 15] >> 3)
        s1 = _rotr(w[t - 2], 17) ^ _rotr(w[t - 2], 19) ^ (w[t - 2] >> 10)
        w.append((w[t - 16] + s0 + w[t - 7] + s1) & M32)
    a, b, c, d, e, f, g, h = state
    for t in range(64):
        S1 = _rotr(e, 6) ^ _rotr(e, 11) ^ _rotr(e, 25)
        ch = (e & f) ^ (~e & g)
        t1 = (h + S1 + ch + K[t] + w[t]) & M32
        S0 = _rotr(a, 2) ^ _rotr(a, 13) ^ _rotr(a, 22)
        maj = (a & b) ^ (a & c) ^ (b & c)
        t2 = (S0 + maj) & M32
        h, g, f, e, d, c, b, a = g, f, e, (d + t1) & M32, c, b, a, (t1 + t2) & M32
    return [(s + v) & M32 for s, v in zip(state, (a, b, c, d, e, f, g, h))]


def pad(message_len: int) -> bytes:
    bit_len = message_len * 8
    return b"\x80" + b"\x00" * ((55 - message_len) % 64) + bit_len.to_bytes(8, "big")


materials = Path(__file__).resolve().parents[2] / "challenges" / "sha256_ext" / "materials"
text = (materials / "server.txt").read_text(encoding="utf-8")
mac = re.search(r"mac\s*=\s*([0-9a-f]{64})", text).group(1)
message = re.search(r"message = (\S+)", text).group(1).encode()
suffix = b";role=admin"

# sanity: this compress() implementation, run from the initial state, must
# agree with hashlib before we trust it with an injected state
state = list(H0)
for i in range(0, len(b"abc" + pad(3)), 64):
    state = compress(state, (b"abc" + pad(3))[i : i + 64])
assert "".join(f"{w:08x}" for w in state) == hashlib.sha256(b"abc").hexdigest()

secret_len = 16
state = [int(mac[i * 8 : (i + 1) * 8], 16) for i in range(8)]  # state after secret ‖ message ‖ glue
inner = secret_len + len(message)                              # bytes hashed before the glue
outer = inner + len(pad(inner)) + len(suffix)                  # total length at the final block
# the glue is already baked into the recovered state — only the suffix and
# the final padding (for the full forged length) remain to compress
stream = suffix + pad(outer)
for i in range(0, len(stream), 64):
    state = compress(state, stream[i : i + 64])
print("".join(f"{w:08x}" for w in state))
