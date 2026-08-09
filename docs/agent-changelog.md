# Agent Learning Changelog

这份 changelog 按时间记录 Code Review Agent 的学习、设计变更和实验结论。每次只追加，不覆盖旧记录，避免后来只看到最终代码却忘记为什么这样设计。

每轮记录包含目标、变更、结果、结论和本轮有意改变的变量。

## 2026-08-06：建立可复现评估闭环

### 目标与变更

- 固定 5 个 benchmark fixtures，运行时 checkout 锁定的 PR head SHA。
- 使用 Kimi 生成 review，下载 dev golden comments。
- 使用 OpenAI Judge 匹配 candidate 与 golden，计算 TP、FP、FN、precision、recall 和 F1。

### 初始 Sentry 结果

```text
Candidates = 11
TP = 2, FP = 9, FN = 3
Precision = 18.2%, Recall = 40.0%, F1 = 25.0%
```

### 结论

形成“运行 → 评分 → 分析 → 修改”的闭环，但误报较多。

## 2026-08-06：加入工具循环与 System Prompt SOP

### 目标与变更

- 实践 `Agent = LLM + Context + Tools`。
- 静态评审指令放入 system message，不可信 diff 放入 user message。
- 提供受路径和大小限制的 `read_file(path)`。
- 工具结果加入消息历史，形成小型 ReAct 循环。
- 使用流程式 SOP 代替零散规则。

### 结果与结论

Prompt 一度因验证要求过严而输出空 issues；“必须调用 read_file”也被模型忽略。Prompt 不是程序约束，过高的报告门槛会损害 recall，必须先增加 trajectory 才能诊断行为。

## 2026-08-06：Trajectory 与完成协议

### 目标与变更

- `result.json` 保存 model response、tool call 和 tool result。
- 失败运行保存 `error.json` 和失败前 trajectory。
- 先尝试 `needs_context / complete` 双状态，后来删除重复的 `needs_context`。

最终协议：

```text
tool_call = 中间状态
status=complete = 最终状态
其他普通响应 = 非法输出并纠正
```

### 结论

工具调用本身已经表达“需要上下文”。关键 workflow 状态应由 Harness 代码维护。

## 2026-08-06：`tool-or-complete-v1` 新基线

### 配置

```text
Provider = Kimi
Thinking = disabled
Workflow = tool call or complete
Max tool calls = 3
```

### 自动评分

| Case | TP | FP | FN | Precision | Recall | F1 |
|---|---:|---:|---:|---:|---:|---:|
| sentry-93824 | 2 | 2 | 3 | 50.0% | 40.0% | 44.4% |
| grafana-79265 | 4 | 1 | 1 | 80.0% | 80.0% | 80.0% |
| calcom-10600 | 1 | 5 | 3 | 16.7% | 25.0% | 20.0% |

```text
Micro Precision = 46.7%
Micro Recall = 50.0%
Micro F1 = 48.3%
```

### 人工抽查

Judge 标记的 8 条 FP 中：

```text
A 明确误报 = 2
B 合理但 golden 未覆盖 = 3
C 低价值推测 = 3
```

### 结论

- 不同仓库表现差异明显。
- 大部分 TP 和 FN 只需 diff，主要瓶颈不是完整文件上下文。
- 在声称“保护或导出不存在”时应先用工具验证。
- `tool-or-complete-v1` 作为后续开发基线。

## 2026-08-07：`verified-claims-v1` Prompt 实验（已撤销）

### 唯一变量

System prompt 增加 reporting quality gate，尝试减少事实错误、未来假设和未经验证的主张。

### 结果

| Case | Baseline F1 | verified-claims-v1 F1 |
|---|---:|---:|
| Sentry | 44.4% | 0% |
| Grafana | 80.0% | 40.0% |
| Cal.com | 20.0% | 22.2% |

```text
Micro F1: 48.3% → 22.2%
```

模型仍未调用工具，仍报告自己承认“没有直接问题”的 candidate，并从具体 bug 转向维护推测。

### 结论

实验失败并撤销。把“报告前验证”写进 Prompt 不能保证模型真正执行，且增加认知负担。

## 2026-08-07：`thinking-v1` 配置实验（不采用）

### 唯一变量

```env
KIMI_THINKING=enabled
```

### 结果

| Case | Baseline F1 | thinking-v1 F1 |
|---|---:|---:|
| Sentry | 44.4% | 0% |
| Grafana | 80.0% | 72.7% |
| Cal.com | 20.0% | 40.0% |

```text
Candidates: 15 → 21
Micro F1: 48.3% → 34.3%
```

Thinking 生成更多、更长的候选，但推测增加；三个 case 仍未调用 `read_file`。Cal.com 第一轮损坏 JSON，由 workflow feedback 恢复；新增并发 TP 与 golden 的具体路径不同，Judge 可能匹配过宽。

### 结论

恢复 `KIMI_THINKING=disabled`。更多推理 token 不等于更高的 Agent 质量。

## 2026-08-07：三阶段 Workflow 实现

### 目标

不再让单次调用同时负责发现和验证，实践代码维护的多阶段 Agent workflow。

### 变更

```text
DISCOVER candidates
→ VERIFY keep / revise / drop，可调用 read_file
→ FINALIZE summary
→ COMPLETE，由代码组装 verified issues
```

代码维护 `ReviewState`：stage、candidates、verified issues、trajectory、model turns 和 tool calls。

关键约束：

- DISCOVER 不能调用工具。
- VERIFY 必须为每个 candidate 返回唯一决定，且不能新增 candidate。
- Drop candidate 不进入最终结果。
- FINALIZE 不能修改 issues，只生成 summary。
- 最大模型轮数从 5 调整为 8；工具上限保持 3。

### 测试

```text
36 tests passed
```

## 2026-08-07：`staged-workflow-v1` 失败与恢复修复

### 现象

DISCOVER 第一次返回非法 JSON，纠正后生成 8 个 candidates。VERIFY 随后返回：

```text
content = ""
tool_calls = []
```

框架识别为空响应并反馈，却把空 assistant message 放回消息历史，下一轮 Kimi API 拒绝请求：

```text
assistant message must not be empty
```

### 修复

- 空响应保留在 trace，但不写回 messages。
- 直接追加 workflow feedback 后重试。
- 增加空 VERIFY 响应回归测试。

### 结论

验证非法输出还不够；无效响应不能污染下一轮上下文。`staged-workflow-v1` 保留为失败记录。

## 2026-08-07：`staged-workflow-v2` 首次完成三阶段运行

### 配置

```text
Provider = Kimi
Thinking = disabled
Workflow = DISCOVER → VERIFY → FINALIZE
Max model turns = 8
Max tool calls = 3
Completion token limit = 2048
```

### 自动评分

| 指标 | Cal.com baseline | staged-workflow-v2 |
|---|---:|---:|
| Candidates | 6 | 9 |
| TP | 1 | 3 |
| FP | 5 | 6 |
| FN | 3 | 1 |
| Precision | 16.7% | 33.3% |
| Recall | 25.0% | 75.0% |
| F1 | 20.0% | 46.2% |

### Trajectory

```text
Turn 1: DISCOVER → 10 candidates
Turn 2: VERIFY → invalid JSON
Turn 3: VERIFY → invalid JSON
Turn 4: VERIFY → 9 keep/revise, 1 drop
Turn 5: FINALIZE → complete
```

VERIFY 没有调用 `read_file`。它成功识别并删除 1 条错误 candidate，但保留了较多弱问题，最终 FP 从 5 增加到 6。Recall 明显上升，说明 discovery/verification 分阶段有助于找出大小写和并发问题，但当前 verifier 的过滤强度不足。

两次非法 VERIFY 输出都被 workflow feedback 恢复，整个流程在 5/8 轮内完成，说明当前轮数上限足够。

该 run 发生在加入 `finish_reason` 和 token usage 诊断之前，因此它的 trace 没有这些字段。不能根据本次结果判断 Kimi 是否返回了诊断信息，也不能确认两次非法 JSON 是否由 token 截断造成。

