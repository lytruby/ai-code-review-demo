# Code Review Agent 搭建与演进笔记

这份笔记记录当前 Code Review Agent 从单轮 LLM workflow 演进到带工具循环、状态协议、失败恢复和 benchmark 评估的过程。重点不是最终代码，而是每一步遇到的问题、设计判断和验证方法。

## 1. 项目目标

输入一个 Pull Request 的固定 diff，让 Agent：

1. 理解代码改动。
2. 在上下文不足时读取仓库中的完整文件。
3. 输出结构化 code review issues。
4. 使用 golden comments 和 LLM Judge 评分。
5. 通过可重复实验观察 Agent 是否进步。

当前使用：

- Review 模型：Kimi。
- Judge 模型：OpenAI。
- Benchmark：`withmartian/code-review-benchmark` 的离线 case。
- 开发集：Sentry、Grafana、Cal.com。
- 保留集：Discourse、Keycloak。

## 2. Agent 的基础模型

书中给出的基础关系可以概括为：

```text
Agent = LLM + Context + Tools
```

在本项目中：

- LLM：Kimi，通过 OpenAI-compatible Chat Completions API 调用。
- Context：system prompt、PR diff、历史消息和工具结果。
- Tool：`read_file`，用于读取 benchmark 临时仓库中的完整文件。

Agent 外部还有一层 Harness：

```text
Harness = 上下文组织 + 工具执行 + 安全约束 + 状态验证 + 纠正重试 + 轨迹记录
```

模型负责判断和生成动作，Python 代码负责保证流程安全、可终止、可观察。

## 3. 从 Workflow 到 Agent 循环

### 3.1 最初的 workflow

最初流程接近单轮调用：

```text
PR diff → LLM → JSON review
```

这种方式简单，但模型只能看到 diff，无法主动获取缺失上下文。

### 3.2 加入 `read_file`

项目加入了一个受限工具：

```json
{
  "name": "read_file",
  "parameters": {
    "path": "repository-relative path"
  }
}
```

工具具有以下安全约束：

- 只接受仓库相对路径。
- 禁止路径逃逸到仓库外。
- 只读取 UTF-8 文本文件。
- 单个文件最大 10,000 字符。
- 错误以结构化工具结果返回给模型。

Eval runner 会 checkout case 锁定的 PR head SHA，然后把临时仓库路径交给 Agent。因此 `read_file` 读取的是 PR 修改后的完整文件，而不是当前 Agent 项目本身。

### 3.3 ReAct 式循环

当前 Agent 的核心循环是：

```text
模型响应
├── tool_call → 执行 read_file → 结果加入消息历史 → 下一轮
└── complete JSON → 解析并结束
```

它具备一个小型 ReAct 轨迹：模型决定动作、框架执行工具、模型观察结果后继续。但工具是否调用仍由模型决定，因此它处于“带工具循环的 workflow”到自主 Agent 的过渡阶段。

当前限制：

```python
MAX_TOOL_CALLS = 3
MAX_MODEL_TURNS = 5
```

工具次数与模型轮数分别限制，防止无限循环和不可控费用。

## 4. System Prompt 的演进

### 4.1 将指令与不可信代码分离

最初，评审指令和 diff 放在同一条 user message 中。后来调整为：

```text
system message：角色、评审流程、工具规则、输出协议
user message：不可信的 PR diff
```

这样 system prompt 承担“操作手册”的角色，静态指令和动态数据边界更清楚。

### 4.2 从规则堆叠改为 SOP

评审提示改成流程驱动：

1. 理解每个 patch 改变的行为。
2. 需要额外上下文时使用 `read_file`。
3. 描述问题的可能失败场景和影响。
4. 排除纯风格意见，保留有代码依据的正确性、安全性和生命周期风险。
5. 只报告 PR 引入或暴露的问题。

流程式提示比大量互相竞争的规则更容易执行和调试。

### 4.3 一次失败的 Prompt 实验

曾经把验证要求设置得过严：

- 必须证明具体输入或执行路径。
- 必须证明可观察后果。
- 必须确认不存在等价保护。
- 强调宁可不报也不要无依据问题。

结果 Kimi 连续返回：

```json
{
  "summary": "No significant issues found",
  "issues": []
}
```

这说明 Prompt 优化不是规则越多越好。过高的报告门槛会降低 recall，模型也可能选择最容易满足格式要求的空结果。

后续将要求放宽为“有代码依据的合理风险”，恢复了有效输出。

## 5. 为什么需要轨迹

仅保存最终 review 时，无法判断空结果来自：

- 模型没有理解任务。
- 模型认为 diff 足够。
- 模型调用工具失败。
- 模型仍停留在计划阶段。

因此加入 `trace`，记录可观察事件：

```text
model_response
tool_result
workflow_feedback
```

