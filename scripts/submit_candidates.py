"""Submit a list of candidate flags to a CTFd-compatible platform, one at a time.

Why this exists: some challenges ship a check that is deliberately lossy — the
vmshare encryptor masks each seed with 0xffffff, so 256 spellings of the flag
produce byte-identical ciphertext and the artifact cannot say which one the
platform expects. The only remaining source of truth is the platform's answer.

Usage:
    python scripts/submit_candidates.py \
        --base https://ctf.example.com --token <token> \
        --challenge 123 --file challenges/vmshare/candidates.txt

Every attempt is appended to a log, so an interrupted run can be resumed with
``--skip N``. Stops at the first ``correct``. Sleeps between attempts: 256 rapid
POSTs is a good way to get an account rate-limited or flagged.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request


def submit(base: str, token: str, challenge_id: int, flag: str, timeout: float) -> tuple[str, str]:
    body = json.dumps({"challenge_id": challenge_id, "submission": flag}).encode()
    request = urllib.request.Request(
        base.rstrip("/") + "/api/v1/challenges/attempt",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Token {token}",
            "User-Agent": "ctfagent/0.1",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8", "replace"))
    data = (payload or {}).get("data") or {}
    return str(data.get("status") or "error"), str(data.get("message") or "")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True, help="平台根地址，例如 https://ctf.example.com")
    parser.add_argument("--token", required=True, help="CTFd 的 Access Token")
    parser.add_argument("--challenge", required=True, type=int, help="题目在平台上的数字 id")
    parser.add_argument("--file", required=True, help="候选 flag 列表，一行一个")
    parser.add_argument("--delay", type=float, default=1.5, help="两次提交之间等几秒")
    parser.add_argument("--skip", type=int, default=0, help="跳过前 N 个（断点续跑）")
    parser.add_argument("--log", default="", help="结果日志，默认 <file>.log")
    args = parser.parse_args()

    candidates = [line.strip() for line in pathlib.Path(args.file).read_text(encoding="utf-8").splitlines()]
    candidates = [c for c in candidates if c]
    log_path = pathlib.Path(args.log) if args.log else pathlib.Path(args.file + ".log")

    for index, flag in enumerate(candidates):
        if index < args.skip:
            continue
        try:
            status, message = submit(args.base, args.token, args.challenge, flag, timeout=20)
        except urllib.error.HTTPError as exc:
            status, message = f"http-{exc.code}", exc.read()[:200].decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            status, message = "error", f"{type(exc).__name__}: {exc}"

        line = f"{index}\t{flag}\t{status}\t{message[:120]}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

        # CTFd: correct / already_solved<br>pause account_banned ratelimited
        if status in ("correct", "already_solved"):
            print(f"\n命中：{flag}", flush=True)
            return 0
        if status in ("ratelimited", "account_banned", "forbidden"):
            print(f"\n被限流/封禁（{status}），停下。已试 {index + 1} 个，可用 --skip {index + 1} 续跑。", flush=True)
            return 2
        time.sleep(args.delay)

    print("\n256 个候选全部试完，没有命中 —— 说明平台的 flag 不是这个格式，或者种子恢复有误。", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