### 初步结论

- 三阶段代码状态机成功运行，错误恢复有效。
- Cal.com 自动 F1 从 20.0% 上升到 46.2%。
- 主要收益来自 Recall；Precision 仍受弱 candidate 影响。
- 暂不增加最大轮数。
- 需要人工检查 VERIFY 保留的 6 条 FP，理解 verifier 为什么没有 drop，之后再决定是否改验证数据结构或上下文，而不是直接堆 Prompt。

## 2026-08-07：记录 Finish Reason 与 Token Usage

### 触发原因

`staged-workflow-v2` 虽然成功恢复两次非法 VERIFY JSON，但当时的 trace 无法判断失败来自 token 截断、正常停止还是服务异常。

### 变更

在 `staged-workflow-v2` 完成之后，每个新的 `model_response` trace 开始记录：

```json
{
  "finish_reason": "stop | length | ...",
  "usage": {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0
  }
}
```

### Token 决策

暂时保持 `LLM_MAX_COMPLETION_TOKENS=2048`：

- 后续 run 若显示 `finish_reason=length`，压缩 candidates 或提高到 4096。
- 后续 run 若显示 `finish_reason=stop` 但 content 为空，提高 token 上限通常无效。
- `staged-workflow-v2` 不能用于验证上述判断，因为它运行时尚未包含这些诊断字段。

### 测试

```text
37 tests passed
```

## 2026-08-07：`staged-workflow-v2` 跨仓库结果

### 自动评分

| Case | Baseline F1 | Staged F1 | Baseline P/R | Staged P/R |
|---|---:|---:|---:|---:|
| Sentry | 44.4% | 20.0% | 50.0% / 40.0% | 20.0% / 20.0% |
| Grafana | 80.0% | 54.5% | 80.0% / 80.0% | 50.0% / 60.0% |
| Cal.com | 20.0% | 46.2% | 16.7% / 25.0% | 33.3% / 75.0% |

按当前 scorer 的口径合并：

```text
Baseline:
  Candidates = 15
  TP = 7
  Micro Precision = 46.7%
  Micro Recall = 50.0%
  Micro F1 = 48.3%

Staged workflow:
  Candidates = 20
  TP = 7
  Micro Precision = 35.0%
  Micro Recall = 50.0%
  Micro F1 = 41.2%
```

### 阶段行为

| Case | DISCOVER | VERIFY 最终保留 | Drop | Model turns | Tool calls |
|---|---:|---:|---:|---:|---:|
| Sentry | 9 | 5 | 4 | 4 | 0 |
| Grafana | 6 | 6 | 0 | 3 | 0 |
| Cal.com | 10 | 9 | 1 | 5 | 0 |
| **合计** | **25** | **20** | **5** | — | **0** |

VERIFY 确实执行了筛选，但总体只删除 20% 的候选。Cal.com 的 recall 收益被 Sentry 和 Grafana 的退步抵消，整体 TP 和 recall 与基线完全相同，新增候选主要变成 FP。

### Finish Reason 与 Token Usage

Sentry 和 Grafana 是诊断功能加入后的运行：

- 所有响应的 `finish_reason` 都是 `stop`，没有 `length`。
- Sentry VERIFY 最长一次 completion 为 1,830 tokens，低于 2,048 上限。
- Grafana VERIFY completion 为 1,726 tokens，低于上限。
- Sentry 共使用约 20,278 tokens；Grafana 共使用约 13,524 tokens。
- Cal.com 在诊断功能加入前运行，没有 usage 数据。

因此当前没有证据支持提高 completion token 上限。Sentry 的非法 VERIFY 输出发生在 `finish_reason=stop`，问题是 schema 遵循失败，不是 token 截断。

### 具体质量问题

1. **同模型确认偏差**：同一个模型先生成 candidate，再验证自己的 candidate，倾向于 keep。
2. **Candidate 不够原子**：一条 candidate 混合多个主张，只要其中一个看似成立，VERIFY 就可能保留整条。
3. **验证理由也会犯事实错误**：例如 Sentry verifier 对 `break` 所在循环的理解不准确，仍然选择 keep。
4. **验证阶段没有使用工具**：三个仓库共 25 条候选，没有一次 `read_file`。
5. **成本明显增加**：每个 case 至少三轮模型请求，但整体自动 F1 反而从 48.3% 降至 41.2%。

### 结论

三阶段状态机在工程上成功：阶段转换、结构验证、错误恢复和最终 issue 所有权均按设计工作。但当前“批量候选 + 同模型批量验证”的策略没有带来整体质量提升。

本轮结论为：

```text
保留三阶段 Harness 作为学习成果；
不把 staged-workflow-v2 提升为质量基线；
质量基线仍为 tool-or-complete-v1；
不增加 token 上限；
下一轮聚焦 candidate 原子性和验证上下文，而不是继续增加 Prompt 规则。
```

## 2026-08-07：原子 Candidate 与 Diff Evidence

### 目标

解决 `staged-workflow-v2` 中一条 candidate 混合多个问题，导致 VERIFY 因部分主张成立而保留整条的问题。

### 数据结构变更

DISCOVER 不再直接生成接近最终 review 的 description 和 suggestion，而是生成：

```json
{
  "file": "path/to/file.py",
  "severity": "medium",
  "claim": "一个单一的疑似缺陷及其直接影响",
  "evidence": "从该文件 diff 中逐字复制的非空片段"
}
```

Suggestion 延迟到 VERIFY 确认问题之后才生成。

### 代码约束

- 每个 candidate schema 只提供一个 `claim` 字段。
- Evidence 必须逐字出现在输入 diff 中。
- Evidence 必须属于 candidate 声明的文件，不能从另一个文件借用。
- VERIFY 必须逐条 keep、revise 或 drop。
- Keep/revise 生成的正式 issue 必须保持在 candidate 原文件中。
- FINALIZE 仍然只能生成 summary，不能修改 verified issues。

### 意图

```text
DISCOVER 只提出单一、可定位的怀疑
→ VERIFY 根据具体 evidence 判断
→ 只有验证后才形成完整 review 和 suggestion
```

这轮优化主要改变 candidate 的结构和代码验证边界，不打开 thinking、不增加 token 或轮数、不增加新工具。

### 测试

```text
40 tests passed
```

### 待运行版本

```text
Run name = atomic-candidates-v1
Baseline = tool-or-complete-v1
Comparison workflow = staged-workflow-v2
```

## 2026-08-08：修复 Atomic Candidate Evidence 校验

### 失败现象

`atomic-candidates-v1` 在 Cal.com 的 DISCOVER 阶段连续两次被框架拒绝，最终保存 `error.json`。

### 诊断

- 两次模型响应均为完整、可解析的 candidate JSON。
- `finish_reason=stop`，completion tokens 分别约为 976 和 977，未接近 2048 上限。
- 失败来自框架校验：模型返回不带 `+/-` 标记的源码片段，而校验器直接与带 diff 标记和缩进的 unified diff 原文比较。

因此这不是 Kimi 空响应、JSON schema 或 token 截断问题，而是 Harness 对 evidence 表示形式的约定错误。

### 修复

- Evidence 仍须来自 candidate 声明的文件。
- 校验前移除每行 unified-diff 标记，并忽略行首尾缩进和空行。
- 多行 evidence 仍须对应连续的源码行。
- 不存在的 evidence 和跨文件 evidence 仍会被拒绝。
- Prompt 同步说明 evidence 不需要包含 diff 标记，避免模型与框架契约不一致。

### 下一次运行

```text
Run name = atomic-candidates-v2
KIMI_THINKING = disabled
Completion token limit = 2048（不变）
```

## 2026-08-08：Atomic Candidates v2（Cal.com）

### 运行结果

```text
Candidates = 6
TP = 1
FP = 5
FN = 3
Precision = 16.7%
Recall = 25.0%
F1 = 20.0%
Tool calls = 0
```

结果与 `tool-or-complete-v1` 在该 case 上相同，原子 candidate 和 evidence 约束没有直接改善最终评分。

