# REA 拆解，以及能搬到 CTFAGENT 的东西

读的是 `rea-agents@6.6.0`（MIT）。仓库 2085 个 TS 文件、约 50 万行、1023 个测试文件、73 篇文档、58 个对外 MCP 工具。
我没有通读，读的是决定它"厉害"的那几层：`src/domain/evidence.ts`、`residualUnknown.ts`、`reconstructionCoverage.ts`、
`analysisProfile.ts`、`src/application/investigation/*`、`src/application/binary/AnalysisProviderRegistry.ts`、
`src/server/toolResult*.ts`、`src/contracts/toolContractTypes.ts` 与 `promptContracts.ts`、
`.agents/skills/reverse-engineer-anything/SKILL.md`、`docs/tool-design.md`、`docs/reconstruction-readiness.md`。

## 七个问题

### 1. 它解决的是什么问题？

不是"给 agent 一个反汇编器"，是"让 agent 的逆向结论可以被审计"。

普通做法是 `objdump -d > out.txt` 然后让模型读。问题是：模型读完之后说不出哪条结论来自哪次观测、
哪些地方它其实没看到、哪些是它猜的。REA 把这三件事变成数据结构，而不是提示词里的道德要求。

一句话概括它的产品定位：**证据优先的逆向工具面**。工具返回值里，"我观察到了什么"和"我因此推断什么"
是分开的两个字段，而且推断必须挂在不完整的覆盖声明上。

### 2. 它凭什么"什么都能逆"？

因为引擎是可插拔的，工具面是 provider 中立的。

- 引擎层：Ghidra、IDA、Hopper、rizin/cutter、GDB、Frida、objdump、apktool、JEB、ADB、
  Electron、浏览器、PCAP、SQLite、WASM、EVM、.NET、固件。
- 对外只暴露统一语义的工具（`analyze_function`、`xrefs`、`compare_functions`…），
  provider 差异被压进 `analysis_profile`：`{provider:{id,name,version}, parameters, digest}`。
  同一份 Evidence 能被另一个 provider 的结果直接比较，前提是 profile 语义可比。

关键不在"接了多少引擎"，在于**换引擎不换契约**。这是它敢说自己"anything"的原因。

### 3. 分层怎么防失控？

依赖单向：domain 语义/契约 → provider 适配器 → 共享应用工作流 → CLI/MCP 适配器。
`AGENTS.md` 明确写了两条硬规则：

- **注册顺序永远不影响选择或调用方可见输出**（`AnalysisProviderRegistry`）。
- **歧义是错误，不是默认值**。`providerSelectionError` 有 5 种失败原因，并逐候选给出拒绝理由；
  没有"静默回退到某个引擎"。

这条对 agent 系统特别重要：静默回退会让模型以为自己在 A 引擎上看到的东西来自 B 引擎，
后面的所有推理都建立在错误前提上。宁可报错。

### 4. 证据模型为什么是核心？

`Evidence` 记录里这些字段是设计上的重点：

| 字段 | 作用 |
|---|---|
| `evidence_id` | `ev_` + 规范化 JSON 的 sha256；内容变了 id 就变，可校验 |
| `normalized_result` / `raw_result` | 归一化结论 vs 引擎原始输出，两者分开 |
| `confidence` | `observed` / `derived` / `inferred` —— 观测、推导、推测 |
| `authority` | `shipped-artifact` / `controlled-replay` / `historical-reference` / `external-service` / `analyst-inference` |
| `limitations[]` | 这次观测的边界，必须显式 |
| `relationship_coverage` | `complete` / `partial` / `unknown`，并带 `facet_availability`（每个面是否真的观测到） |
| `analysis_profile` | 哪台引擎、什么参数 —— 参与语义身份 |

最狠的一点是 `confidence` 和 `authority` 的分离：**"我算得很确定"和"我是从真实产物里看到的"是两件事**。
静态分析再确定，也不能声称观察到了运行时行为；skill 里专门有一句 "never imply that static analysis observed execution"。

