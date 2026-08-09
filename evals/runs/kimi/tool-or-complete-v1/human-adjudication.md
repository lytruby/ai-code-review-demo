# Human Adjudication of Unmatched Candidates

本报告人工复核 `tool-or-complete-v1` 在三个 dev cases 中被 Judge 标记为 false positive 的 8 条 candidate。判断依据是锁定 fixture 的实际 diff，而不是只看模型描述。

分类标准：

- **A — 明确误报**：代码已有保护、结论不成立，或技术机制描述错误。
- **B — 合理问题但 golden 未覆盖**：从 diff 可以指出具体风险，值得 review，但 benchmark golden 没有包含。
- **C — 低价值推测**：理论上可能，但缺乏代码证据、影响很弱，或只基于未来可能的重构。

## 汇总

| Case | A | B | C | 合计 |
|---|---:|---:|---:|---:|
| sentry-93824 | 0 | 2 | 0 | 2 |
| grafana-79265 | 1 | 0 | 0 | 1 |
| calcom-10600 | 1 | 1 | 3 | 5 |
| **合计** | **2** | **3** | **3** | **8** |

因此，Judge 统计的 8 条 false positives 中：

- 2 条可以明确判为错误评论。
- 3 条是有代码依据、但 golden 未覆盖的合理问题。
- 3 条属于证据不足或价值较低的推测。

## sentry-93824

### Candidate 0 — B：合理问题但 golden 未覆盖

> `process_restarts` 在阈值检查之后才递增，可能导致多允许一次重启。

实际代码先执行：

```python
if self.process_restarts[process_index] > MAX_PROCESS_RESTARTS:
    raise RuntimeError(...)
self.process_restarts[process_index] += 1
```

当上限为 5 时，计数为 5 仍不会抛错，而是递增到 6 并再次重启；下一次异常才会抛错。如果常量语义是“最多重启 5 次”，这里确实存在 off-by-one。Golden 没覆盖不代表问题不存在。

模型描述基本正确，但更准确的表述应该区分“崩溃次数”和“已执行的重启次数”。

### Candidate 2 — B：合理问题但 golden 未覆盖

> 崩溃或挂起的旧进程在被新进程替换前没有 `join()`/回收。

实际代码对旧进程执行可选的 `kill()` 后，立即调用 `_create_process_for_shards()`，并用新对象覆盖 `self.processes[process_index]`。没有看到对旧进程执行 `join()`。

对已经退出的子进程，不回收可能留下 zombie；对挂起进程，如果 kill 没有成功，覆盖引用可能使旧进程继续运行。该风险具体且与进程生命周期有关，因此判为 B。

需要注意：模型所说的 “calling kill could fail” 不是这里最清楚的核心问题；更准确的核心是 kill 后没有等待和回收。

## grafana-79265

### Candidate 3 — A：发现了正确位置，但技术结论错误

> 把 query 放进 `[]interface{}` 后执行 `dbSession.Exec(args...)` 很脆弱，依赖 driver 识别第一个参数。

这不是“driver 能否正确处理”的运行时问题。Go 会在编译阶段检查方法签名：`Exec` 的第一个参数要求 `string`，而 `args...` 展开的是 `[]interface{}`，因此调用无法编译，driver 根本不会收到请求。

模型建议的修复：

```go
dbSession.Exec(query, args...)
```

是正确的，但问题机制被描述错了。按照分类中“技术结论错误”的定义，判为 A。它也说明 Agent 已定位异常代码，却缺少足够精确的 Go 类型判断。

## calcom-10600

### Candidate 0 — C：有安全直觉，但风险论证不足

> 5 个随机字节只有 40 bit，而且 hex 字符集会进一步降低熵。

`crypto.randomBytes(5)` 确实提供 40 bit 随机输入，但 hex 只是编码方式，不会在 40 bit 基础上再次降低熵。是否必须达到 80 bit 还取决于在线尝试次数、速率限制、账号锁定和备份码数量；模型没有检查这些条件。

因此，这条可以作为安全设计讨论，但当前描述包含技术错误，且没有证明 40 bit 在该系统威胁模型下可被现实利用。主要价值属于证据不足的安全推测，判为 C。

### Candidate 1 — A：代码已有保护，且 `null` 不会导致重复使用

> 登录流程没有验证 `CALENDSO_ENCRYPTION_KEY`；把已使用 code 设置为 `null` 没有真正删除。