### Trajectory 观察

- 第一次 DISCOVER 返回了 7 个 candidate，但其中一条 evidence 使用了 `...` 省略中间代码，未满足连续源码片段约束，因此被拒绝。
- 第二次 DISCOVER 修正 evidence 后通过，证明 evidence 校验修复有效。
- VERIFY 保留 6 条、丢弃 1 条，没有调用 `read_file`。
- VERIFY 对 candidate 0 的理由已经承认 disable 流程最终会把 `backupCodes` 整体设为 `null`，却仍将“同一 backup code 可重复禁用 2FA”判为 keep。决策与自身理由矛盾。
- VERIFY 能修正 candidate 4 中关于 `indexOf` 的错误子主张，但仍保留“null 累积浪费空间”这一低价值问题。
- 三条 golden 漏报（错误文案、大小写敏感、并发重复使用）在 DISCOVER 阶段就没有成为 candidate，VERIFY 无法补回。

### 结论

Atomic candidate 解决的是数据结构和证据可定位性，而不是模型判断质量。当前主要瓶颈变为：

```text
1. DISCOVER 没有发现关键 golden 问题；
2. VERIFY 遇到足以推翻 claim 的反证时仍倾向 keep；
3. 需要文件上下文的判断没有触发 read_file。
```

下一步不继续增加 DISCOVER Prompt 规则。优先在 workflow 代码中把 VERIFY 改为逐 candidate 验证，使每次调用只做一个 keep/revise/drop 决策，并为“理由包含反证但 verdict=keep”提供更清晰的重试边界。

## 2026-08-08：逐 Candidate VERIFY

### 目标

减少批量验证时不同 claim 相互干扰的问题，并为每个候选提供独立的工具调用、重试和轨迹边界。

### Workflow 变更

```text
DISCOVER candidates
  → VERIFY candidate 0 → keep/revise/drop
  → VERIFY candidate 1 → keep/revise/drop
  → ...
  → 聚合 verified issues
  → FINALIZE summary
```

- 每次 VERIFY 请求只包含一个 candidate，并要求返回 `candidate_index=0`。
- Workflow 将局部 index 映射回原始 candidate index。
- 每个 candidate 最多使用 3 个模型 turn，以容纳工具调用或一次格式修正。
- Candidate 总数上限为 10，避免请求数量无界增长。
- 所有 candidate 共享最多 3 次 `read_file` 调用。
- FINALIZE 独立保留最多 2 次尝试。
- Trace 新增 `candidate_result`，记录每条 candidate 的最终 verdict 和原始 index。

全局模型 turn 安全上限由各阶段边界计算，当前最坏情况为 34；它是防失控上限，不代表正常运行轮数。7 个 candidate 在没有工具和重试时预计使用 9 次模型调用：DISCOVER 1 次、VERIFY 7 次、FINALIZE 1 次。

### 测试

```text
42 tests passed
```

### 待运行版本

```text
Run name = per-candidate-verify-v1
KIMI_THINKING = disabled
```

## 2026-08-08：Structured Evidence v1 结果与 Hunk Header 修复

### Suite 结果

- Sentry：DISCOVER 失败。
- Grafana：完整运行并评分。
- Suite 最终汇总 Sentry 失败，同时保留 Grafana 结果，失败隔离符合预期。

Grafana 自动评分：

```text
Candidates = 3
TP = 2
FP = 1
FN = 3
Precision = 66.7%
Recall = 40.0%
F1 = 50.0%
Tool calls = 0
```

结构化 evidence 成功表达了从异步调用到同步调用的 before/after transition，并发现两条 golden。但结果低于 `tool-or-complete-v1` 在 Grafana 的 F1 80.0%，因此不能把结构化 evidence 当作质量提升；它目前是更准确的数据契约。

VERIFY 错误地 drop 了一个与 race condition golden 相关的 candidate，说明逐 candidate 验证仍可能产生错误过滤。其余三条 FN 在 DISCOVER 阶段未出现。仍无 `read_file` 调用。

两次 VERIFY 首次响应再次出现 2048 tokens 全部用于 reasoning、空 content，candidate 局部重试恢复成功。

### Sentry 根因

Candidate evidence 从 `def __init__(` 开始。该源码行只作为 unified diff hunk header 的 section context 出现：

```text
@@ -42,27 +44,53 @@ def __init__(
```

重建器原先完全跳过 hunk header，因此误拒绝合法引用。这不是模型违反结构化 evidence 契约。

### 修复

- 从 hunk header 第二个 `@@` 后提取 section context。
- 将 section context 加入 before 和 after 两侧。
- 不改变 evidence side、连续性或文件归属规则。
- 使用失败轨迹回放后，两次 Sentry 响应的 7 条 candidate 均可通过。

```text
47 tests passed
```

下一次 Sentry run name 使用 `structured-evidence-v2`。

## 2026-08-08：Per-candidate Verify v1（Cal.com）

### 自动评分

```text
Candidates = 5
TP = 1
FP = 4
FN = 3
Precision = 20.0%
Recall = 25.0%
F1 = 22.2%
Tool calls = 0
```

相对 `atomic-candidates-v2`：FP 从 5 降到 4，F1 从 20.0% 升到 22.2%，Recall 不变。

### Workflow 观察

- DISCOVER 生成 7 条 candidate。
- 独立 VERIFY 保留 5 条、丢弃 2 条。
- Candidate 0（disable 后 backup code 可重复使用）被正确 drop。Verifier 明确识别到流程末尾会把 `backupCodes` 整体设为 `null`，修复了批量 VERIFY 中“理由承认反证但仍 keep”的矛盾。
- Nested ternary 的样式类 candidate 也被正确 drop。
- 7 次 candidate 验证仍然没有调用 `read_file`。
- 三条 golden FN 与上一轮相同，说明关键问题在 DISCOVER 阶段就没有被发现，逐条 VERIFY 无法改善 Recall。

### 稳定性观察

两次 VERIFY 首次响应出现：

```text
finish_reason = length
completion_tokens = 2048
reasoning_tokens = 2046
content = ""
```

虽然运行配置为 `KIMI_THINKING=disabled`，Moonshot 仍在这两次请求中报告了大量 reasoning tokens。两次均由 candidate 级重试恢复，说明新的局部失败恢复边界有效；暂不据此提高全局 token 上限。

### 结论

Per-candidate VERIFY 在工程和决策一致性上有效，但单个 case 的微小分数提升不足以证明跨仓库质量提升。下一步先用同一版本运行 Sentry 和 Grafana，不修改 Prompt 或 workflow，以确认：

1. FP 降低是否跨仓库存在；
2. 工具是否仍然始终不被调用；
3. reasoning token 异常是否重复出现。

## 2026-08-08：Sentry Evidence 校验与 Suite 失败隔离

### 失败现象

`per-candidate-verify-v1` 的 Sentry DISCOVER 两次均返回 10 条完整 candidate，但被 evidence 校验拒绝。Suite 随后直接退出，Grafana 没有运行。

### 根因

校验器移除 diff 标记后，把删除行和新增行放进了同一文本流。Unified diff 会在同一位置交错展示 old/new 行，因此模型引用的一段连续新代码可能被一条旧的 `-` 行隔开，形成误拒绝。

### 修复

- 分别使用 context + removed lines 重建变更前片段。
- 分别使用 context + added lines 重建变更后片段。
- Evidence 匹配其中任意一个版本，不再混合 old/new 行。
- 校验错误增加 candidate index 和 file。
- 使用失败轨迹回放验证，两次 Sentry 响应的 10 条 candidate 现在都能通过解析。
- Suite 改为 case 级失败隔离：review 失败时跳过该 case 的 score，但继续运行后续 case；全部结束后统一报告失败。

### 测试

```text
43 tests passed
```

## 2026-08-08：Per-candidate Verify v2 DISCOVER 失败

### 运行结果

