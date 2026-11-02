<!-- usage {"helped": 4, "used": 4} -->
<!-- 沉淀自 run 30（http://hbc2.haobachang.com:21848/ sql绕过，2026-10-27）。由解出该题的运行蒸馏；flag 字面量已擦除。 -->

# 纯数字白名单型 WAF 下的 UNION 注入

## 什么时候用

目标是一个查询表单（单参数，如 `id`），正常数字输入返回结构化数据（HTML 表格行、JSON 字段）。当你用经典注入特征去试探时，响应**不是** SQL 报错、也不是 500，而是一条固定的自定义拦截文案（本战例：「请求已被 WAF 拦截」），并且这个文案是应用层返回的。

- 命中拦截文案的输入集合呈**白名单形状**，不是黑名单形状。逐个探测即可看出来：`1a`、`1.0`、`-1`、`+1`、`0x31`、`1'`、`1 and 1=1`、`1/**/` 全部被拦，而裸 `1`、`0`、`999` 正常返回。
- 换 Content-Type 不走：`application/x-www-form-urlencoded` 与 `multipart/form-data`（`curl -F`）实测同样被拦，说明 WAF 作用在参数解码后的值上。
- 后端口味从 `version()` 回显读（MySQL / MariaDB 系）。

只要观察到「非纯数字一律同一个拦截文案」，就直接跳到「零数字字面量 payload」这条路，不要在注释符 / 大小写 / 编码上耗时间。

## 怎么做

核心思路：**payload 里一个数字字面量都不写**。这个场景下数字全是负担（列序号、`-1` 空结果、`0x..`），而 SQL 允许用 `NULL` 占位、允许标识符直接进 SELECT 列表。

1. **基线**：`id=1`（纯数字）确认参数名与回显结构，数清正常行有几个 `<td>`。

2. **WAF 指纹**：跑一张字符探测表（`work/solve.sh` 第 2 段），用被拦/放行两端的样本确认是白名单。

3. **列数探测**：`NULL` 占位，不写列序号。`id=null` 让原查询无结果，UNION 行成为唯一输出。
   ```bash
   curl -s -X POST -d "id=null union select null,null,null,null" "$T"
   ```
   看回显里有几个空值格子（本战例渲染成 `None`），那就是列数。列数不对时 MySQL/MariaDB 通常会报列数不匹配，但这类站常把异常吞掉、只返回空白页，所以**以回显格子数为准，不要等报错**。

4. **回显位探测**：把全字母函数放进候选列，看哪一列被页面渲染。
   ```bash
   curl -s -X POST -d "id=null union select null,null,database(),null" "$T"
   ```
   `database()`、`version()`、`user()` 都是字母+括号，天然过白名单。

5. **取数据**：表名/列名本身就是字母标识符，直接写进 SELECT 列表。
   ```bash
   curl -s -X POST -d "id=null union select null,null,null,<COL> from <TABLE>" "$T"
   ```

6. **表名未知时**：走 `information_schema.tables` 拿 `table_name` / `table_schema`，这些标识符不含数字。本战例用这种方式枚举出业务表；具体 payload 未留存，复现时需自行确认 `where table_schema=database()` 这类全字母条件是否被放行。

## 坑

- **不要试 `/**/`、大小写混淆、`%09`/`%0a` 空白、`-1`、`0x..`、科学计数法**：本战例实测 `1/**/`、`-1`、`0x31`、`1a`、`1.0` 全部返回拦截文案，一次探测就能否定这一整类变形。
- **不要以为 multipart 能绕**：`curl -F "id=1 and 1=1"` 实测同样返回拦截文案。multipart 只在 WAF 只解析 urlencoded 时有效，验证方法是直接发一个 `-F` 请求看是否同样被拦。
- **不要用 `id=-1` 制造空结果**：负号被拦。用 `id=null`（或 `id=0`）代替，零成本。
- **空格要单独实测，别假设**：本战例中裸 `1 `（尾随空格）被拦，但最终成功的 payload（`null union select ... from ...`）**含空格且通过了**——同一份记录里这两个观察看起来矛盾，运行时未能解释清楚规则究竟怎么判。因此出发前先用 `1 ` 与 `null union select null` 两条请求分别探一次空格，别照抄。
- **不要依赖报错回显**：SQL 异常可能被吞掉；判断列数用 `<td>` / 空值格子的数量。

## 边界

- 只在 WAF 确认为「纯数字白名单」时成立。若目标额外过滤 SQL 关键字（`union`/`select`/`from`），需要关键字层面的绕过（注释拼接、大小写等），而那条路在本战例里已被证明走不通，属于另一种场景。
- 依赖 UNION 注入可行且有回显。盲注（布尔/时间）payload 几乎必然包含数字比较（如 `substr(x,1,1)`），与纯数字白名单直接冲突，本方法不适用。
- 本战例后端 MariaDB 11.8.6；`database()`/`version()`/`user()` 在 MySQL 5.x+ 行为基本一致。
- 需要目标在线：本次运行在赛后复验时该端口已关闭（`Connection refused`），结论全部来自首次运行留存的 command + output，未做二次复现。凡未记录原始请求/响应的步骤，复现时都要重新实测。