轨迹不记录或伪造模型的隐藏思考，只记录 API 可观察到的内容、工具调用和框架反馈。

轨迹揭示过一个关键问题：模型输出了 “I need to review”，但没有 tool call。旧框架看到没有 tool call，就把这条计划误认为最终结果。

可观测性让问题从“模型为什么表现不好”变成了可以定位的状态机问题。

## 6. Workflow 完成协议

### 6.1 不成功的双状态设计

第一版尝试让模型返回：

```text
needs_context
complete
```

同时又提供原生 `read_file` tool call。结果 `needs_context` 和 tool call 表达了同一个中间状态，造成协议重复。

Prompt 还要求返回 JSON，Kimi 因此连续输出：

```json
{
  "status": "needs_context",
  "summary": "Need additional context to complete review",
  "issues": []
}
```

但始终没有调用工具，最终达到轮数限制。

### 6.2 当前的单一完成状态

当前协议删除了 `needs_context`，只保留：

```json
{
  "status": "complete",
  "summary": "...",
  "issues": []
}
```

中间状态不再使用 JSON status，而由消息类型表达：

```text
tool_call = workflow 继续
status=complete = workflow 结束
其他普通输出 = 非法响应
```

System prompt 明确：

- 需要上下文时直接调用 `read_file`。
- 在评审完成前不要返回 JSON。
- 只有最终评审才返回 `status="complete"` 的 JSON。
- 计划、意图或“还需要检查”的描述不算完成。

这个协议避免用两个机制表达相同状态。

## 7. 验证、纠正与失败恢复

### 7.1 结构验证

最终结果必须满足：

- `status` 必须是 `complete`。
- `summary` 必须是字符串。
- `issues` 必须是数组。
- 每个 issue 必须包含 file、severity、description、suggestion。
- severity 只能是 low、medium、high。

### 7.2 纠正重试

如果模型没有调用工具，又没有返回合法的 complete JSON，框架不会立即结束，而是加入反馈：

```text
Invalid final review: ...
If you need more context, call read_file directly.
Otherwise return the completed review with status "complete".
```

反馈进入消息历史，模型在下一轮可以纠正。

### 7.3 轮数保护

如果模型在最大轮数内仍未完成，框架抛出明确错误：

```text
AI review did not finish within the workflow turn limit
```

不应该在没有证据时直接增加轮数。应该先分析轨迹，判断是合理调查尚未完成，还是同一错误在循环。

### 7.4 失败轨迹持久化

成功运行保存 `result.json`；失败运行保存 `error.json`：

```text
evals/runs/<provider>/<run-name>/<case-id>/
├── result.json
└── error.json
```

`error.json` 包含错误类型、错误信息和失败前轨迹。这样即使临时 checkout 仓库已经删除，也能复盘 Agent 的行为。

## 8. Eval 设计

### 8.1 固定 Fixture

每个 fixture 保存：

- repository 和 PR number。
- 固定的 base SHA、head SHA。
- Agent 输入使用的固定 diff。

运行时校验 checkout 得到的 SHA，保证不同 Agent 版本面对相同输入。

### 8.2 Provider 与 Run Name

两个维度承担不同职责：

```text
provider = 模型供应商，例如 kimi、openai
run-name = Agent 版本或实验，例如 baseline、tool-or-complete-v1
```

目录结构：

```text
evals/runs/kimi/tool-or-complete-v1/sentry-93824/
├── result.json
└── evaluation.json
```

这能防止覆盖基线，并支持同一模型下的 Agent A/B 实验。

### 8.3 Judge 与指标

OpenAI Judge 将每个 candidate issue 与每个 golden comment 做语义匹配，然后计算：

```text
TP：命中的 golden comment
FP：没有命中任何 golden 的 candidate
FN：没有 candidate 命中的 golden comment

precision = TP / candidates
recall = TP / golden comments
F1 = precision 与 recall 的调和平均
```

目前暂未做 candidate 去重。

## 9. 当前实验结果

Sentry case 的结果：

| 指标 | Kimi baseline | tool-or-complete-v1 |
|---|---:|---:|
| Candidates | 11 | 4 |
| TP | 2 | 2 |
| FP | 9 | 2 |
| FN | 3 | 3 |
| Precision | 18.2% | 50.0% |
| Recall | 40.0% | 40.0% |
| F1 | 25.0% | 44.4% |

主要变化：

- 误报从 9 条减少到 2 条。
- Precision 明显提升。
- Recall 保持不变。
- 命中内容发生变化：新版命中了 metrics tag 和 deadline/termination 问题。
- 新版没有调用 `read_file`，说明这次提升主要来自 system prompt 和完成协议，而不是额外文件上下文。

不能只根据一个 case 宣布整体能力提升。下一步应使用同一 Agent 版本运行 Grafana 和 Cal.com，验证是否泛化，避免针对 Sentry 过拟合。

