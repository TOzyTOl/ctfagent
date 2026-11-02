---
name: ctf-ai-ml
description: Provides AI and machine learning techniques for CTF challenges. Use when attacking ML models, crafting adversarial examples, performing model extraction, prompt injection, membership inference, training data poisoning, fine-tuning manipulation, neural network analysis, LoRA adapter exploitation, LLM jailbreaking, or solving AI-related puzzles.
license: MIT
compatibility: Requires filesystem-based agent (Claude Code or similar) with bash, Python 3, and internet access for tool installation.
allowed-tools: Bash Read Write Edit Glob Grep Task WebFetch WebSearch
metadata:
  user-invocable: "false"
---

# AI / ML 题

**入口只做判别，不要通读。** 选题看 [INDEX.md](INDEX.md)。

## 判别树

| 拿到什么 | 先试 | 读 |
|---|---|---|
| 模型权重 + 推理脚本，要绕分类器 | 先复现干净样本的预测，再上 FGSM / PGD | adversarial-ml.md |
| 只有推理 API，要偷模型 | 查询预算盘点 → 边界样本 → 蒸馏 | model-attacks.md |
| 给了 LoRA / 微调产物 | 权重合并与增量差值分析 | model-attacks.md |
| LLM 接口，要套系统提示或越权 | 提示注入、上下文分隔符、编码绕过 | llm-attacks.md |
| 要判断某样本是否在训练集 | 成员推断：比较置信度与损失 | model-attacks.md |
| 题面只是用 ML 包装的谜题 | 转 `/ctf-misc` | — |

## 纪律

1. **先把干净样本跑通**，拿到基线置信度再扰动；没有基线，扰动成功也说不清原因。
2. 对抗样本优先改**梯度可达的连续输入**；离散输入（文本、token）改用替换式搜索。
3. 记录每次查询的输入输出，模型窃取类题目本质是在做数据集构造。

## 转出

- 变成纯数学 / 格 / 数论 → `/ctf-crypto`
- 要逆的是编译好的推理二进制 → `/ctf-reverse`
- 是 jail 或编码题 → `/ctf-misc`

## 细节

[INDEX.md](INDEX.md)：3 篇、63K，可以整篇读。