### 5. 未知登记为什么比结论更重要？

`residualUnknown` 是独立的一等对象：

- id 稳定：`unk_` + sha256(`{domain, question, scope_digest}`)，同一问题反复记录不会分裂。
- 有 `supporting_evidence` / `contradicting_evidence`，可以互相 `depends-on` / `related-to` / `contradicts`。
- 有修订链（revision），状态变化留痕。
- 解决必须给 `disposition`：`verified`（带证据）、`withdrawn`、`out-of-scope`。
- 有 `required_authority`：**要解决它，需要什么级别的证据**。

这解决的是 agent 最常见的失败模式：没找到就沉默，然后编一个看起来合理的答案。
把"我不知道什么"显式登记之后，它变成可派发的工作项，而不是幻觉的温床。

### 6. 上下文预算怎么管？

- `resultBudgetBytes = maximumResponseBytes - 1024`：留 1 KiB 给信封。
- 超预算不是截断就完事：返回 `resource_constraint`，并给出恢复路径
  —— `inspect_analysis_view` 分页、`nativeEvidenceContinuation()` 续读、`export_evidence_bundle` 落盘。
- 完整性从"我看了多少"推导，而不是"我截到哪"。
- skill 里强制 **summary-first**：先给结论，再按需翻页。

翻译成一句话：**大结果不是不能返回，是不能一次性灌进模型上下文**。

### 7. agent 技能和工具形状怎么配合？

工具按"分析问题"分类，不按引擎 API 分类（`docs/tool-design.md`）：

| 形状 | 用途 |
|---|---|
| `inspect` | 单个对象的事实 |
| `search` / `list` | 候选发现，稳定排序 + 分页 |
| `trace` | 跨证据的关系 |
| `compare` | 显式配对，只报可比维度 |
| `workflow` | 反复出现的组合结果 |
| `observe` / `capture` | 运行时行为，带启动/附着副作用与清理状态 |

skill 则是**分步工作流**（`investigate_feature`、`verify_reconstruction`、`trace_crash`、
`audit_residual_unknowns`），每一步绑定一组工具和一条指令。模型不需要记 58 个工具，
只需要按步骤走。

### 8. 工程上为什么"厉害"？

- `returnsTargetContent` 标记：来自目标的文本一律按不可信数据投递。逆向对象本身就是攻击面。
- 9 阶段 `verify:readiness`：发现分类、环境诊断、授权获取、静态分析、反应式场景、权威对比、
  重建验证、清理、报告。**每一项都有可执行的证明**，不是文档承诺。
- 失败关闭（fail-closed）：`verify_reconstruction` 的 pass 只对"声明的那个 claim 和那个维度"成立，
  不允许外推成全局等价。

---

## 能搬的 8 条（按对 CTF 平台的价值排序）

| # | 设计 | 为什么对 CTF 有用 | 落地情况 |
|---|---|---|---|
| 1 | **未知登记表** | 逆向题最常见的失败是"卡住了不说"，主 agent 以为还在推进。显式登记后可以派活 | ✅ `record_unknown` + `unknown` 事实类型 + `open_unknowns()` |
| 2 | **结论可信级别**（observed/derived/inferred/external） | flag 的推导链里混进一个"推测"，验证者就该重点打它 | ✅ `facts.basis`，工具、黑板、UI、CLI 全通 |
| 3 | **未解问题驱动派发** | 主 agent 收尾前必须先清空未解问题，而不是凭任务完成度收工 | ✅ 主 agent prompt + 黑板摘要单列"未解问题"节 |
| 4 | **静态 ≠ 运行时** | 逆向题里"反汇编看到常量"和"跑起来看到输出"经常被混为一谈 | ✅ worker/verifier prompt + `ctf-reverse` 证据纪律表 |
| 5 | **有界输出 + 摘要优先** | `strings`/`objdump` 输出动辄几十 MB，直接灌进去等于烧钱 | ✅ `re_tools` 60k 上限 + 分页 + `coverage.status` |
| 6 | **确定性路由，歧义即报错** | 我们按 role 派 skill，目前是查表，没有歧义概念 | 弱相关，未做 |
| 7 | **目标内容不可信** | 题目二进制/附件里可以埋"忽略之前指令" | ✅ worker prompt 新增 "Untrusted input" 段 |
| 8 | **失败关闭的验证声明** | 验证者的 `VERDICT: reproduced` 目前不限定范围 | ✅ verifier prompt 第 5 条限定范围 |

