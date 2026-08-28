# Hermes Power Guard

**当 Hermes 可观测的工作全部结束后，安全地让 Windows 自动睡眠。**

Power Guard 是一个统一的 Hermes Agent 插件：Python 后端负责生命周期状态机，Desktop 插件提供原生控制面板。它不会因为模型回复“完成了”就睡眠，而是实际检查 turn、后台终端、待回灌结果、子代理、运行中的 Cron、审批等待、Kanban worker 和可见 Desktop 忙碌会话。

> 默认动作是**睡眠**。真实电源动作仅支持 Windows，并且只能由本地 Hermes Desktop 管理的后端执行。自动测试永远只模拟。

[English README](README.md)

## 核心能力

- 七道安全门，直接解释“为什么还没睡眠”
- 每次必须显式武装；安装、重启后永远未武装
- 只有武装后新出现的工作才能构成完成批次
- 后台传感器异常按“未知”处理，禁止冒险睡眠
- 控制面板心跳消失，立即停止资格流程
- 倒计时期间任何键鼠输入都会撤销
- 新任务、设置变化、时钟跳变、系统恢复都会重走确认链
- 多窗口独立心跳，空闲窗口不会覆盖忙碌窗口
- 支持延后15分钟、恢复判断、12小时自动失效
- 原生 Hermes 确认框、状态栏、预设、历史依据和离线提示

## 安装

Windows PowerShell：

```powershell
git clone https://github.com/Yueyue673/hermes-power-guard.git "$env:LOCALAPPDATA\hermes\plugins\power-guard"
hermes plugins enable power-guard --no-allow-tool-override
```

然后重启一次 Hermes Desktop，在 **设置 → 插件** 中启用 **Power Guard**。Python/API 和 Desktop UI 是两个独立开关，必须同时启用。

也可以克隆到任意目录后运行：

```bash
python scripts/install.py
```

安装脚本只复制插件源码并通过 Hermes CLI 启用插件，绝不会自动武装。

## 使用

1. 从侧栏打开 **自动睡眠**。
2. 选择“稳妥睡眠”“只在成功后睡眠”或“长任务省电”，也可以自定义。
3. 保存设置。
4. 点击“检查并确认自动睡眠”。
5. 检查动作、终态规则、用户空闲、静默窗口、倒计时和有效期。
6. 在 Hermes 原生确认框中武装。

武装后不会立即睡眠；必须先观察到一个新任务。

## 默认规则

| 设置 | 默认值 |
|---|---:|
| 最终动作 | 睡眠 |
| 终态规则 | 完成或结构化阻塞 |
| 手动中断 | 不符合执行条件 |
| 自然语言阻塞识别 | 关闭 |
| 用户空闲 | 5分钟 |
| 静默确认 | 30秒 |
| 最终倒计时 | 90秒 |
| 武装有效期 | 12小时 |
| 模拟模式 | 关闭 |

睡眠使用非强制的 `SetSuspendState`，Windows 或应用可以因为未保存工作而拒绝挂起。

## 范围边界

完成目标是当前 Hermes profile 中、确认武装后被插件观察到的任务。当前 Desktop 能看到的其他忙碌会话、后台进程、子代理和运行中的 Cron 始终作为安全否决项。

完全独立、未连接到当前 Desktop 的远程 backend 无法被证明为空闲；Power Guard 不会假装覆盖它。

## 开发与验证

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
python -m py_compile power_guard_core.py __init__.py dashboard/plugin_api.py
node --check desktop/plugin.js
hermes plugins doctor . --ci
```

0.3.0 已通过40项单元/API/并发/安全测试、Plugin Doctor、隔离真实 API 链路和模拟倒计时。测试套件不会执行真实睡眠、关机、休眠、锁屏或关闭显示器。

## 许可证

MIT，见 [LICENSE](LICENSE)。
