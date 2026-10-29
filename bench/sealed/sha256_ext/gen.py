"""sha256_ext — a length-extension forgery against SHA-256(prefix ‖ message).

Difficulty: medium-hard. The secret's *content* is unknowable and unneeded:
its length (16 bytes) plus one captured (message, mac) pair is enough to
predict the mac of message ‖ glue ‖ suffix. The worker has to implement
SHA-256 padding and state injection by hand — hashlib alone cannot do it.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _common import write_out  # noqa: E402

SECRET = hashlib.sha256(b"bench-secret-v1").digest()[:16]  # fixed 16 bytes
MESSAGE = b"user=guest"
SUFFIX = b";role=admin"


def glue_pad(total_len: int) -> bytes:
    bit_len = total_len * 8
    pad = b"\x80" + b"\x00" * ((55 - total_len) % 64) + bit_len.to_bytes(8, "big")
    return pad


def main() -> None:
    mac = hashlib.sha256(SECRET + MESSAGE).hexdigest()
    forged_len = 16 + len(MESSAGE) + len(glue_pad(16 + len(MESSAGE))) + len(SUFFIX)
    # answer computed with hashlib over the exact byte sequence the forgery
    # must equal — the ground truth the hand-rolled solver has to reproduce
    answer = hashlib.sha256(
        SECRET + MESSAGE + glue_pad(16 + len(MESSAGE)) + SUFFIX
    ).hexdigest()
    assert len(SECRET) == 16 and forged_len > 0
    write_out(
        "sha256_ext",
        {
            "server.txt": (
                "内网 API 网关的会话校验机制如下：\n\n"
                "  mac = SHA-256( secret || message )\n\n"
                "secret 是 16 字节的未知密钥，只参与拼接、不做任何变换。\n"
                "一次抓包拿到的有效请求：\n\n"
                f"  message = {MESSAGE.decode()}\n"
                f"  mac     = {mac}\n\n"
                f"目标：把 message 追加字节 {SUFFIX.decode()!r} 得到新 message，"
                "在不掌握 secret 的前提下给出新 message 对应的合法 mac。\n"
                "提交 64 位十六进制 mac（可带 flag{} 包装）。\n"
            ),
        },
        answer=answer,
    )


if __name__ == "__main__":
    main()
