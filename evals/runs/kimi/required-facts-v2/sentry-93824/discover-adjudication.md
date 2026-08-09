# DISCOVER Candidate 人工标注

Run: `kimi/required-facts-v2/sentry-93824`

标注目标：判断 DISCOVER 产生的候选是否值得进入 VERIFY。本文件不以是否命中 golden comment 作为唯一判断标准。

## 结论汇总

| Candidate | 标注 | 简要原因 |
|---:|---|---|
| 0 | drop | 对 diff 的描述错误，旧代码同样是在限制检查之后递增 restart 计数；旧 process 也会被新值覆盖 |
| 1 | drop（但应重写） | `break` 所属循环判断相反；代码位置确实存在另一个真实 shutdown bug |
| 2 | needs_context | 是否存在资源泄漏取决于 `SpansBuffer` 是否拥有需要显式释放的资源，以及该方法的调用路径 |
| 3 | drop | 把全局 backpressure 策略假设成 per-process 策略，缺少设计依据；metric “少计数”也没有明确语义依据 |
| 4 | drop | 候选自己承认 thread 无法 kill，且没有给出未 join 已结束线程会造成的具体故障 |
| 5 | keep | diff 中存在明确、可复现的 `shard` / `shards` metric tag 不一致 |
| 6 | drop | 只是测试覆盖建议，没有证明当前测试遗漏会掩盖一个具体回归或错误行为 |
| 7 | drop | 候选在 claim 内自行推翻问题，明确说明 buffer entry 会被覆盖且调用路径正确 |
| 8 | drop | 与 Candidate 2 重复，并混入缺乏并发证据的 shutdown race 推测 |
| 9 | drop | tag 长度是无边界证据的推测；buffer 与 shards 不一致的担忧被构造代码直接反驳 |

统计：`keep=1`、`needs_context=1`、`drop=8`。

## 逐条说明

### Candidate 0 — drop

该候选包含两个可以直接从 diff 反驳的判断：

1. 它声称旧代码在 restart limit 检查前递增计数，但旧代码同样先检查、后递增。
2. 它声称旧 process 没有从 `self.processes` 移除，但创建新 process 时会执行 `self.processes[process_index] = process`，覆盖旧 entry。

这是典型的 diff-local 事实错误，不应进入仓库验证。

### Candidate 1 — drop，但代码位置需要重新分析

候选声称 deadline 到期时的 `break` 只退出 inner `while`。实际结构是：

```python
for process_index, process in self.processes.items():
    if deadline is not None:
        remaining_time = deadline - time.time()
        if remaining_time <= 0:
            break

    while process.is_alive():
        ...
```

`break` 位于 `for` 的直接循环体中，因此退出的是 `for`，不是后面的 `while`。原 claim 的控制流方向错误，应当删除。

但这里确实存在另一个候选：退出 `for` 会跳过剩余 flusher processes 的 termination，可能使它们在 shutdown 后继续运行。理想的 candidate filtering 不应只做 `drop`，还应允许把“代码位置正确、解释错误”的候选改写为正确 claim。

### Candidate 2 — needs_context

“覆盖旧 buffer 会泄漏 Redis connection”不能只根据 diff 判断，需要读取：

- `SpansBuffer` 是否拥有独占连接或显式 close 生命周期；
- `_create_process_for_shards` 的所有调用点是否都已经 kill/reap 旧 process；
- 被覆盖的 process 是否可能仍然存活。

本次自动上下文已经证明 `SpansBuffer.client` 使用共享/延迟获取的 Redis client，因此原始资源泄漏部分应删除。剩余的 process reaping 判断仍需要结合调用路径验证。

### Candidate 3 — drop

从“任一 process backpressure 会拒绝 submit”无法直接推出 bug。当前 `submit` 面对的是 consumer 输入，consumer-wide backpressure 可能是有意策略。候选没有提供代码或配置证据说明消息能够在 submit 阶段可靠路由到某个 flusher process。

“metric 应按 backpressured process 递增”同样是假设了 metric 的计数语义。如果该 metric 表示被拒绝的 submit 次数，那么每次调用递增一次是合理的。

### Candidate 4 — drop

候选自己确认 `threading.Thread` 没有 `kill()`，当前 `isinstance` 分支避免了错误调用。它没有说明“不 join 一个已经结束的 thread object”会导致什么实际故障，因此属于低价值推测。

### Candidate 5 — keep

同一个 `shard_tag` 在两个相邻 metric 中分别使用：

```python
tags={"shard": shard_tag}
tags={"shards": shard_tag}
```

这是明确、局部、可执行修复的问题，不需要仓库上下文。

### Candidate 6 — drop

新增测试确实只检查 process/shard 的静态分配，但“测试还可以更完整”本身不是 code review issue。候选没有连接到一个具体实现风险，也没有证明缺失的行为断言会让当前错误实现通过测试。

### Candidate 7 — drop

候选先提出旧 buffer 可能残留，随后又确认：

- `self.buffers[process_index] = shard_buffer` 会覆盖旧值；
- singular helper 会委托给 plural helper；
- 初始化与 restart 都会填充 buffer。

它在同一 claim 内完成了反证，应在 DISCOVER 输出前被清除。

### Candidate 8 — drop

旧 process 未 join 的部分与 Candidate 2 属于同一根因，应去重。后半段 shutdown race 没有指出 `_ensure_processes_alive` 与 `join` 能够并发执行的调用证据，也没有给出可复现 interleaving，属于无证据的并发推测。

### Candidate 9 — drop

Kafka partition 数量可能让 tag 太长只是理论可能，没有项目约束或 metrics backend 限制作为证据。

`buffer` 和 `shards` 不一致则被代码直接反驳：`shard_buffer = SpansBuffer(shards)` 使用同一个 `shards` 构造对象，随后又把这两个值传入 process target。

## 对下一步设计的启示

本轮最主要的问题不是缺少更多 candidates，而是 DISCOVER 没有完成三个基础动作：

1. 在输出前删除被自己推翻的候选；
2. 合并相同根因的重复候选；
3. 区分“代码位置可疑”和“当前 claim 正确”。

如果增加 FILTER 阶段，仅支持 `keep/drop` 仍不够。Candidate 1 表明至少需要 `keep/drop/revise`：模型应能保留证据位置，同时修正错误的控制流解释。