Sentry 和 Grafana 都完成了各自的两次 DISCOVER 尝试并保存独立 `error.json`。Suite 在 Sentry 失败后继续运行 Grafana，证明 case 级失败隔离生效。

### 非 token 原因

所有四次响应均为：

```text
finish_reason = stop
reasoning_tokens = 1
```

失败来自 evidence 数据结构限制：

- Sentry candidate 2 在两个非连续代码片段之间使用字面量 `...`，无法作为连续源码片段匹配。
- Grafana candidate 1 将 removed 的异步实现和 added 的同步实现拼进同一个 evidence。该组合既不完整存在于 before 版本，也不完整存在于 after 版本。
- 模型收到带 candidate index/file 的反馈后，第二次仍返回相同 evidence，说明增加同类重试不会解决表达能力问题。

### 设计结论

单个字符串 `evidence` 只能表达一个连续的 before 或 after 片段，无法可靠表达：

```text
旧实现被删除 + 新实现被加入
多个非连续位置共同支持一个 claim
```

下一步应把 evidence 改为结构化引用列表，每条引用显式包含 `side = before | after` 和一个连续 `text`。校验器按 side 验证对应文件版本。不要退回到忽略行类型或接受 `...` 的模糊匹配。

## 2026-08-08：Structured Evidence Refs

### 数据模型

Candidate evidence 从单个字符串改为引用列表：

```json
{
  "evidence": [
    {
      "side": "before",
      "text": "被删除的连续源码片段"
    },
    {
      "side": "after",
      "text": "新增的连续源码片段"
    }
  ]
}
```

代码中新增 `EvidenceRef`，`CandidateIssue.evidence` 变为 `list[EvidenceRef]`。

### 校验规则

- Evidence list 至少包含一条引用。
- `side` 只能是 `before` 或 `after`。
- `text` 必须为非空字符串。
- 每条 text 必须连续出现在声明文件对应 side 的重建源码中。
- Transition claim 使用至少一条 before 和一条 after 引用。
- 多个非连续位置拆成多条引用。
- 不接受 `...` 代替省略代码。

### Workflow 影响

DISCOVER 负责生成结构化引用；VERIFY 通过 dataclass 序列化收到完整的 evidence refs。最终 review issue schema 和 benchmark scorer 不受影响。

### 测试

覆盖普通 after evidence、before/after transition、错误 side、非连续 ellipsis 和跨文件引用：

```text
46 tests passed
```

### 待运行版本

```text
Run name = structured-evidence-v1
KIMI_THINKING = disabled
```

## 2026-08-08：Transient API Failure Recovery

### 失败现象

`structured-evidence-v2` 第一次运行在 DISCOVER 请求遇到 Moonshot `502 Bad Gateway`；第二次运行在 VERIFY 请求等待 120 秒后 timeout。第二次保存失败轨迹时，因为同一 case 目录已由第一次运行创建，又触发 `FileExistsError`，遮住了原始 timeout。

### 失败分类

502 和 timeout 属于模型供应商/网络的瞬时基础设施错误，不属于：

- Agent 的 DISCOVER/VERIFY 判断失败；
- JSON 或 evidence schema 失败；
- token 截断；
- workflow turn 耗尽。

### 恢复策略

- 对 timeout、connection error、HTTP 5xx 和 rate limit 在同一逻辑模型 turn 内最多重试 2 次。
- 默认指数退避 1 秒、2 秒。
- SDK 隐藏重试仍保持关闭，重试由 workflow 显式维护。
- 每次失败记录为 `model_request_error`，包含 stage、attempt、是否继续重试、异常类型、HTTP status 和截断后的错误内容。
- 失败请求增加 `api_attempts`，但不增加 `model_turns`，因为没有收到模型响应。
- 重试次数和退避可由 `LLM_TRANSIENT_RETRIES`、`LLM_RETRY_BACKOFF_SECONDS` 配置。

### Artifact 恢复

- 同一 run/case 多次失败依次保存 `error.json`、`error-2.json` 等文件。
- 只有失败文件时，后续成功重跑可以在同一目录写入 `result.json`。
- 已有 `result.json` 仍禁止覆盖，防止成功实验被无意替换。

### 测试

```text
49 tests passed
```

## 2026-08-08：Structured Evidence v2（Sentry）

### 自动评分

```text
TP = 4
FP = 5
FN = 1
Precision = 50.0%
Recall = 80.0%
F1 = 61.5%
read_file calls = 2
```

相对 `tool-or-complete-v1` 的 Sentry 结果：Precision 保持 50.0%，Recall 从 40.0% 升到 80.0%，F1 从 44.4% 升到 61.5%。

### Golden 命中

- `shard` / `shards` metrics tag 不一致。
- 测试依赖固定 sleep，可能 flaky。
- spawn context process 与 `multiprocessing.Process` 的 `isinstance` 不匹配。
- monkeypatch 将测试中的 `time.sleep(0.1)` 变为 no-op。

唯一 FN 是 join 在 deadline 到期后 `break`，导致剩余 flusher processes 不再执行 terminate。

### 工具行为

这是当前版本第一次观察到真实 `read_file` 调用：

1. 验证 `_create_process_for_shard` 是否在 `flusher.py` 内使用；
2. 验证测试文件中 monkeypatch 与后续 sleep 的关系。

工具调用机制已经生效，但工具覆盖范围仍不充分：读取单一文件不能证明一个方法在整个 repository 中未被调用；`self.buffer` 的外部依赖也需要 repository search，而当前只有 `read_file`。

### FP 人工判断

- Join candidate 把控制流结论说反：deadline 到期后外层 `break` 会跳过后续 terminate，而不是“后续进程仍被立即 terminate”。这是明确技术错误，并对应唯一 FN。
- Kill 后未 join 可能产生 zombie：合理问题，但 golden 未覆盖。
- 新 `SpansBuffer(shards)` 丢失配置：缺少读取 `SpansBuffer` 定义的证据，属于低证据推测。
- 未使用 `_create_process_for_shard`：文件内观察成立，但没有 repository-wide search，只能证明本文件未调用；属于合理但低价值问题。
- 外部代码可能依赖 `self.buffer`：没有调用点或继承关系证据，属于低价值推测。

### 稳定性

- 两次 `read_file` 均成功。
- 一个 VERIFY 首次响应因 2048 reasoning tokens 截断，局部重试恢复。
- 没有出现 502/timeout，新增 transient retry 未被触发。

### 结论

Structured evidence + per-candidate VERIFY 在 Sentry 上提高了 recall，并首次触发工具，但 FP 仍多、控制流验证仍会犯错。Grafana 同版本族 F1 为 50.0%，低于其旧基线 80.0%，因此不能宣称跨仓库整体提升。

下一步应增加 repository search 工具，并在 VERIFY 中区分“当前文件可证实”和“需要全仓库调用点/类型定义”的 claim；暂不继续修改 DISCOVER Prompt。

## 2026-08-08：Repository Search 与分段 Read File

### 问题

Sentry trajectory 中 Agent 虽然调用了 `read_file`，但 `flusher.py` 超过 10,000 characters，工具直接失败并没有返回任何上下文。同时只有准确路径读取能力，Agent 无法自行定位符号定义、调用点和继承关系。

### 新工具链

```text
search_code(query, path?)
  → path + line + matching content
  → read_file(path, line, context_lines)
  → VERIFY keep/revise/drop
```

两个工具保持独立，以便 trajectory 区分“没有定位到代码”和“读到代码但判断错误”。

### `search_code`

- 使用 ripgrep 的 JSON 输出执行 exact-text lexical retrieval。
- 模型只传结构化参数，Python 使用 subprocess argument list，不经过 shell。
- 可选 repository-relative path 限定搜索范围。
- 最多返回 20 条 match，包含 path、line 和 content，并标记 truncated。
- 限制 query 非空、单行、最多 200 characters。
- 禁止绝对路径和 repository traversal。

### `read_file`

