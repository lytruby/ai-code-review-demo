# 完整本地 Benchmark

目标是得到可复现、可诊断的本地分数，判断 agent 改动是否有效。无需 GitHub
组织、发布 PR 评论或上榜。官方离线数据包含 50 个 PR / 5 个项目 / 173 条
golden comments。本项目锁定上游版本
`e616e849755441da38f18bf3adba2c9583b03803`。

## 快速开始

```bash
# 只下载数据目录与 goldens；不调用模型。
uv run python -m evals.benchmark prepare

# 使用当前 .env 的 Kimi K3 review 配置，Judge 默认 GPT-6.1 Sol。
# 第一次先选原有开发集确认链路，避免一次启动 50 个 review。
uv run python -m evals.benchmark run --provider kimi --run-name smoke-v1 \
  --case-id sentry-93824 grafana-79265 calcom-10600

# 全量运行：每个 PR 自动准备/缓存固定 SHA 的输入，review 后评分。
# 这会调用 review 模型和 Judge；案例串行执行，Judge 对比受并发限制。
uv run python -m evals.benchmark run --provider kimi --run-name full-v1

# 中断或失败后原命令重跑：复用 review、去重和成功的逐对判断。
uv run python -m evals.benchmark run --provider kimi --run-name full-v1

# 不调用模型，重新生成报告。
uv run python -m evals.benchmark report --provider kimi --run-name full-v1
```

也可设置 `--provider openai`。Review 的模型/推理配置继续使用原有环境变量；
当前推荐并默认使用 `KIMI_MODEL=kimi-k3`、`LLM_REASONING_EFFORT=low`。
K3 始终开启推理，`KIMI_THINKING` 仅用于旧 K2.x，不作用于 K3。
Judge 始终读取 `OPENAI_API_KEY`、`JUDGE_MODEL`，默认 `gpt-6.1-sol`。
`JUDGE_REASONING_EFFORT=medium`、`JUDGE_MAX_COMPLETION_TOKENS=4096`。
GPT-6 Judge 不发送不兼容的 temperature 参数；显式回选旧 Judge 时保留温度 0。
Judge 推理强度与 review 强度分别配置，也分别记录到运行配置/判断缓存中。
`JUDGE_TIMEOUT` 默认 60 秒、`JUDGE_MAX_RETRIES` 默认 2、
`JUDGE_CONCURRENCY` 默认 5。`.env` 自动加载。
为批量下载 PR 输入配置 `GITHUB_TOKEN` 或 `GH_TOKEN`，否则可能触发 GitHub
匿名访问限额。可提前运行 `prepare --fixtures`；失败信息保存在
`evals/benchmark-data/fixture-errors.json`，重跑复用已成功的数据。
GitHub 缺失 patch 或返回不完整文件列表时会拒绝评估该输入，不静默算分。

如果只想对已有 review 输出重新评分：

```bash
uv run python -m evals.benchmark run --provider kimi --run-name rescore-v1 \
  --case-id sentry-93824 grafana-79265 calcom-10600 \
  --source-run evals/runs/kimi/core-defect-verify-v1
```

这不会运行 reviewer，也不会覆盖旧产物。历史 review 的模型配置标记为未知，
来源目录会保存；不能把新评分当作当前工作区 agent 的一次新运行。

## 只评测 discover（低成本）

只改 discover 时，可以只跑 discover 和去重，把未验证的候选直接交给 Judge
和 golden 匹配，不跑 verify / finalize，也不 checkout 仓库：

```bash
uv run python -m evals.benchmark run --provider kimi --run-name disc-pass5-r1 \
  --discover-only --case-id ...
# 用同一份代码模拟旧配置（每个 pass 3 个候选、上限 8）
uv run python -m evals.benchmark run --provider kimi --run-name disc-pass3-r1 \
  --discover-only --candidates-per-pass 3 --max-candidates 8 --case-id ...
```

报告里的 Recall 表示候选对 golden 的覆盖率，是完整 review 召回的上限；
Precision / F1 没有意义。每个案例约 5–6 次模型调用，完整 review 约 25–40 次。
确认 discover 有提升后，仍需要用完整 review 小规模复核 verify 和 FP。
`--candidates-per-pass` 和 `--max-candidates` 只能调低，不能超过代码中的上限。

## 分数含义

本地 adapter 直接使用固定版本官方源码中的 `evaluate_review` 和 `score_tools`，
以及官方的匹配与去重 prompt；源码和 MIT 许可位于 `evals/vendor/`。

| Profile | 纳入类别 | Golden 数量 |
|---|---|---:|
| Strict | bug / security / concurrency / data / api | 139 |
| Core（建议主指标） | Strict + perf / test_gap / doc_defect | 158 |
| All | Core + style / speculative | 173 |

所有 profile 同时报告 Precision、Recall、F1、F2。主汇总是先累加 TP/FP/FN
再计算的 micro 分数，不是简单平均各 PR 的百分比。
匹配到被 profile 排除类别的评论不奖励也不罚为 FP。
官方语义去重会使匹配评论的重复项免于 FP 惩罚；官方匹配允许一个 candidate
匹配多个 golden，不采用旧 scorer 的一对一最大匹配。
官方原始 per-review precision 和 profile precision 分母不同，因此报告
始终用 profile 聚合函数计算单案例和汇总分数。

