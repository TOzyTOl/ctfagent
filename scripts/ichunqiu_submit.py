"""Submit candidate flags to the ichunqiu (爱春秋) match platform.

The platform's SPA signs every request client-side, in its axios interceptor:

    e.data.stamp = Date.now();
    e.data.rs    = md5((Date.now() + Math.random()) + "icq");   # nonce
    SIGN         = sha1("&".join(sorted(f"{k}={v}" for k, v in data)) + "&" + SALT)

with SALT = 7637b08bdb0b29e08300a976b24ca672 and baseURL
https://apiterminator.ichunqiu.com. Reproduced here so a list of equivalent
answers can be tried without a browser.

Usage (single check first, then the list):
    python scripts/ichunqiu_submit.py --token <token> --k <k> --question-id <id> \
        --img-url <img> --answer test
    python scripts/ichunqiu_submit.py ... --file challenges/vmshare/candidates.txt
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import random
import sys
import time
import urllib.error
import urllib.request

BASE = "https://apiterminator.ichunqiu.com"
SUBMIT = "/match/red/submit"
SALT = "7637b08bdb0b29e08300a976b24ca672"


def signed_payload(answer: str, *, token: str, k: str, question_id: str, img_url: str) -> tuple[dict, str]:
    stamp = int(time.time() * 1000)
    nonce = hashlib.md5(f"{stamp}{random.random()}icq".encode()).hexdigest()
    data = {
        "question_id": question_id,
        "answer": answer,
        "img_url": img_url,
        "k": k,
        "rs": nonce,
        "stamp": stamp,
        "token": token,
    }
    pairs = sorted(f"{key}={value}" for key, value in data.items())
    sign = hashlib.sha1(("&".join(pairs) + "&" + SALT).encode()).hexdigest()
    return data, sign


def submit(answer: str, *, token: str, k: str, question_id: str, img_url: str,
           timeout: float = 25.0) -> dict:
    data, sign = signed_payload(answer, token=token, k=k, question_id=question_id, img_url=img_url)
    request = urllib.request.Request(
        BASE + SUBMIT,
        data=json.dumps(data).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "SIGN": sign,
            "X-Lang": "zh-CN",
            "Origin": "https://match.ichunqiu.com",
            "Referer": "https://match.ichunqiu.com/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", "replace")
    try:
        return json.loads(body)
    except ValueError:
        return {"raw": body[:500]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", required=True)
    parser.add_argument("--k", required=True, help="URL 里的 k 参数")
    parser.add_argument("--question-id", required=True, help="题目 id，例如 brz2pZwJzt8gV8r6")
    parser.add_argument("--img-url", required=True, help="payload 里的 img_url")
    parser.add_argument("--answer", default="", help="只提交这一个答案（先验证签名用）")
    parser.add_argument("--file", default="", help="候选列表，一行一个")
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--skip", type=int, default=0, help="跳过前 N 个（断点续跑）")
    parser.add_argument("--log", default="")
    args = parser.parse_args()

    if args.answer:
        result = submit(args.answer, token=args.token, k=args.k,
                        question_id=args.question_id, img_url=args.img_url)
        print(json.dumps(result, ensure_ascii=False)[:600])
        return 0

    if not args.file:
        print("需要 --answer 或 --file", file=sys.stderr)
        return 2

    candidates = [line.strip() for line in
                  pathlib.Path(args.file).read_text(encoding="utf-8").splitlines() if line.strip()]
    log_path = pathlib.Path(args.log) if args.log else pathlib.Path(args.file + ".submit.log")

    for index, flag in enumerate(candidates):
        if index < args.skip:
            continue
        try:
            result = submit(flag, token=args.token, k=args.k,
                            question_id=args.question_id, img_url=args.img_url)
        except urllib.error.HTTPError as exc:
            result = {"http": exc.code, "body": exc.read()[:200].decode("utf-8", "replace")}
        except Exception as exc:  # noqa: BLE001
            result = {"error": f"{type(exc).__name__}: {exc}"}

        line = f"{index}\t{flag}\t{json.dumps(result, ensure_ascii=False)[:200]}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

        text = json.dumps(result, ensure_ascii=False)
        if "签名" in text or "sign" in text.lower():
            print("\n签名被拒 —— 先停下来，配方需要修正。", flush=True)
            return 3
        if '"code": 0' in text or '"code":0' in text:
            print(f"\n返回 code=0，检查是不是命中：{flag}", flush=True)
            return 0
        time.sleep(args.delay)
    return 1


if __name__ == "__main__":
    sys.exit(main())