- 接口为 `path` 加可选 `line/context_lines`。
- 小文件未指定 line 时返回完整文件和行数元数据。
- 大文件未指定 line 时返回 `total_lines` 和再次分段读取提示。
- 指定 line 时默认读取前后各 50 行，最大各 100 行。
- 返回实际 start/end/total lines 和 truncated 状态。
- 窗口仍受 10,000-character 输出限制，过大时提示缩小 context。

### Workflow

- VERIFY prompt 明确：路径未知时先 search，得到行号后再 read；路径已知时直接 read。
- 两个工具都只在 VERIFY 暴露。
- 全局 tool call budget 从 3 增至 6，以容纳最多三组 search + read。
- 每 candidate 的 3-turn 上限不变；正常 search → read → decision 正好使用 3 turns。

### 测试

覆盖 schema、安全路径、大文件提示、中心行窗口、搜索结果上限，以及完整 search → read → verdict trajectory：

```text
56 tests passed
```

### 待运行版本

```text
Run name = repo-search-v1
KIMI_THINKING = disabled
```

## 2026-08-08：DISCOVER Candidate 级失败隔离

### 失败现象

`repo-search-v1` 尚未进入 VERIFY 就失败。两次 DISCOVER 都生成 9 条候选，但整批被 evidence 校验拒绝，因此新工具没有机会运行。

### 轨迹回放

9 条 candidate 中有 7 条有效，2 条应被拒绝：

- Candidate 7 evidence 使用了 diff 中不存在的 `producer.produce(self.output_topic, payload)`。
- Candidate 8 声明文件为 `test_consumer.py`，但 before evidence 实际来自 `factory.py`，违反文件归属。

模型收到反馈后原样重复这两条引用，整批重试没有恢复价值。

### Workflow 修复

- DISCOVER 的顶层 JSON/candidates list 仍作为批次结构校验。
- 每条 candidate 的 schema 和 evidence 独立校验。
- 有效 candidate 立即保留，无效 candidate 记录到 `candidate_rejections` trace。
- `stage_result` 同时记录 candidate count 和 rejected candidate count。
- 只有所有 candidate 都无效时才反馈模型并重试 DISCOVER。
- 严格 `_parse_candidates` 接口仍保留，用于单元测试和需要全量合法的调用场景。

失败轨迹回放结果：

```text
valid candidates = 7
rejected candidates = 2
```

```text
57 tests passed
```

这次修改将错误恢复边界从“整批 candidate”缩小到“单条 candidate”，避免一个幻觉引用丢失整轮有效发现。

## 2026-08-08：固定 SHA Repository Cache

### 失败现象

重跑 `repo-search-v1` 时，在 Agent 创建前执行 `git fetch` 失败：本地无法解析 `github.com`。这与 Kimi、Prompt、DISCOVER 和工具实现无关。

### Harness 问题

Fixture 已锁定 `head_sha`，但 runner 原先每次都 checkout 到一次性 temporary directory，运行结束即删除。因此每次评测都重新依赖 GitHub DNS 和网络，既慢又不稳定。

### 修复

- 默认将 checkout 缓存在 `evals/repositories/<owner--repo>/<head_sha>/`。
- 缓存目录加入 `.gitignore`，不提交大型 repository 副本。
- 首次 checkout 在同文件系统 temporary directory 完成，验证 SHA 后再原子移动到最终缓存路径，避免留下半成品缓存。
- 使用 `.benchmark-head-sha` marker 验证缓存对应 fixture SHA。
- 相同 repository/SHA 后续运行直接复用，不执行 GitHub fetch。
- 首次 `git fetch` 遇错最多重试 2 次，退避 1 秒、2 秒。
- 测试注入路径仍可选择不使用持久缓存。

缓存无法消除第一次下载的网络要求；必须至少成功 fetch 一次。之后相同 fixture 可离线运行 repository tools。

```text
58 tests passed
```

## 2026-08-08：Repo Search v1（Sentry）

### 自动评分

```text
DISCOVER valid = 6
DISCOVER rejected = 1
VERIFY kept = 4
TP = 2
FP = 2
FN = 3
Precision = 50.0%
Recall = 40.0%
F1 = 44.4%
search_code calls = 0
read_file calls = 0
```

分数与最早 `tool-or-complete-v1` Sentry 基线相同，低于 `structured-evidence-v2` 的 F1 61.5%。由于 candidate sampling 不同，不能把差异直接归因于新增工具；本轮工具实际完全没有被使用。

### Candidate 隔离

一条包含字面量 `...` 的无效 evidence 被记录并拒绝，其余 6 条 candidate 正常进入 VERIFY，证明 candidate 级失败隔离生效。

### TP / FN

命中：

- join deadline 后 break 导致剩余 processes 未 terminate；上一轮读反的控制流本轮判断正确。
- metrics tag `shard` / `shards` 不一致。

漏报：

- fixed sleep flaky；
- spawn context process 的 isinstance 错误；
- monkeypatch 后 sleep 为 no-op。

三条 FN 都没有在 DISCOVER 中形成有效 candidate，因此只允许 VERIFY 使用的 repository tools 无法补回。

### 工具策略失败

Verifier 对 `SpansBuffer` candidate 直接声称其使用全局 Redis 配置、没有 instance-level state，然后 drop；但这些事实不在 diff 中，且没有 search/read `SpansBuffer` 定义。这属于无工具支持的 repository-level 断言。

新增工具解决了 capability 问题，但没有解决 policy 问题：模型知道可以调用工具，并不代表它会正确判断何时必须调用。

### 下一步设计问题

下一步不继续增加工具。应让 VERIFY 显式声明判断依据：

```text
basis = diff | repository
```

- `basis=repository` 必须有该 candidate 的成功工具轨迹，否则框架拒绝 verdict。
- `basis=diff` 的 reason 不得声称定义、调用点、继承关系或外部状态；这一部分先通过 prompt 约束和 trajectory 人工检查，不立即做脆弱的关键词规则。
- Repository tools 仍只负责验证已有 candidate，若要改善 FN，需要另行设计 DISCOVER context retrieval，不能混为同一个变量。

## 2026-08-08：VERIFY Basis 工具策略

### 目标

解决 `repo-search-v1` 中“工具可用但模型不调用，却直接声称 repository facts”的问题。

### Decision contract

每条 VERIFY decision 新增必填字段：

```json
{
  "basis": "diff | repository"
}
```

- `basis=diff`：claim 可完全由 supplied diff 判断。Prompt 禁止 reason 声称 diff 外的定义、调用点、继承、配置或 runtime state。
- `basis=repository`：claim 依赖 repository context。

### 代码强制边界

- 每个 candidate 独立统计成功工具调用。
- Repository basis 没有该 candidate 的成功 `search_code/read_file` 时，parser 拒绝 verdict 并反馈模型重试。
- 工具调用失败、达到 limit 或返回 `ok=false` 不算成功 context。
- Tool result trace 增加原始 candidate index。
- Candidate result trace 增加 basis 和 successful tool call count。
- Diff basis 的 reason 内容暂时仅由 Prompt 约束和人工 trajectory 检查，不增加脆弱关键词规则。
- 每 candidate turn limit 从 3 调整为 4，使错误的无工具 repository verdict 被拒绝后，仍有完整的 search → read → decision 恢复空间。正常路径的请求数不变。

### 测试环境隔离

持久 repository cache 出现后，Pytest 会递归收集缓存的 Sentry tests。新增 `pytest.ini`，将本项目发现范围固定为 `tests/` 并排除 `evals/repositories`。

```text
59 tests passed
```

### 待运行版本

```text
Run name = verify-basis-v1
KIMI_THINKING = disabled
```

## 2026-08-08：Tool-call Protocol Recovery

### 失败现象

`verify-basis-v1` 在 candidate 4 用完 4 turns。失败不是全局 6 次工具额度耗尽；前四个 candidate 均使用 diff basis，工具调用数为 0。

### 轨迹诊断

Candidate 4 两次返回：

```json
{"path": "src/sentry/spans/buffer.py", "query": "class SpansBuffer"}
```

