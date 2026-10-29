"""Solver for aes_pin — brute the 10^6 keyspace, first block only."""

from __future__ import annotations

import hashlib
from pathlib import Path

from Crypto.Cipher import AES

materials = Path(__file__).resolve().parents[2] / "challenges" / "aes_pin" / "materials"
hex_text = (materials / "cipher.hex").read_text().strip()
ct_first = bytes.fromhex(hex_text)[:16]  # the flag starts in block 0

for n in range(1_000_000):
    key = hashlib.md5(f"{n:06d}".encode()).digest()
    if AES.new(key, AES.MODE_ECB).decrypt(ct_first).startswith(b"flag{"):
        # decrypt the whole thing once for the final answer
        full = AES.new(key, AES.MODE_ECB).decrypt(bytes.fromhex(hex_text))
        print(full[: full.index(b"}") + 1].decode())
        break
else:
    raise SystemExit("no pin matched")