实际 diff 在进入 backup-code 登录分支后立即检查：

```ts
if (!process.env.CALENDSO_ENCRYPTION_KEY) {
  throw new Error(ErrorCode.InternalServerError);
}
```

所以“没有验证 encryption key”明确错误。

将匹配位置设置为 `null` 后重新加密，虽然不如删除元素整洁，但后续 `indexOf(userCode)` 不会再匹配该位置，仍能阻止顺序执行下的重复使用。真正的高风险问题是两个并发请求可能在任一请求写回前同时验证成功，这是 golden 中的并发问题，但模型没有指出。

### Candidate 2 — B：Object URL 清理问题合理，但混入了弱问题

> `URL.createObjectURL` 创建的 URL 在组件卸载时可能没有回收。

diff 只在创建新 URL 前有条件地调用 `URL.revokeObjectURL`，没有看到组件卸载 cleanup。关闭 modal 或完成流程后，当前 URL 可能保留到文档卸载，因此资源泄漏风险有直接代码依据，判为 B。

同一 candidate 中“可能下载空文件”的部分较弱：下载按钮只在成功获取 backup codes 并进入展示步骤后出现。Agent 将一个合理问题和一个弱推测捆绑在同一条评论里，降低了评论质量。

### Candidate 4 — C：代码明确说明当前行为正确，只讨论未来重构

> disable 流程没有单独删除已使用的 backup code，未来重构时可能出问题。

该 endpoint 的目标是关闭 2FA，最终更新明确执行：

```ts
backupCodes: null
```

diff 中也有注释说明所有 stored backup codes 会在结尾删除。模型自己承认当前行为 “functionally okay”，剩余担忧只依赖未来可能发生的功能变化，不应作为当前 PR issue 报告，判为 C。

### Candidate 5 — C：未经验证的导入猜测

> `showToast` 可能不是 `@calcom/ui` 的公开导出。

模型没有读取 UI package 的导出文件，也没有构建错误证据，仅根据新增 import 推测“可能不存在”。这种问题应先通过代码搜索、`read_file` 或类型检查验证；在没有证据时不应报告，判为 C。

## 对 Agent 优化的启示

这次人工复核不支持简单地把所有 unmatched candidate 都当成模型误报：8 条中有 3 条可能是 golden 未覆盖的合理问题。

当前更具体的质量问题是：

1. **事实验证不足**：Cal.com candidate 1 没看到同一 diff 中已经存在的 encryption-key check。
2. **技术机制不精确**：Grafana candidate 3 找到正确代码，却把编译错误描述成 driver 行为。
3. **混合多个问题**：合理问题和弱推测被放进同一个 candidate。
4. **未来假设被当作当前缺陷**：例如 disable 流程未来可能重构。
5. **可验证猜测没有使用工具**：例如 `showToast` 是否导出。

后续若增加验证阶段，重点不应是要求模型证明所有运行时细节，而应该执行几个轻量检查：

- 在提交 candidate 前重新检查同一 diff 是否已经存在保护。
- 一条 candidate 只描述一个底层问题。
- 对“可能不存在/可能未导出”这类可验证主张，先读取或搜索相关定义。
- 区分当前 PR 的实际缺陷与未来维护建议。
- 对语言类型错误，明确判断发生在编译期还是运行期。

## TP、FP、FN 与上下文需求分析

### 哪些 TP 只看 diff 就能发现

当前三个 dev cases 共命中 7 条 golden comments。这 7 条问题基本都可以仅凭 fixture diff 发现：

| Case | 已命中问题 | 是否需要完整文件 |
|---|---|---|
| Sentry | `shard` / `shards` metrics tag 不一致 | 否 |
| Sentry | deadline 到期后跳过剩余进程的 terminate 流程 | 否 |
| Grafana | count 与 insert/update 之间的并发竞态 | 否 |
| Grafana | 同步 `TagDevice` 错误阻断匿名认证 | 否 |
| Grafana | `rowsAffected == 0` 被错误映射为 device limit | 否 |
| Grafana | `device.UpdatedAt` 时间窗口语义不一致 | 否 |
| Cal.com | `BackupCode.tsx` 仍导出名为 `TwoFactor` 的组件 | 否 |

当前 TP 不依赖 `read_file`，因此 Agent 没有使用工具仍然可以得到部分有效结果。

