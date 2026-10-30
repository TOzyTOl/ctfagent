<!-- usage {"helped": 7, "used": 7} -->
<!-- 沉淀自 run 77（古典密码，2026-10-28）。由解出该题的运行蒸馏；flag 字面量已擦除。 -->

# 前端 JS 泄露接口 + 同会话提交的古典密码（Atbash / Keyed Atbash）自动化解题

## 什么时候用

出现下面这些可观察特征时，优先套用这套流程：

- 目标是一个带页面的 Web 服务（本题指纹：Flask + gunicorn，首页返回 HTML，`Server` 头为 `gunicorn`）。题面只给一句提示（例如"某某古典密码"），页面上没有可交互的输入框或明文密文。
- 首页 HTML 引用了 `/static/*.js`，脚本里能直接读到 `fetch('/api/...')` 一类的接口路径和请求体字段名。**先看静态资源，再谈爆破。**
- 接口呈现"服务端发题 / 客户端作答"的两段式：一个 GET 下发密文（JSON 里是 `part1`/`part2` 之类的纯小写字母串），一个 POST 交答案（JSON 里是 `ans1`/`ans2`）。
- 同一个响应里带了 `Set-Cookie: session=...`，且值是 itsdangerous 风格（`eyJ...` 开头、base64 分段、第三段是签名）。这说明题目状态存在**客户端 session 里**，答案与 cookie 绑定。

密文本身的特征：字母表内的一一替换、长度不变、大小写保留、非字母原样保留、字频分布和英文一致但字母整体"错位"——这是单表替换，先试 Atbash，再试 keyed 变体。

## 怎么做

**第 1 步：抓首页和响应头，确认形态。**

```bash
curl -si "http://TARGET/" | head -40
curl -s "http://TARGET/static/script.js"
```

重点看：`Set-Cookie` 是否下发 session、JS 里出现的所有 `/api/` 路径、POST 的 `Content-Type`（`application/json` 还是表单）以及字段名。

**第 2 步：本地实现解密，并用公开已知量校准。** 校准是这套流程里最值钱的一步——它把"猜变换方向/猜表的构造方式"变成"跑一条断言"。

标准 Atbash（字母镜像）：

```python
ALPHA = "abcdefghijklmnopqrstuvwxyz"

def standard_dec(c):
    out = []
    for ch in c:
        if 'a' <= ch <= 'z':
            out.append(ALPHA[25 - ALPHA.index(ch)])
        elif 'A' <= ch <= 'Z':
            out.append(ALPHA[25 - ALPHA.index(ch.lower())].upper())
        else:
            out.append(ch)
    return "".join(out)

assert standard_dec("ABC") == "ZYX"
assert standard_dec("HELLO") == "SVOOL"   # 教科书级已知量
```

Keyed Atbash：先按关键词去重、把剩余字母按字母序补齐得到置换表 `M`，再**整体反转**得到密文→明文的映射底，密文字母在反转表中的下标即明文字母。

```python
def build_table(key):
    seen = []
    for ch in key.lower():
        if ch.isalpha() and ch not in seen:
            seen.append(ch)
    seen += [ch for ch in ALPHA if ch not in seen]
    return "".join(seen)

def keyed_dec(c, key):
    rev = build_table(key)[::-1]
    return "".join(ALPHA[rev.index(ch)] if ch in ALPHA else ch for ch in c.lower())
```

关键词从题面/JS/接口里取（往往就是那句提示的一部分）。**校准要用外部已知量验证方向和表的构造**：例如任选一个 keyed 样本，用 `keyed_dec` 的结果必须是一个可读英文单词；如果解出乱码，先把"反转"这一步去掉再试一次——keyed Atbash 最常见的错就是"表建对了但方向反了"。

**第 3 步：必须用同一个会话取题和提交。** 服务端把 p1/p2 写进了签名 cookie，换 session 提交必错。

```python
import requests

BASE = "http://TARGET"
def solve(key):
    s = requests.Session()
    d = s.get(BASE + "/api/get_chal", timeout=15).json()
    a1, a2 = standard_dec(d["part1"]), keyed_dec(d["part2"], key)
    r = s.post(BASE + "/api/chk_ans", json={"ans1": a1, "ans2": a2}, timeout=15)
    return r.text
```

`requests.Session()` 自动维护 cookie，不要用两次独立的 `requests.get/post`（那会拿到不同的 session，答案对不上）。中间不做任何"先打印再手工拼"的往返——`part1`/`part2` 每次请求都会变。

**第 4 步（可选）：验证 cookie 内容自洽。** 若想确认自己的解密方向，可在本地解出 session cookie 的载荷比对（itsdangerous 签名密钥未知时不能伪造，但能读明文段——本题实测载荷就是 `{"p1":..., "p2":...}` 的 base64）。这只用于交叉验证，不是解题必需。

## 坑

- **不要用两次独立请求取题和提交。** 答案存在 session cookie 里，第二次请求是新 session，服务端比对失败。验证方式：两次 `get_chal` 返回的 `part1` 不同、`Set-Cookie` 不同；而用同一个 `Session` 对象时提交一次就返回 flag。
- **不要跳过校准直接套 Atbash。** 标准 Atbash 与 keyed Atbash 的输出都是"纯字母乱码"，肉眼分不清对错。先用 `ABC→ZYX`、`HELLO→SVOOL` 这类不依赖本题的已知量确认实现没写错（本题实测这四条断言全过），再用一个 keyed 样本确认表构造与方向。
- **不要只做 GET 就手工把密文贴进 curl。** 密文与 cookie 绑定且每次刷新即变，手工搬运必然错过窗口，且极易把大小写/换行带错。
- **不要在拿到 JSON 后假设它一定是题目密文。** 本题 `status:1` 是"已发题"标志，密文在 `part1`/`part2`；`status` 不是答案正确性，别拿它做判断。
- **不要只扫端口不看静态资源。** 接口路径直接写在前端 JS 里，比猜 `/api/check`、`/submit` 省得多。

## 边界

- 本方法依赖服务端**明文把答案写进客户端 session**（或至少校验时依赖同一 cookie）。若目标改成服务端存储状态、或加了时间戳/一次性 nonce（同 session 重复提交失效），第 3 步的单次「取题→提交」仍然成立，但"多次重试"会失败，需要在一次请求链内完成。
- 若 session 使用服务端签名且校验还包括 `Referer`/CSRF token/自定义 header，需要在 `Session` 上补齐这些头；本次未遇到，未验证。
- Keyed Atbash 的关键词若不在题面/JS/接口里，需要字典或字频爆破，那属于另一类工作，本篇未覆盖。
- Atbash 系只处理单表替换；若密文里出现数字/Base64 外层/多轮替换，本流程不适用，需先识别外层编码。
- 本次环境为 Windows + Git Bash + `python -I` + `requests`；`curl.exe` 用于抓页面。若目标仅允许浏览器访问（如需要执行 JS 生成 token），需改用 Playwright 一类工具，未验证。