这表明模型意图搜索，但把参数放进普通 assistant content；API response 的 `tool_calls=[]`，框架不能执行。随后模型在没有工具结果的情况下返回 repository basis，basis contract 将其正确拒绝。

### 修复

- VERIFY prompt 明确：必须通过 actual function tool call 调用工具，禁止把 argument JSON 当普通 content 返回。
- Framework 识别仅包含 `query/path/line/context_lines` 的 pseudo-tool JSON。
- 识别后反馈明确要求立即调用 `search_code/read_file` function tool，而不是只报告 decisions schema 错误。
- 无工具 repository basis 的反馈也明确要求实际 function call。
- 仍不由框架擅自执行普通 content；工具调用必须由模型按协议发出，保持安全边界和 trajectory 真实性。

测试覆盖以下 4-turn 恢复路径：

```text
pseudo-tool content
→ targeted feedback
→ actual search_code
→ actual read_file
→ repository decision
```

```text
59 tests passed
```

同一 `verify-basis-v1` run name 可直接重跑，失败 artifact 会保留。

## 2026-08-08：Stage-specific DISCOVER Token Budget

### 失败现象

重跑 `verify-basis-v1` 时，两次 DISCOVER 都在第 8 条 candidate 中途结束：

```text
finish_reason = length
completion_tokens = 2048
reasoning_tokens = 1
JSON incomplete
```

因为顶层 JSON 没有闭合，框架无法进入 candidate 级解析和失败隔离。第二次重试生成几乎相同的长输出，也再次截断。

### 与历史失败的区别

此前不提高 token 的结论基于 `finish_reason=stop` 或 reasoning tokens 异常耗尽；那些失败不是正常输出空间不足。本次是正常 DISCOVER JSON 明确达到 completion cap，有直接证据支持增加预算。

### 修复

- 新增 `LLM_DISCOVER_MAX_COMPLETION_TOKENS`，默认 4096。
- 仅 DISCOVER 使用 4096，以容纳最多 10 条带 structured evidence 的 candidates。
- VERIFY 和 FINALIZE 继续使用 `LLM_MAX_COMPLETION_TOKENS=2048`。
- 不修改 DISCOVER Prompt，不同时改变候选策略。

```text
59 tests passed
```

同一 `verify-basis-v1` run name 可继续重跑；默认值已生效，`.env` 可显式记录 4096。

## 2026-08-08：Verify Basis v1（Sentry）结果

### 自动评分

```text
DISCOVER candidates = 8
VERIFY kept = 4
TP = 1
FP = 3
FN = 4
Precision = 25.0%
Recall = 20.0%
F1 = 22.2%
search_code calls = 0
read_file calls = 0
```

仅命中 metrics tag `shard/shards` 不一致。该结果低于 `repo-search-v1` 的 F1 44.4% 和 `structured-evidence-v2` 的 61.5%。

### Token 修复验证

DISCOVER 使用 2050 completion tokens，`finish_reason=stop`，完整生成 8 条 candidates。Stage-specific 4096 budget 解决了上一轮 2048 截断问题。

### Basis 策略结果

所有 8 条 VERIFY decision 都由模型声明为 `basis=diff`。因此 repository basis 的工具前置条件从未触发，整个 run 没有 search/read。

这说明 self-declared basis 只能防止一种情况：模型主动承认依赖 repository，却没有工具证据。它无法防止模型把实际依赖外部事实的判断错误标记为 diff。

例如模型未读取 `SpansBuffer` 定义，却以 diff basis 声称它是 Redis wrapper、没有需要保留的内存状态；Prompt 禁止此类行为，但代码无法从字符串稳定判断该断言是否越界。

### 关键控制流错误

Join candidate 将：

```python
for process in processes:
    if remaining_time <= 0:
        break
```

错误解释为“break 只退出 inner while，随后继续遍历并 terminate 剩余进程”。实际 `break` 位于 outer for body，会退出 for，正是 golden 所指出的剩余 processes 未 terminate。

因此同一错误同时形成：

- 一个 FP：声称 deadline 后仍继续 terminate；
- 一个 FN：漏掉 deadline 后跳过 terminate。

这是 diff-local control-flow reasoning 错误，repository search 不能解决。

### 结论

Basis contract 作为可观察字段有价值，但作为工具强制策略失败：模型通过把所有判断标为 diff 绕过了 repository gate。当前不应继续叠加 basis Prompt。

下一步应拆开两个问题：

1. 对明确需要 repository context 的 candidate，由代码强制执行 context acquisition，而不依赖模型自报 basis；
2. 对 diff-local 控制流 candidate，引入专门的结构化控制流复核，而不是调用 repository search。

在设计出可靠的代码路由规则前，`verify-basis-v1` 不提升为基线。

## 2026-08-08：DISCOVER Required Facts 与自动 Context Acquisition

### 目标

将“是否需要 repository context”的决定从 VERIFY verdict 前移到 DISCOVER candidate，并由代码在 VERIFY 前执行上下文获取。

### Candidate schema

新增：

```json
{
  "required_facts": [
    {
      "question": "验证 claim 所需的具体事实",
      "source": "repository",
      "search_query": "用于精确定位的符号或文本"
    }
  ]
}
```

- 每条 candidate 必须包含 required_facts list。
- 完全由 diff 判断时使用空列表。
- 每条最多 2 个 repository facts。
- Question 和 search_query 必须为非空字符串。

### Code-owned workflow

```text
DISCOVER
→ 对每个 required fact 自动 search_code
→ 自动 read_file 首个 match 附近 50 行
→ 将 search/read 结果注入该 candidate VERIFY
→ VERIFY
```

- 自动工具结果标记 `stage=acquire_context`、candidate index、fact index 和 `origin=workflow`。
- Required facts 非空时，代码只接受 repository basis。
- Required facts 为空时，代码只接受 diff basis。
- 自动 context acquisition 的成功调用计入 candidate repository context，不要求模型重复调用。
- 模型仍可在 VERIFY 中主动使用工具补充上下文。
- 全局 tool budget 调整为 40，覆盖最多 10 candidates × 2 facts × search/read；这是本地只读工具上限，不增加模型请求数。

### 当前边界

该设计不再依赖 VERIFY 临时决定是否调用工具，但仍依赖 DISCOVER 是否完整、正确地声明 required facts。下一轮 trajectory 需要检查：

1. repository-dependent candidate 是否输出 required facts；
2. search query 是否能精确命中定义/调用点；
3. 自动读取的首个 match 是否是正确上下文；
4. FP 是否因获得上下文而被 drop。

```text
59 tests passed
```

### 待运行版本

```text
Run name = required-facts-v1
KIMI_THINKING = disabled
```

## 2026-08-08：required-facts-v1 首次运行失败与搜索降级

### 运行结果

- Candidate 0 声明了 repository required fact，workflow 自动调用 `search_code`。
- 运行环境没有 `rg`，搜索返回失败；因此没有自动读取到仓库上下文。
- VERIFY 随后要求 repository basis 和成功工具调用，但 Kimi 连续四轮只返回 decision JSON，没有调用工具，最终超过单 candidate turn limit。

### 根因

这不是模型轮数不足，而是 workflow 进入了不可达状态：自动取证依赖可选的外部命令 `rg`，取证失败后又把补救责任交回给不调用工具的模型。

### 修改

- 保留 `rg` 作为首选的快速搜索实现。
- 当系统找不到 `rg` 时，`search_code` 自动降级到 Python 固定文本搜索。
- 降级搜索保持相同的结构化结果与数量上限，并跳过 `.git`、`.venv` 和 `node_modules`。
- 增加无 `rg` 环境的回归测试。

### 结论

框架负责的 required-fact acquisition 不应依赖模型临场恢复，也不应因可选系统依赖缺失而变成无法完成的状态。修复后重新运行时应使用新的 run name，避免与失败轨迹混合。

## 2026-08-08：focused-discover-v1

### 目标

减少 DISCOVER 中自相矛盾、重复和低证据候选，提高进入 VERIFY 的候选密度。

### 唯一变量 / 代码变更

