# Runtime state matrix

This document maps internal lifecycle states to user-visible language. The Desktop UI should not expose the internal names.

| Internal state | Primary message | Evidence | Next event / action |
|---|---|---|---|
| `disarmed` | 自动睡眠未启用 | 当前不会执行任何电源动作 | 保存规则；启用本次自动睡眠 |
| `armed_waiting_for_task` | 等待启用后的第一个任务 | 还没有本批次任务 | 开始一个新的 Hermes 任务 |
| `running` | Hermes 还有 N 项工作 | 会话、后台、子代理、其他窗口计数 | 全部结束后自动继续 |
| `waiting_for_terminal_signal` | 任务结束信号不完整 | 没有可确认的结果 | 保持唤醒，等待明确结果 |
| `terminal_not_eligible` | N 个任务结果不符合规则 | 首个不合格结果和原因 | 查看记录或修改规则后重新启用 |
| `waiting_for_desktop_ui` | 控制面板已离线 | Desktop lease 已过期 | 恢复控制面板连接 |
| `waiting_for_protected_process` | 受保护程序仍在运行 | 进程名 | 关闭程序或修改规则 |
| `waiting_for_idle_check` | 读不到键鼠空闲时间 | Windows 输入传感器不可用 | 保持唤醒，等待恢复 |
| `waiting_for_user_idle` | 刚刚检测到键鼠操作 | 距门槛还差的时间 | 停止操作后自动继续 |
| `armed_waiting_for_quiet` | 条件发生变化 | 旧倒计时已取消 | 重新检查全部条件 |
| `quiescence` | 正在检查迟到任务 | 剩余确认时间 | 无新活动后进入倒计时 |
| `snoozed` | 本次自动睡眠已延后 | 延后剩余时间 | 等待或取消延后 |
| `countdown` | NN 秒后睡眠 | 可见计时器 | 键鼠/新任务/离线自动取消；可手动取消 |
| `executing` | 正在向 Windows 请求睡眠 | 最终检查进行中 | 无需操作 |
| `action_requested` | 已向 Windows 请求睡眠 | Windows API 接受请求 | 若未执行，检查系统或应用阻止原因 |
| `action_complete` | 动作已完成 | 模拟、提醒或同步动作结果 | 本次流程结束 |
| `expired` | 本次自动睡眠已失效 | 超过有效期且未执行 | 需要时重新启用 |
| `error` | Windows 没有接受电源请求 | 原始错误 | 查看记录；手动重新启用；不自动重试 |
| `waiting_for_desktop_backend` | 等待本机 Desktop 后端 | 当前进程无真实执行权限 | 保持本机 Desktop 在线 |
| unknown | 状态无法识别 | 没有可信映射 | 保持唤醒并查看记录 |

## Interaction invariants

- Countdown always exposes a cancellation action.
- Connection loss never leaves a countdown looking live.
- Policy edits cancel the current one-shot campaign.
- Error never implies automatic retry.
- A successful Windows request is not described as proof that the machine actually slept.
