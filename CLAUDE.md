# CLAUDE.md

学习和评测 Code Review Agent 的小项目。Agent 在 `src/reviewer.py`，走
OpenAI 兼容接口，默认 Kimi K3；评测在 `evals/`。说明文档用中文。

## 常用命令

```bash
uv run python -m pytest -q                      # 全部测试，不调用模型
uv run python -m evals.benchmark run --provider kimi \
  --run-name <新名字> --case-id <case> ...      # 调用 review 模型和 Judge，会花钱
uv run python -m evals.benchmark report --provider kimi --run-name <名字>
```

## 模型请求与成本

- 提示词按“不变的在前、变化的在后”排列，保证跨请求共享缓存前缀：
  system prompt → diff 等整次 review 共享的内容 → 候选、pass、工具结果等
  每次不同的内容。新增 stage 或调整消息结构时都按这个顺序。
- 不要往共享前缀里放时间戳、随机 ID、计数器等每次都变的值。
- 带工具的轮次不传 `response_format: json_object`：Kimi 在 JSON 模式下会把
  工具调用写成普通文本（见 `evals/benchmark-runs/kimi/json-mode-ab-v1`）。
  无工具的轮次保留 JSON 模式。
- 格式纠正重试同样要花一整轮 token。反馈要说清楚下一步能做什么，预算用完
  就直接要求给出结论，不要让模型空转。
- 改完后用报告里的 “Review tokens” 一行（缓存命中 / 未命中 / 输出）对比
  成本。

## 实验规则

- 每次运行都用新的 `--run-name`，不覆盖旧结果；`evals/benchmark-runs/` 不入库。
- 同一份代码跑两次，sentry-67876 的 F1 就差了 13 个百分点。判断改动好坏至少
  要多案例、每个案例跑多次，单次结果只能当参考。
- 不要读取或下载 held-out 案例的 golden，也不要用它调优。
- 每次 agent 设计变更、实验结论都追加到 `docs/agent-changelog.md`，写清楚
  运行名、改了什么、数字和不能下的结论。