- Candidate 上限从 10 降到 5，并要求按 evidence strength 排序。
- 优先具体 changed line/code path、明确 runtime/behavioral failure，以及 diff 中有初始证据的候选。
- Diff 直接反驳 claim 时禁止返回。
- Diff 不足时仍允许保留候选，但必须声明 `required_facts` 交给后续仓库验证。
- 没有具体 failure path 时，不返回测试增强建议、推测性资源泄漏或设计偏好。

本轮不修改 VERIFY、工具策略和候选解析流程。

### 待运行版本

```text
Run name = focused-discover-v1
KIMI_THINKING = disabled
```

### 观察指标

1. DISCOVER candidate 数量；
2. 人工 keep / needs_context / drop 比例；
3. 自相矛盾和重复候选是否减少；
4. Golden 对应的候选是否仍被发现。

### Sentry 运行结果

```text
Candidates = 5
Final issues = 3
TP = 2, FP = 1, FN = 3
Precision = 66.7%, Recall = 40.0%, F1 = 50.0%
```

与 `required-facts-v2` 对比：

```text
Precision: 20.0% → 66.7%
Recall:    20.0% → 40.0%
F1:        20.0% → 50.0%
```

### Trajectory 观察

- DISCOVER 从 10 条收敛到 5 条，最终保留 3 条。
- 正确发现 `join()` 中 `break` 提前退出 process loop，遗漏剩余 process cleanup；上一轮对此处控制流的理解相反。
- 继续发现 `shard` / `shards` metric tag 不一致。
- 两个 repository-dependent 推测经自动 search/read 后被正确 drop：tag 长度缺少证据、旧 process entry 会被覆盖。
- `required_facts` 自动取证执行 6 次成功工具调用。

### 人工抽查

唯一 FP 是 restart limit：`>` 判断在 PR 前已经存在，不属于本 PR 引入；模型还错误声称抛出 `RuntimeError` 后 counter 会继续 increment，实际 `raise` 会中止当前控制流。

### 结论

保留本轮修改。候选限额和 evidence-strength 排序显著提高了 Sentry 单 case 的候选密度与最终 precision，但仍需在其他仓库 case 上验证，不能把单次提升直接归因于稳定的跨仓库改进。

### 三仓库复测

```text
Case           TP  FP  FN  Precision  Recall  F1
calcom-10600    1   3   3      25.0%   25.0%  25.0%
grafana-79265   3   1   2      75.0%   60.0%  66.7%
sentry-93824    2   1   3      66.7%   40.0%  50.0%
---------------------------------------------------
Micro total     6   5   8      54.5%   42.9%  48.0%
Macro average                         41.7%  47.2%
```

所有 case 均完成，没有 workflow error。三仓库共 DISCOVER 15 条、最终输出 11 条。

人工观察：

- Cal.com 的 3 个自动 FP 中，plaintext backup-code response 是正常的一次性展示流程；null entry “持续增长”与固定数组行为不符；object URL cleanup 可能是合理但低价值的非 golden 问题。关键的大小写验证与并发重复消费仍漏报。
- Grafana 的唯一自动 FP 与同步 `TagDevice`/错误传播 TP 高度重叠，更像同一根因未去重；真正漏掉的是并发设备上限和 `Exec(args...)` 编译错误。
- Sentry 的唯一 FP 是 PR 前已存在的 restart-limit 判断，并混有 `raise` 后继续 increment 的错误控制流推理。

跨仓库结果支持“候选收敛提高 precision”的方向，但尚不能证明 recall 稳定提升。当前主要剩余问题是：重复根因、未检查问题是否由本 PR 引入，以及 DISCOVER 对并发/类型/API 签名问题的召回不足。

## 2026-08-08：required-fact 精确路径路由

### 触发问题

`focused-discover-v1/discourse-benchmark-2` 的 candidate 4 把
`app/views/topics/show.html.erb` 作为 `search_query`。该文件实际存在，但
`search_code` 执行内容搜索，因此得到 0 matches。Kimi 随后连续复述空的
acquisition JSON，没有返回 VERIFY decision，最终超过 candidate turn limit。

### 修改

- Required-fact acquisition 先安全解析 `search_query` 是否为仓库内现有文件。
- 精确文件路径命中时直接调用 `read_file`，不再执行内容搜索。
- 普通符号或文本仍保持 `search_code → read_file(first match)`。
- 绝对路径、仓库外路径和不存在的路径不会进入直接读取分支。
- 增加精确 template path 只触发一次 `read_file` 的回归测试。

### 验证

```text
61 tests passed
```

### 结论

Required fact 目前仍使用单一 `search_query` 字段，但 workflow 可以区分两种常见定位意图：已知文件路径直接读取，未知符号/文本先搜索再分段读取。

### Dev cases 复测：path-aware-context-v1

```text
Case           TP  FP  FN  Precision  Recall  F1
calcom-10600    1   1   3      50.0%   25.0%  33.3%
grafana-79265   2   3   3      40.0%   40.0%  40.0%
sentry-93824    2   0   3     100.0%   40.0%  57.1%
---------------------------------------------------
Micro total     5   4   9      55.6%   35.7%  43.5%
```

与 `focused-discover-v1` 的 micro score 对比：precision 基本持平
（54.5% → 55.6%），recall 下降（42.9% → 35.7%），F1 下降
（48.0% → 43.5%）。

本轮三个 dev case 没有 candidate 提供精确文件路径；所有 acquisition 均走
`search_code`，因此该评分不能衡量新路径路由的效果。分数差异主要体现 Kimi
在重复采样中的 candidate 波动。

人工 FP 观察：

- Cal.com 的 40-bit entropy 评论技术依据较弱：5 random bytes 本身就是 40 bits，hex 只是编码，没有额外减少这 5 bytes 的熵；是否不足还依赖在线尝试限制和产品要求。
- Grafana 的同步 `TagDevice` 评论与已命中的错误传播根因重叠；future timestamp 评论缺少实际 failure path；移除 interface injection 属于测试性/设计偏好，不满足本轮 DISCOVER 规则。
- Sentry 最终只输出两个 golden-matched issues，没有 FP；repository context 正确 drop 了三条推测。

结论：保留路径路由作为 workflow robustness 修复，但不把本轮 score 变化解释为该修复带来的 review 能力变化。其直接效果应通过此前失败的 Discourse heldout trajectory 验证。

## 2026-08-08：RequiredFact 结构化 locator

### 触发问题

Discourse 再次失败。DISCOVER 把自然语言检索意图写入 `search_query`，例如：

```text
before_filter :ensure_logged_in or authenticate_user or similar in TopicsController
```

`search_code` 是精确文本搜索，因此返回 0 matches；Kimi 随后连续复述空的
acquisition context，没有返回 VERIFY decision。

### 根因

单一 `search_query` 字段同时承载文件路径、精确符号和自然语言搜索请求，模型
输出协议与工具能力不一致。

### Schema 修改

```json
{
  "question": "验证 claim 所需的事实",
  "source": "repository",
  "path": "可选的仓库相对文件或目录",
  "query": "可选的精确源码文本或符号"
}
```

每条 fact 必须提供 `path`、`query` 或两者：

```text
path + query → 在指定路径搜索，再读取首个 match
path only    → 直接读取文件
query only   → 全仓库搜索，再读取首个 match
```

- DISCOVER prompt 明确禁止把自然语言搜索请求放入 `query`。
- Parser 拒绝既没有 path 也没有 query 的 fact。
- 移除旧 `search_query` 模型字段和“猜测 query 是否为路径”的隐式路由。
- README、model tests 和 reviewer workflow tests 同步更新。

### 验证

```text
63 tests passed
```

### 待验证

使用新 run name 重跑 Discourse，检查 DISCOVER 是否输出可执行 locator、workflow
是否完成，以及 acquisition 是否命中目标 controller/template。

### Discourse 验证结果：structured-locator-v1

