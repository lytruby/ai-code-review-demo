# AI Code Review Agent

这是一个用于学习和评测 Code Review Agent 的小型项目。当前使用固定的 GitHub PR 作为测试 case，通过 Kimi 或 OpenAI 模型生成 review，并与开发集的 golden comments 对比。

Agent 的逐轮设计变更、失败实验和评估结论记录在 [Agent Learning Changelog](docs/agent-changelog.md)。

完整的 50 PR 本地 benchmark、官方评分口径、断点续跑和诊断报告见
[完整 Benchmark 使用说明](docs/benchmark.md)。这条新入口直接评估结构化 issues，
不需要发布 GitHub 评论或申请排行榜。旧 `evals.score` 保留一对一匹配的历史口径，
不能和新入口的 upstream profile 分数混合比较。

## 1. 准备虚拟环境

项目使用 [uv](https://docs.astral.sh/uv/) 管理本地虚拟环境和依赖。

```bash
uv venv
uv pip install -r requirements.txt
```

之后运行项目命令时使用 `uv run`，不需要手动激活 `.venv`：

```bash
uv run python --version
```

## 2. 配置 Kimi

在仓库根目录的 `.env` 中填写 Kimi 开放平台 API Key：

```env
MOONSHOT_API_KEY=your_api_key_here
KIMI_MODEL=kimi-k3
KIMI_THINKING=disabled
LLM_MAX_COMPLETION_TOKENS=2048
LLM_DISCOVER_MAX_COMPLETION_TOKENS=4096
LLM_TIMEOUT=120
LLM_MAX_RETRIES=0
LLM_TRANSIENT_RETRIES=2
LLM_RETRY_BACKOFF_SECONDS=1
LLM_PROGRESS_INTERVAL=30
LLM_REASONING_EFFORT=low
```

程序使用 `python-dotenv` 自动读取 `.env`，不需要执行 `source .env`。

`LLM_MAX_RETRIES=0` 关闭 SDK 内部的隐藏重试；Agent workflow 会对 timeout、connection error、HTTP 5xx 和 rate limit 显式重试最多 2 次，并将每次失败记录到 trace。退避时间默认依次为 1 秒、2 秒。

每次模型请求会立即打印 stage、turn、attempt 和单次 timeout。请求超过
`LLM_PROGRESS_INTERVAL`（默认 30 秒）后会持续打印等待时间；timeout 或其他临时
错误发生时会打印错误及下一次重试。设置为 `0` 可以关闭周期性 heartbeat，但保留
请求开始、完成和失败日志。注意 `LLM_TIMEOUT=300` 且显式重试 2 次时，单个 workflow
turn 的最坏等待时间接近 15 分钟，而不是总共 5 分钟。

`.env` 已被 `.gitignore` 排除，不应提交真实 API Key。这里需要使用 Kimi 开放平台的 API Key，不是 Kimi Code 或 Kimi 会员的 Key。

当前默认使用 Kimi K3（`low` 推理强度）。若显式使用旧 K2.x 模型，可以设置：

```env
KIMI_THINKING=enabled
```

使用相同 case 比较思考模式对 review 质量、速度和成本的影响。

`KIMI_THINKING` 只用于 Kimi K2.x。Kimi K3 始终开启推理，程序通过顶层
`reasoning_effort` 参数控制强度；环境变量 `LLM_REASONING_EFFORT` 支持
`low`、`high`、`max`，默认使用 `low` 以降低延迟和 token 消耗。

### 配置 OpenAI review 模型

同一个 `.env` 可以同时保存两个 provider 的 key：

```env
MOONSHOT_API_KEY=your_moonshot_key
OPENAI_API_KEY=your_openai_key
OPENAI_MODEL=gpt-5.6
```

Runner 会把 `--provider` 显式传给 Reviewer：`kimi` 只读取
`MOONSHOT_API_KEY` 并使用 Moonshot endpoint，`openai` 只读取
`OPENAI_API_KEY` 并使用 OpenAI 默认 endpoint。即使两个 key 同时存在，也不会再
因环境变量优先级误用 Kimi。

Kimi 使用 Chat Completions API；OpenAI GPT-5.6 使用 Responses API，因为
GPT-5.6 的 Chat Completions endpoint 不支持 reasoning effort 与 function tools
同时使用。两者仍共享相同的 Agent prompt、工具定义、workflow 状态、预算和输出
校验。

OpenAI 当前默认 review model 是 `gpt-5.6`，默认 reasoning effort 是
`medium`。也可以在单次命令中用 `LLM_MODEL` 和 `LLM_REASONING_EFFORT` 覆盖。
GPT-5.6 支持 `none`、`low`、`medium`、`high`、`xhigh` 和 `max`。

## 3. 测试 Kimi 模型连接

先运行最小连通性测试，确认 API Key、端点和配置的模型（默认 `kimi-k3`）正常：

```bash
uv run python tests/manual/check_kimi.py
```

成功输出类似：

```text
authentication: OK
completion: KIMI_OK
PASS: Kimi connection is working
```

这个测试只发送一个很短的请求，不会运行完整 Code Review。

OpenAI GPT-5.6 使用 Responses API。首次运行 benchmark 前，先用两轮小请求验证
reasoning、JSON mode、strict function schema 和 tool-result continuation：

```bash
LLM_MODEL=gpt-5.6 LLM_REASONING_EFFORT=medium \
uv run python tests/manual/check_openai.py
```

成功时会输出 `PASS: OpenAI Responses connection is working`。这个检查会实际读取
当前仓库的 `README.md`，但不会运行 fixture checkout、DISCOVER 或评分。

## 4. 测试 case

当前有 5 个固定 case：

- `evals/cases/dev.json`：3 个开发集 case，可以查看 golden comments。
- `evals/cases/heldout.json`：2 个保留集 case，不应在开发期间查看答案。

下载并锁定 case 的 PR SHA 和 diff：

```bash
uv run python -m evals.fetch_fixtures
```

结果保存到：

```text
evals/fixtures/<case-id>/
├── metadata.json
└── changes.json
```

- `metadata.json` 保存 repository、PR number、`base_sha` 和 `head_sha`。
- `changes.json` 保存 agent review 时使用的固定 diff。

默认会跳过已经存在的 fixture。如需重新获取：

```bash
uv run python -m evals.fetch_fixtures --force
```

## 5. 运行 Code Review

先运行一个 case：

```bash
uv run python -m evals.run_eval \
  --case-id sentry-93824 \
  --provider kimi \
  --run-name sop-v1
```

使用相同 Agent 配置运行 OpenAI 模型对照：

```bash
LLM_MODEL=gpt-5.6 LLM_REASONING_EFFORT=medium \
uv run python -m evals.run_suite \
  --case-id sentry-93824 grafana-79265 calcom-10600 \
  --provider openai \
  --run-name gpt-5-6-medium-v1
```

Review 和 Judge 都使用 `OPENAI_API_KEY`，但它们仍是两次独立调用：review model
由 `LLM_MODEL` 控制，judge model 由 `JUDGE_MODEL` 控制。结果保存在
`evals/runs/openai/gpt-5-6-medium-v1/`，不会与 Kimi 结果混合。

Eval runner 会：

1. 读取固定 fixture。
2. Checkout PR 的固定 `head_sha`。
3. 把完整代码目录作为只读环境交给 agent，通过 `search_code` 定位代码，再用 `read_file` 读取局部上下文。
4. 由代码依次执行 DISCOVER、VERIFY、FINALIZE 三个阶段。
5. VERIFY 为每个 candidate 建立独立模型调用，按需调用 `search_code` 和 `read_file`，逐条保留、修改或删除候选问题。
6. 调用 Kimi 生成最终 review 摘要，由代码组装已验证 issues。
7. 保存结构化结果和可观察轨迹（阶段、模型响应、finish reason、token usage、工具调用及工具结果）。

固定 SHA 的 repository checkout 会缓存到 `evals/repositories/`（已加入 `.gitignore`）。第一次运行需要从 GitHub fetch；成功后相同仓库和 SHA 会直接离线复用，避免每次评测重新下载。首次 fetch 遇到临时 DNS/网络错误时会自动重试 2 次。

三个阶段的状态由 Python 维护，而不是依赖模型记忆：

```text
DISCOVER_CORRECTNESS runtime/API candidates (max 3)
＋ DISCOVER_STATE state/lifecycle/concurrency candidates (max 3)
＋ DISCOVER_CONSISTENCY behavioral-contract candidates (max 3)
＋ DISCOVER_TESTS concrete test defects (max 3)
→ exact deduplication
→ semantic grouping and evidence/fact merge
→ truncate after deduplication (max 8)
→ ACQUIRE_CONTEXT resolve required_facts within a budget
→ VERIFY keep / revise / rejected
→ unresolved facts become inconclusive
→ FINALIZE summary
→ COMPLETE
```

最终阶段不能新增或改写 issues；正式 issues 只来自 VERIFY 的结构化结果。
DISCOVER 由四个独立模型调用组成。Correctness pass 检查类型/API、空值、控制流和边界条件；state pass 检查状态转换、生命周期、事务与并发；consistency pass 检查公开命名、用户文案、默认值、序列化与跨文件行为契约；tests pass 只检查会造成假通过或不稳定失败的 mock、sleep、assertion 和 setup/cleanup。代码先按四个 pass 的证据强度顺序交错收集全部候选并做精确 claim 去重，再进入独立 DEDUPLICATE 阶段。只有 root cause、主要 observable impact 和 remediation 都相同，最终 review comment 可以互换的 candidates 才能合并；同一调用链中的上游原因和下游影响保持独立。代码校验分组必须完整、不重叠且 representative 属于组内；重复组保留最清晰的 representative claim，并合并各候选的 evidence 与最多 2 条 required facts。每组还必须给出 1–5 priority，代码按“priority 降序、原始位置升序”稳定排序后再截断到最多 8 条。模型输出无效时重试，预算耗尽则安全回退到精确去重结果，并在 trajectory 中记录 fallback。
每个 candidate 只能描述一个 claim，并携带结构化 evidence 引用。每条引用使用 `file` 声明证据所在的 diff 文件，使用 `side: before | after` 标明变更侧，并包含一个连续源码片段；跨文件因果链拆成多条带各自 `file` 的 evidence。Candidate 顶层 `file` 表示最终评论位置，并且必须至少出现在一条 evidence 中。非连续位置或 before/after transition 也必须拆成多条引用。代码会分别重建各文件变更前后的源码，验证每条 evidence 的 side、内容和文件归属。旧轨迹中缺少 `evidence.file` 的引用仍兼容为 candidate 顶层文件。

DISCOVER 还必须列出验证该 claim 所需、但 diff 中不可见的 repository facts：

```json
{
  "required_facts": [
    {
      "question": "SpansBuffer 是否保存实例级状态？",
      "source": "repository",
      "path": "src/sentry/spans/buffer.py",
      "query": "class SpansBuffer"
    }
  ]
}
```

每条 candidate 最多 2 个 required facts。每个 fact 使用结构化 locator：`path + query` 会在指定路径内搜索并读取首个匹配位置，只有 `path` 时直接读取文件，只有 `query` 时执行全仓库搜索并读取首个匹配位置。一次成功的搜索不等于 fact 已解决；只有成功读取相关源码才算 resolved。初始定位失败时，ACQUIRE_CONTEXT 允许模型在每条 fact 的独立预算内调整查询或读取位置。预算耗尽仍未读取到源码时，该 candidate 记为 `inconclusive`，不会进入 VERIFY。Required facts 非空时，VERIFY 必须使用 repository basis；空列表时必须使用 diff basis。

Repository 工具分工：

```text
search_code(query, path?)
  → 返回最多 20 个文件路径、行号和匹配行
read_file(path, line?, context_lines?)
  → 小文件可完整读取；大文件按中心行读取局部窗口
```

`read_file` 的 `context_lines` 默认 50、最大 100。大文件未指定 `line` 时，工具会返回总行数和再次分段读取的提示，而不是直接丢失全部上下文。两个工具都只能访问固定 checkout 的 repository 内部。

VERIFY 的每条 decision 必须声明判断依据：

```json
{"basis": "diff | repository"}
```

VERIFY 的否定结论记为 `rejected`，表示已有证据证明 candidate 不成立；`inconclusive` 由 workflow 生成，表示 required facts 在预算内没有查清。这两个结果都不会成为最终 issue，但会在 trajectory 中分开记录，便于区分验证判断问题与 retrieval/budget 问题。`basis=repository` 只会在 required facts 全部 resolved 后进入 VERIFY；`basis=diff` 表示只能依据 supplied diff，不应声称仓库中的定义、调用点、继承或运行时状态。

`keep` 和 `revise` 还必须返回结构化 `supporting_evidence`。Diff evidence 会再次按 `file + side + text` 校验；repository evidence 的文件和原文必须真实出现在该 candidate 成功的 `read_file` 结果中。模型不能仅凭一次搜索、一次无关文件读取，或自身常识把新的 repository fact 写进最终 issue。Supporting evidence 会保留在 candidate trajectory 中，供人工检查最终描述中的每项行为断言是否都有证据。

如果评审失败，同一目录下会保存 `error.json`，其中包含错误信息和失败前的可观察轨迹。

结果路径：

```text
evals/runs/kimi/sop-v1/sentry-93824/result.json
```

`provider` 用于区分 review 模型供应商，`run-name` 用于区分 agent 版本或实验。同一 provider、run name 和 case 的结果默认不会被覆盖。

可用的 case id：

```text
sentry-93824
grafana-79265
calcom-10600
discourse-benchmark-2
keycloak-32918
```

## 6. 获取 Golden Comments

只下载开发集的 golden comments：

```bash
uv run python -m evals.fetch_golden
```

结果保存到：

```text
evals/golden/dev/
├── sentry-93824.json
├── grafana-79265.json
└── calcom-10600.json
```

默认跳过已存在文件。如需从官方 benchmark 重新下载：

```bash
uv run python -m evals.fetch_golden --force
```

不要下载或复制 `heldout.json` 中两个 case 的 golden comments，否则它们将失去保留集的意义。

Golden comments 来源于 [withmartian/code-review-benchmark](https://github.com/withmartian/code-review-benchmark/tree/main/offline/golden_comments)。

## 7. 使用 OpenAI Judge 评分

在 `.env` 中配置独立的 OpenAI Judge：

```env
OPENAI_API_KEY=your_openai_api_key
JUDGE_MODEL=gpt-5.2
```

对一个已经生成 review 的开发集 case 评分：

```bash
uv run python -m evals.score \
  --case-id sentry-93824 \
  --provider kimi \
  --run-name sop-v1
```

评分结果保存到：

```text
evals/runs/kimi/sop-v1/sentry-93824/evaluation.json
```

Scorer 先让 Judge 对每个 candidate 与每个 golden comment 做语义判断，再执行一对一最大匹配：优先最大化 TP 数量，TP 数相同时最大化总 confidence。未进入最终匹配的 candidate 计为 FP；如果它也匹配某个已占用的 golden，则标记为 `duplicate_match`，不会在评分前静默去重。Evaluation 同时保存全部 `pairwise_judgments`，并强制校验 `TP + FP = total_candidates`、`TP + FN = total_golden`。

如果要连续运行并评分一个或多个 case，可以使用 suite 命令：

```bash
uv run python -m evals.run_suite \
  --case-id sentry-93824 grafana-79265 calcom-10600 \
  --provider kimi \
  --run-name verified-claims-v1
```

每个 case 会先运行 review，再执行评分；任何一步失败时 suite 会停止，避免继续产生无效调用费用。

## 8. 运行自动测试

运行全部单元测试：

```bash
uv run python -m pytest -q
```

手动 Kimi 连通性测试不属于自动测试套件，需要单独运行：

```bash
uv run python tests/manual/check_kimi.py
```

## 当前评测流程

```text
选择固定 case
→ 获取 fixture
→ Checkout 固定 head_sha
→ Agent review
→ 保存 result.json
→ 获取开发集 golden comments
→ 对比 agent 结果与真实问题
```

下一阶段将增加自动匹配和 precision/recall 计算。目前先保留每次 review 的原始结果，作为 agent 后续改进的 baseline。