## 10. Thinking 与工具调用

当前 Kimi 配置默认关闭 thinking，以降低延迟和费用。

需要区分：

- Thinking：模型投入多少推理过程。
- Tool calling：模型是否认为需要外部上下文。
- Harness：框架是否允许、约束并正确执行模型动作。

关闭 thinking 不等于工具不可用；模型不调用工具也不一定是失败。如果仅凭 diff 就能完成高质量 review，直接结束是合理行为。

若要验证 thinking 的价值，应保持代码和 Prompt 不变，只切换：

```env
KIMI_THINKING=enabled
```

然后使用新的 run name 对相同 case 做 A/B，比较质量、轨迹、延迟和费用。

## 11. 当前架构中的关键取舍

### 为什么先用 workflow，而不是完全自主 Agent

- Code review 的输入和输出边界清晰。
- 固定流程更容易测试和复现。
- 工具、轮数和结果结构都可以由代码约束。
- 在建立可靠评估前增加自主性，难以判断变化是否真正改善质量。

### 为什么不强制每次调用 `read_file`

- 工具是为缺失上下文服务，不是任务目标。
- 强制读取会增加延迟和 token。
- 有些问题仅凭 diff 就能可靠发现。
- 是否需要强制读取，应由跨 case 的评估数据决定。

### 为什么不依赖自然语言关键词判断完成

检测 “I need to review” 等关键词容易误判。当前使用结构化 `status="complete"` 作为完成信号，用 tool call 表达中间动作。

### 为什么需要最大轮数

模型可能重复同一动作或持续返回无效状态。最大轮数提供成本控制和确定的失败边界，并与失败轨迹结合用于诊断。

## 12. RAG 与 Memory 在当前项目中的位置

当前 Agent 暂时不需要完整 RAG：

- benchmark checkout 已经提供准确的目标仓库。
- `read_file` 可以按路径获取局部上下文。
- 当前主要瓶颈是评审判断与流程可靠性，而不是大规模知识检索。

当需要跨大量文件搜索调用关系、历史 PR、编码规范或架构文档时，可以引入检索层。选型应从需求出发，而不是为了使用 RAG 而增加 RAG。

当前也没有长期 memory：

- 每个 benchmark case 应独立运行，避免前一个 case 污染后一个 case。
- 同一次 review 的 messages 和 tool results 已经构成短期工作记忆。
- 跨运行需要保留的是结构化实验结果、轨迹和指标，而不是让模型自行记忆过去答案。

未来若用于真实团队，可以考虑保存项目约定、历史误报反馈和开发者偏好，但需要版本管理、作用域隔离、过期策略和隐私控制。

## 13. 可用于面试的项目叙述

可以用以下结构介绍：

### 背景

我搭建了一个可在固定 benchmark 上评估的 Code Review Agent，目标不是只调用一次模型，而是建立可观察、可恢复、可量化优化的 Agent Harness。

### 初始问题

第一版可以输出 review，但误报很多：Sentry case 有 11 条 candidate，其中 9 条是 FP，F1 只有 25%。

### 关键改进

1. 将 system prompt 与不可信 diff 分离，并用 SOP 组织评审过程。
2. 提供安全的 `read_file` 工具和 ReAct 式工具循环。
3. 加入完整轨迹，定位模型停留在计划阶段的问题。
4. 用 tool call 表示中间动作、用 `status=complete` 表示唯一完成状态。
5. 增加结构验证、纠正重试、轮数限制和失败轨迹保存。
6. 用独立 Judge 和 golden comments 衡量 precision、recall、F1。

### 结果

在相同 Sentry fixture 和 Judge 下，candidate 从 11 条减少到 4 条，FP 从 9 条减少到 2 条，recall 保持 40%，F1 从 25% 提升到 44.4%。

### 方法论

- 关键流程约束不能只依赖 Prompt，要由 Harness 执行和验证。
- 先增加可观测性，再调整模型行为。
- 每次实验尽量只改变一个变量。
- 工具调用不是目标，benchmark 上的质量、成本和延迟才是目标。
- 先用开发集迭代，再用保留集验证，防止过拟合。

## 14. 下一步

当前优先级：

1. 用 `tool-or-complete-v1` 跑 Grafana 和 Cal.com。
2. 汇总三个 dev case 的 micro/macro 指标。
3. 分析哪些漏报需要完整文件上下文或新工具。
4. 再决定是否进行 thinking A/B、改进工具描述或加入搜索工具。
5. 最后使用 heldout case 验证，避免在开发过程中查看其 golden comments。

不要同时修改 Prompt、模型、工具和轮数。每次实验记录假设、变量、结果和结论，才能把“调 Agent”变成可复现的工程过程。