Workflow 成功完成并生成 `result.json`，不再发生 acquisition JSON 复述或 VERIFY
turn-limit。DISCOVER 输出 5 candidates、6 required facts；全部使用结构化
`path + query`。其中 4 个 facts 搜索命中并完成分段读取，2 个搜索无匹配。

但最终两条评论经人工检查均不应保留：

1. “unsubscribe 没有 authentication guard”是明确误报。仓库文件顶部的
   `before_filter :ensure_logged_in` action list 明确包含 `:unsubscribe`。模型使用
   `before_filter.*authenticate` 作为 query；工具是 fixed-text search，因此无匹配，
   模型却把“没有搜索到”错误解释成“没有认证保护”。
2. “route 未 catch `loadTopicView`”属于低价值框架行为推测。Route model promise
   rejection 可以交给 Ember route error flow；评论关于 `afterModel` 提前 mutation
   的说法也自相矛盾，因为 model reject 时 `afterModel` 不会执行。

结论：结构化 locator 修复了 workflow 可执行性，但没有保证 required fact 已经被
实际回答。目前代码把“工具调用成功但 0 matches”也算作 repository context，允许
VERIFY keep。下一步应区分 tool success 与 fact resolution：存在未解析 required
fact 时不允许 keep/revise，只允许 drop 或继续获取上下文。

## 2026-08-09：Kimi K3 首次对照失败

### 配置

```text
LLM_MODEL = kimi-k3
Run name = structured-locator-k3
Cases = sentry-93824, grafana-79265, calcom-10600
```

### 结果

三个 case 均在 DISCOVER 请求阶段发生 `APITimeoutError`，没有进入候选生成和
workflow 验证。当前 HTTP timeout 为 120 秒，每次请求又执行 2 次 transient
retry，因此每个 case 连续等待 3 次后失败。

通过当前 `https://api.moonshot.cn/v1` 的 Models API 确认，该账号可用模型列表
包含 `kimi-k3`；因此不是 model ID 或 endpoint 不支持，属于 K3 完整响应延迟超过
当前非流式请求 timeout，或服务容量波动。

### 下一步

先只跑一个 dev case，将 `LLM_TIMEOUT` 提高到 600 秒并把
`LLM_TRANSIENT_RETRIES` 临时设为 0。确认单次 K3 inference 可以完成后，再决定
是否跑完整三 case；不要直接在高 timeout 下保留三次重试。

### K3 reasoning effort 调整

官方参数说明确认 K3 始终运行 thinking，不能使用 `KIMI_THINKING=disabled`；其
顶层 `reasoning_effort` 支持 `low`、`high`、`max`，默认值为 `max`。首次请求
未显式传参，因此实际使用最高推理强度，可能是非流式请求超过 120 秒的重要原因。

代码新增 `LLM_REASONING_EFFORT`，对 `kimi-k3` 默认发送：

```json
{"reasoning_effort": "low"}
```

- K3 只发送顶层 `reasoning_effort`，不发送 K2 的 `extra_body.thinking`。
- K2.x 行为保持不变。
- K3 配置仅接受 `low`、`high`、`max`。
- README 和请求参数回归测试已更新。

```text
65 tests passed
```

### K3 low：Sentry 单 case 结果

```text
Run name = structured-locator-k3-low
TP = 3, FP = 0, FN = 2
Precision = 100.0%, Recall = 60.0%, F1 = 75.0%
```

这是当前 Sentry 自动评分最高的一轮。最终三条 issue 均命中 golden：

1. monkeypatched `time.sleep` 使新增等待成为 no-op；
2. deadline 到期后 `break` 跳过剩余 process cleanup；
3. metric tag 使用 `shard` / `shards` 不一致。

两个 repository-dependent 推测均被正确 drop。对于 `self.buffer` 候选，workflow
的 fixed-text search 首先误命中 `self.buffers`；K3 在 VERIFY 中主动追加
`search_code("flusher.buffer")`，得到 0 matches 后才删除候选，表现出补充取证行为。

Usage：

```text
Model requests = 8
Prompt tokens = 41,281
Completion tokens = 3,536
Reasoning tokens = 1,249
Total tokens = 44,817
Cached prompt tokens = 6,912
```

两个自动 FN 中，“fixed sleep can be flaky”与已命中的 monkeypatched no-op 指向
同一新增 sleep，但 benchmark 将其作为另一条 golden comment；另一个真实 FN 是
`SpawnProcess` 与 `multiprocessing.Process` 的类型判断问题。

结论：K3 low 在单个 Sentry case 上同时改善了候选质量、工具补充调用和最终评分，
支持继续进行三 dev case 对照；但单 case 仍不足以证明跨仓库稳定提升。

### K3 low：三 dev case 汇总

```text
Case           TP  Reported FP  FN  Precision  Recall  F1
calcom-10600    0       1        4       0.0%    0.0%   0.0%
grafana-79265   2       1        3      50.0%   40.0%  44.4%
sentry-93824    3       0        2     100.0%   60.0%  75.0%
```

三 case 共 8 个最终 candidates、5 TP、9 FN。按一对一匹配应有 3 FP，因此 micro
precision 为 62.5%、recall 为 35.7%、F1 为 45.5%。这比 K2.6
`focused-discover-v1` 的 micro precision 54.5% 更高，但 recall 42.9% 更低，
F1 48.0% 略低。

Cal.com 没有命中 golden，但唯一输出的 modal-dismiss state 问题具有具体代码路径，
更适合人工标为“合理非 golden”，而不是明确技术误报。Grafana 命中并发 device-limit
race 和同步 TagDevice/认证失败；其中两条关于 TagDevice 的最终评论高度重复。低价值
DI registration 评论未命中，且真正的 `Exec(args...)` 编译错误仍然漏报。

Usage 总计：

```text
Model requests = 24
Prompt tokens = 146,336
Completion tokens = 12,607
Reasoning tokens = 3,781
Total tokens = 158,943
Cached prompt tokens = 31,744
```

### Scorer 计数 bug

当前 scorer 对每个 candidate/golden 两两判断。一条 golden 被多个 candidates 匹配
时，`candidate_matched` 会把所有匹配 candidate 标为 true，但 `golden_matches` 只
保留最高 confidence candidate。未被选中的重复 candidate 随后既不成为 TP，也不
进入 FP。Grafana 因此出现 4 candidates 但 `tp + fp = 3` 的不一致；precision 使用
`tp / total_candidates`，数值未被高估，但 FP 明细和 `fp` 字段少计 1。下一步应在
选定最终一对一匹配后，再从最终 TP candidate indices 计算 false positives。

## 2026-08-09：Scorer v2 一对一最大匹配

### 目标

修复 candidate/golden collision 导致重复 candidate 既不计 TP 也不计 FP 的问题，
让重复输出真实降低 precision。

### 设计

1. Judge 仍对所有 candidate × golden pair 独立判断，保存完整
   `pairwise_judgments`。
2. 在所有 match=true 的边上执行一对一二分匹配。
3. 优化目标首先最大化匹配数量（TP），TP 相同时最大化 confidence 总和。
4. 最终未选中的 candidate 全部计为 FP。
5. 若未选 candidate 匹配了已被另一 candidate 占用的 golden，FP reason 标记为
   `duplicate_match`，并记录 competing golden 与胜出的 candidate index。
6. 未匹配 golden 计为 FN。

### 一致性约束

```text
TP + FP = total_candidates
TP + FN = total_golden
```

违反约束时 scorer 直接失败，不再写出内部计数不一致的 evaluation。

### 测试

- 两个 candidates 命中同一 golden：一个 TP，一个 duplicate FP。
- Broad candidate 同时命中两个 goldens：匹配优先选择可产生 2 TP 的组合，而不是
  只选择单条最高 confidence 边。
- 全部 pairwise judgments 被保存。

```text
67 tests passed
```

## 后续记录模板

```markdown
## YYYY-MM-DD：版本或实验名

### 目标

### 唯一变量 / 代码变更

### 运行配置

### 自动评分

### Trajectory 观察

### 人工抽查

### 结论

保留 / 撤销 / 继续观察
```
