# AI Code Review Agent

这是一个用于学习和评测 Code Review Agent 的小型项目。当前使用固定的 GitHub PR 作为测试 case，通过 Kimi 生成 review，并与开发集的 golden comments 对比。

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
KIMI_THINKING=disabled
LLM_MAX_COMPLETION_TOKENS=2048
LLM_TIMEOUT=120
LLM_MAX_RETRIES=0
```

程序使用 `python-dotenv` 自动读取 `.env`，不需要执行 `source .env`。

`.env` 已被 `.gitignore` 排除，不应提交真实 API Key。这里需要使用 Kimi 开放平台的 API Key，不是 Kimi Code 或 Kimi 会员的 Key。

当前 baseline 配置关闭思考模式。以后可以设置：

```env
KIMI_THINKING=enabled
```

使用相同 case 比较思考模式对 review 质量、速度和成本的影响。

## 3. 测试 Kimi 模型连接

先运行最小连通性测试，确认 API Key、端点和 `kimi-k2.6` 模型正常：

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

Eval runner 会：

1. 读取固定 fixture。
2. Checkout PR 的固定 `head_sha`。
3. 把完整代码目录交给 agent 的 `read_file` 工具。
4. 调用 Kimi 生成 review。
5. 保存结构化结果和可观察轨迹（模型响应、工具调用及工具结果）。

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

Scorer 按官方 benchmark 的规则，将每个 candidate 与每个 golden comment 做语义匹配，再计算 TP、FP、FN、precision、recall 和 F1。第一版暂不执行 candidate 去重。

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