这里是“使用官方评分代码的本地评测”，不是官方排行榜分数。区别包括：

- 输入为 agent 的每条 `description + suggestion`，跳过 GitHub 评论抽取。
  不重复加入 summary。保留原始 review 和全部 Judge 判断供检查。
- Judge 使用本地配置的 OpenAI endpoint 与 JSON mode。LLM 判断仍有随机性；
  固定模型与推理配置不能使语义评价成为绝对客观答案。
- Judge 有任一失败时，案例不写成功 evaluation。缓存成功判断，续跑仅补缺项；
  不把 API 错误当作 FP/FN。

## 产物与实验隔离

`evals/benchmark-data/` 保存固定版本 manifest、goldens 和 PR fixtures。
Reviewer 只接收 diff，并只能读取 PR checkout；goldens 不进入 reviewer context。
原有 `dev/heldout` 标签保留，其余标记为 benchmark。查看/用于调优过的全量案例
不能继续宣称是未见测试集；正式调优还应建立新的未见验证集。

`evals/benchmark-runs/<provider>/<run-name>/` 包含：

- `run.json`：数据版本、案例选择、代码哈希、非敏感配置、历史结果来源。
- `<case>/input.json`：新 review 的 base/head SHA 与 diff 校验和。
- `<case>/result.json`：完整 review 和 trajectory。
- `<case>/judgments.json`：去重分组、逐对匹配、理由和置信度；支持续跑。
- `<case>/benchmark-evaluation.json`：成功评分及三个 profile。
- `<case>/status.json` / `judge-errors.json`：失败阶段与详情。
- `summary.json` / `report.md`：全量完成率、分数、按项目分组、各案例 FP/FN，
  以及验证阶段 verdict 数量。阶段计数不是每条漏报的因果归因。

代码、模型配置、数据或案例选择变化时必须使用新 run name，防止断点续跑混合
不同实验。未完成 50/50 时，报告明确写 **completed subset only**；失败案例
不静默消失，也不伪装成完整 benchmark 成绩。空 issues 的正常完成属于有效结果，
其 golden 计为漏报；调用失败与空审查严格分开。

## 怎样用结果优化

1. 冻结一个完整 baseline，再只改变一个因素进行对照，比较相同案例集合。
2. 先看运行完成率，再看 Core Precision/Recall/F1/F2 和各项目分数。
3. 人工抽查 FP：golden 未覆盖的合理问题，也会被算作未匹配。
4. 对重要 FN 查看 trajectory，区分未发现、去重/截断、验证排除、上下文不足；
   不能仅凭最终分数确定损失阶段。
5. 对有希望的改动重复实验，确认收益不是一次生成或 Judge 判断波动。

## 候选验证失败的处理

验证结束提示与解析器遵循同一个 required decision basis。依赖仓库事实的候选
不能在最后一轮改用 diff 绕过证据要求；证据不足时可以返回 `inconclusive`，
且 `issue` 必须为空。

模型发出的正式工具调用只是一项 proposal。`src/tool_gateway.py` 使用工作流
提供的来源、阶段、权限和共享预算检查请求，再验证工具名、参数及路径。
模型参数无法设置这些权限。工作流主动获取上下文也经过同一 gateway。
每次允许或拒绝写入 `tool_gateway_decision`；工具结果依然属于不可信数据。

取证和验证阶段均检查普通文本中的工具调用 JSON，包括 `tool_calls` 包装、
工具名/参数对象及裸参数。只记录协议异常，绝不执行其中内容。
首次异常在剩余预算内仅允许一次纠正，明确告知“没有执行工具，也没有获得
新上下文”，要求原生工具调用或正常结束。纠正回复仍无效则停止该候选的
后续取证，记录 `failure_kind=tool_protocol_error` 和 `verdict=inconclusive`。
取证阶段允许显式 `{"status":"inconclusive","reason":"..."}` 结束；验证
阶段仍必须遵守既有 decision 与证据校验。最后一轮禁止调用工具。
结果摘要明确展示上下文不足；空 issues 不被解释为“未发现问题”。

工具层对直接读取、rg 搜索和 Python 降级搜索执行相同路径策略：禁止
越出仓库、禁止符号链接、禁止 `.env*`、`.git`、常见凭据目录/文件和私钥后缀。
为保守起见，`.env.example` 与仓库内部符号链接也禁止；普通源码及显式指定的
`.github` 文件仍可访问。Gateway 是应用层权限边界，不是操作系统沙箱或秘密
内容检测器；任意源码中的密钥以及原始 PR diff 中的内容不由此文件名策略过滤。

若候选在验证轮数内始终不能返回有效判定，记录 `inconclusive`、
`failure_kind=verification_turn_limit` 和最后一次校验错误，然后继续后续候选。
已有有效问题保留。API 调用失败等运行错误仍按案例失败处理。

报告中的 `cases_with_verification_failures` 和逐案例诊断显示这种降级。
它不改变 benchmark 的标准问题集合或分母：未输出的问题仍可能计为漏报。
`selection_complete` 只表示选定案例全部得到评分，不表示所有候选均验证成功。

本次验证流程改动之后必须使用新 run name。旧 `kimi-k3-first5-v1` 保存原始基线，
不能将新代码的运行结果续写进旧实验。