前 5 条和 CTF 解题直接相关，第 6–8 条是工程卫生。**没有 Ghidra 不是障碍**：
REA 的价值 80% 在证据纪律，不在反编译引擎。我们手上的 objdump / strings / Python / Node
足够支撑静态分析，关键是让结论带上"这是实测还是读代码看出来的"。

---

## 落到 CTFAGENT 的具体改动

证据记录层（`ctfagent/re_probe.py` + `ctfagent/re_tools.py`）已经把 REA 的工具面搬过来了：
`re_capabilities` / `re_inventory` / `re_inspect` / `re_strings` / `re_search` /
`re_disassemble` / `re_symbols` / `re_read` / `re_xrefs` / `re_evidence_get`，
每次返回 `evidence_id` + `normalized_result` + `coverage.status` + `limitations` + `residual_unknowns`，
带分页和 60k 字符上限；只读工作区，不执行目标。这一步没有 Ghidra 也能跑，因为内置解析器
直接读 ELF/PE/Mach-O 头，外部工具（readelf/objdump/nm/strings/file/gdb）只作为增强，缺失会明说。

在这之上又补了三样，让"结论"本身带上出处：

1. **`facts.basis`**（`observed` / `derived` / `inferred` / `external`）。`record_fact` 工具必填，
   黑板摘要和 UI 显示；空串表示历史数据。静态反汇编不能被标成 `observed`。
   标 `observed` / `derived` 时还必须带 `evidence`：`command`（或 `request`/`url`，即实际跑了什么）
   与 `output`（或 `response`，即原始输出）。缺了任意一半，`grounding_gap()` 会把这条记录降级为
   `inferred`，原标注留在 `evidence.declared_basis`，并广播 `fact.downgraded` 事件；黑板摘要里
   这类条目直接标"原标「实测」已降为推测"。带 `evidence_id`（RE 工具的结构化记录）或
   `system: true`（编排器自己写的额度交接记录）的记录豁免。
2. **`record_unknown` 工具 + `unknown` 事实类型**。结构化字段 `question`、`why_it_matters`、
   `would_resolve`。解决时用 `record_fact(resolves=<id>)` —— 黑板是 append-only 的，
   所以"已解决"用指针表达，历史不被覆盖。`open_unknowns()` 算差集，黑板摘要单列一节，
   主 agent 收尾前必须解决或明确放弃。
3. **prompt 与 skill 的证据纪律**：worker 必须给 `basis`；验证者的 `VERDICT: reproduced`
   只对"我实际跑出来这个值"成立，不能外推成"推导正确"；目标内容（二进制字符串、附件）
   一律按不可信数据对待；`ctf-reverse` 的开头新增证据纪律表和本机真实可用的工具清单，
   删掉 Ghidra/radare2 的默认假设。
   黑板摘要现在把证据本身带下去（`证据·命令:` / `证据·输出:`，各截 220 字符，全局预算 2400 字符，
   从最新一条往前分配），未解问题也带 `判据:`（即 `would_resolve`），下一个人不需要重跑才知道
   当时在等什么。远程题另加一段 `REMOTE_SERVICE_BLOCK`：先在本地复制运行时验证，再对远端发
   一个能区分假设的请求，而不是整族变体。

