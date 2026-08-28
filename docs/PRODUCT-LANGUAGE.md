# Product language

Power Guard reports machine state. Its language should read like an instrument panel, not a pitch deck.

## Message contract

Every primary runtime message must contain at least two of these:

- **state** — what is true now;
- **evidence** — the count, process, task result, input time, or timer that proves it;
- **next event** — what advances or cancels the flow;
- **action** — what the user can do.

A sentence that contains none of them should be deleted.

## Terms

| Internal term | Primary UI | Technical logs/docs only |
|---|---|---|
| armed / campaign | 本次自动睡眠已启用 | armed, campaign, generation |
| gate | 执行条件 / specific condition name | safety gate |
| terminal | 任务结果 | terminal outcome |
| quiescence | 迟到任务检查 | quiescence |
| claim | 正在做最后检查 | action claim |
| Desktop heartbeat | 控制面板在线 / 离线 | heartbeat lease |
| ineligible | 结果不符合当前规则 | ineligible |
| fail-awake | 电脑保持唤醒 | fail-awake |

## Runtime patterns

### Work remains

```text
Hermes 还有 3 项工作
会话 1 · 后台 1 · 子代理 1
下一步：全部结束后开始 30 秒迟到任务检查。
```

### User input

```text
刚刚检测到键鼠操作
再空闲 4分12秒后开始迟到任务检查。
```

### Unknown evidence

```text
读不到后台进程状态
无法证明工作已经结束，因此电脑保持唤醒。
下一步：恢复 Hermes 后台连接；不会自动睡眠。
```

### Countdown

```text
01:12 后睡眠
键鼠输入、新任务或控制面板离线都会取消。
[取消睡眠]
```

### Error

```text
Windows 没有接受睡眠请求
Power Guard 不会自动重试。
[查看记录]
```

## Button grammar

Use verb + object + scope:

- `启用本次自动睡眠`
- `取消本次自动睡眠`
- `取消睡眠`
- `延后 15 分钟`
- `保存规则`
- `运行 8 秒模拟`
- `查看记录`

Avoid:

- `确认并武装`
- `解除启用`
- `执行最终动作`
- `检查并确认模拟模式`
- `安全测试` when the actual action is specifically a simulation

## Presets

Preset names describe policy, not quality:

- `平衡` — complete or blocked; 5-minute idle; 30-second check; 90-second countdown.
- `仅成功` — any failed, interrupted, blocked, or unknown result keeps the computer awake.
- `夜间节能` — longer check/countdown plus display and power-plan adjustments.

Do not call one preset “safe” or “smart”; every supported preset must already be safe.

## Repository copy

The root README should answer:

1. What does it do?
2. How do I install it?
3. What evidence does it use?
4. When will it refuse?
5. What is outside its scope?

Move implementation detail into linked documents. Do not use test counts, file counts, “robust”, “powerful”, or “mature” as the product proposition. Tests support a claim; they are not the claim.
