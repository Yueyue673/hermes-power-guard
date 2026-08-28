# Troubleshooting

## The PC has not slept

Open **Overview** and read the current conclusion. Common blockers:

| Message | Meaning | What happens next |
|---|---|---|
| Hermes 还有 N 项工作 | A session, terminal, subagent, cron job, or Desktop window is still active | Power Guard continues automatically when the count reaches zero |
| 等待启用后的第一个任务 | No new task has started since confirmation | Start a new Hermes task; old work is not used to trigger sleep |
| 任务结果不符合规则 | A result is failed, interrupted, blocked, or unknown under the selected rule | Open **记录** or change the rule and confirm again |
| 控制面板已离线 | The visible cancellation lease expired | Reopen Power Guard and restore the Desktop connection |
| 刚刚检测到键鼠操作 | The configured idle period has not elapsed | Stop input; the remaining time updates automatically |
| 受保护程序仍在运行 | An executable listed under protected applications is active | Close it or remove it from the rule |
| 读不到后台/键鼠状态 | Required evidence is unavailable | Power Guard stays awake until the sensor recovers |

## The plugin page says the backend is not loaded

The Python plugin and Desktop UI are separate enable gates.

1. Run:
   ```bash
   hermes plugins enable power-guard --no-allow-tool-override
   ```
2. Restart Hermes Desktop once.
3. Open **Settings → Plugins** and enable **Power Guard**.

## Windows did not sleep after the request

Power Guard uses a non-forced Windows request. An application, driver, update, or Windows power policy may reject it.

- Open **记录** and read the latest error.
- Check Windows power requests and application state.
- Save unsaved work.
- Power Guard does not retry automatically; enable a new one-shot rule if you want to try again.

## Test without sleeping the PC

Open **节能 → 诊断工具** and run the 8-second simulation. It exercises status polling, notifications, countdown, cancellation, and action recording without calling the Windows power API.

`关闭显示器测试` is different: it sends a real display-off signal. Move the mouse to wake the display.

## Another profile or remote backend is running

Power Guard can use busy state visible to the connected Desktop as a blocker. A completely separate remote backend or profile that is not connected to this Desktop cannot be proven idle and is outside the completion cohort.

## Logs and safe bug reports

Use **记录** for recent task outcomes and state changes. Before posting a bug:

- remove tokens, local paths, task content, and process commands that contain private data;
- reproduce with simulation mode when possible;
- include Power Guard, Hermes, and Windows versions;
- describe the exact state sequence.

Use the repository's structured bug report form. Report a power-action bypass privately through the security policy.
