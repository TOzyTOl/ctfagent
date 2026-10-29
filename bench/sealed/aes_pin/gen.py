"""aes_pin — a 6-digit PIN stretched into an AES key by one MD5 round.

Difficulty: easy-medium. The point of the item: recognise that the keyspace
is 10^6 regardless of the cipher, and let the machine do the walking.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _common import write_out  # noqa: E402

PIN = "473829"
FLAG = b"flag{6_d1g1t_p1n_1s_n0t_a_k3y}"


def main() -> None:
    key = hashlib.md5(PIN.encode()).digest()
    ct = AES.new(key, AES.MODE_ECB).encrypt(pad(FLAG, 16))
    write_out(
        "aes_pin",
        {
            "cipher.hex": ct.hex() + "\n",
            "note.txt": (
                "保险库固件升级包里的校验固件被提取出来了。\n"
                "主密钥 = MD5(6 位数字 PIN 的 ASCII 字节)，取 16 字节摘要，AES-128-ECB 加密，PKCS#7 填充。\n"
                "PIN 是 000000–999999 之间的一个 6 位数字。密文如下（hex）：\n"
            ) + ct.hex() + "\n",
        },
        answer=FLAG.decode(),
    )


if __name__ == "__main__":
    main()
