"""Password check with a trivially reversible transform."""
import base64

_ENC = "ZmxhZ3tyZXZlcnNpbmdfaXNfZnVufQ=="
_EXPECTED = "s3cr3t"


def check(password: str) -> bool:
    return password == _EXPECTED


def reveal() -> str:
    return base64.b64decode(_ENC).decode()


if __name__ == "__main__":
    import sys

    pw = sys.argv[1] if len(sys.argv) > 1 else ""
    if check(pw):
        print(reveal())
    else:
        print("nope")