### 哪些 FN 必须读取完整文件

当前共有 7 条 FN，严格来说没有一条必须读取完整文件才能发现：

| Case | 漏报 | 更可能缺少的能力 |
|---|---|---|
| Sentry | fixed sleep 可能导致测试 flaky | 测试稳定性意识 |
| Sentry | `SpawnProcess` 不是预期的 `multiprocessing.Process` 子类 | Python multiprocessing 类型知识 |
| Sentry | `time.sleep` 已在测试中被 monkeypatch | 跨 patch 关联和注意力 |
| Grafana | `Exec(args...)` 无法满足 Go 方法签名 | Go 编译期类型判断 |
| Cal.com | disable endpoint 的错误文案仍写 login | 基础一致性检查 |
| Cal.com | backup code 比较大小写敏感 | 输入规范化和边界分析 |
| Cal.com | 并发请求可重复消费一次性 backup code | 并发和原子性分析 |

这些漏报主要反映注意力、语言知识以及并发/安全语义推理不足，而不是仓库上下文不足。读取完整文件可能辅助确认，但不是发现它们的必要条件。

### Agent 是否在应该调用工具时没有调用

有，但主要影响 candidate 的事实验证，而不是上述 FN：

- 声称 `showToast` 可能没有导出前，应搜索或读取 UI package 的 exports。
- 声称 Object URL 没有 cleanup 前，应读取完整组件确认其他位置是否存在清理逻辑。
- 声称 Sentry 旧进程没有回收前，可以读取完整类确认是否有其他 cleanup 路径。
- 判断 40-bit backup code 是否构成实际安全风险前，需要了解速率限制和认证保护。

当前 `read_file(path)` 要求模型预先知道准确文件路径。验证 `showToast` 这类符号时，更匹配的能力可能是受限的 `search_code(query)`，而不是强制调用现有工具。

因此当前判断是：

```text
Agent 没有因为不读取完整文件而漏掉大部分 golden；
但它在报告“缺少某项保护或定义”时，没有主动验证可验证的事实。
```

## 跨仓库结果

`tool-or-complete-v1` 在三个 dev cases 上的结果：

| Case | TP | FP | FN | Precision | Recall | F1 |
|---|---:|---:|---:|---:|---:|---:|
| sentry-93824 | 2 | 2 | 3 | 50.0% | 40.0% | 44.4% |
| grafana-79265 | 4 | 1 | 1 | 80.0% | 80.0% | 80.0% |
| calcom-10600 | 1 | 5 | 3 | 16.7% | 25.0% | 20.0% |

合并后的 micro 指标：

```text
TP = 7
FP = 8
FN = 7
Precision = 46.7%
Recall = 50.0%
Micro F1 = 48.3%
```

表现具有明显的仓库差异：Grafana 很好，Sentry 中等，Cal.com 较差。当前版本尚不能说明 Agent 在所有语言和问题类型上表现稳定。

旧版本只在 Sentry 上有评分，因此只能确认 Sentry 的一次运行中 FP 从 9 降到 2、F1 从 25% 提升到 44.4%。缺少 Grafana 和 Cal.com 的旧版本结果，不能证明 Prompt 在所有仓库都减少了误报；单次 LLM 输出还可能受到随机性影响。

## 新基线定义

从下一轮开发开始，将当前版本定义为新的工程基线：

```text
Baseline name: tool-or-complete-v1
Provider: kimi
Dev cases: sentry-93824, grafana-79265, calcom-10600
Micro Precision: 46.7%
Micro Recall: 50.0%
Micro F1: 48.3%
```

该基线的意义不是证明它优于所有历史版本，而是提供一个完整覆盖三个 dev cases、包含 result、trajectory、Judge evaluation 和人工抽查的可复现实验起点。

后续版本应保持以下条件不变：

- 使用相同的三个 fixture 和锁定 SHA。
- Review provider 保持 Kimi。
- Judge 模型和匹配逻辑保持一致。
- 使用新的 `run-name`，不覆盖基线。
- 记录 Prompt、工具、workflow 或 thinking 中具体改变的变量。
- 同时比较自动指标和少量人工复核结果。

下一轮优化应以解决一个明确问题为目标，例如“减少未经验证的缺失性主张”，而不是同时调整 Prompt、增加工具、打开 thinking 和修改轮数。
