import {
  Badge,
  Button,
  Codicon,
  ConfirmDialog,
  host,
  Input,
  PALETTE_AREA,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  STATUSBAR_AREAS,
  Switch,
  useMutation,
  useQuery,
  useQueryClient,
  useValue
} from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

let pluginContext = null
const QUERY_KEY = ['power-guard', 'status']
const UI_INSTANCE_ID = globalThis.crypto?.randomUUID?.() || `renderer-${Date.now()}-${Math.random().toString(16).slice(2)}`

const STATE_LABELS = {
  disarmed: '未启用',
  armed_waiting_for_task: '已启用 · 等待新任务',
  running: '任务运行中',
  armed_waiting_for_quiet: '等待任务安静',
  waiting_for_terminal_signal: '等待任务终态',
  waiting_for_desktop_ui: '等待桌面控制面板在线',
  terminal_not_eligible: '终态不符合当前规则',
  waiting_for_protected_process: '受保护程序仍在运行',
  waiting_for_idle_check: '无法读取用户空闲状态',
  waiting_for_user_idle: '等待用户离开电脑',
  quiescence: '最后确认窗口',
  snoozed: '已延后',
  countdown: '最终倒计时',
  executing: '正在执行电源动作',
  action_requested: '系统请求已发送',
  action_complete: '动作已完成',
  expired: '武装已过期',
  error: '发生错误'
}

const ACTION_LABELS = {
  shutdown: '关机',
  hibernate: '休眠',
  sleep: '睡眠',
  lock: '锁屏',
  notify: '只提醒，不关机'
}

const PRESETS = [
  {
    id: 'safe-sleep',
    label: '稳妥睡眠',
    description: '完成或结构化阻塞后，空闲 5 分钟再睡眠。',
    values: {
      action: 'sleep',
      trigger_mode: 'done_or_blocked',
      include_failures: true,
      include_interrupted: false,
      detect_blocker_text: false,
      require_user_idle: true,
      user_idle_seconds: 300,
      quiescence_seconds: 30,
      countdown_seconds: 90,
      waiting_input_timeout_seconds: 900,
      background_process_policy: 'wait_all',
      prevent_sleep_while_working: true,
      turn_off_display_when_idle: false,
      lower_hermes_priority: false,
      power_plan: 'unchanged',
      arm_expiry_minutes: 720,
      dry_run: false
    }
  },
  {
    id: 'success-only',
    label: '只在成功后睡眠',
    description: '任何失败、阻塞或未知结果都保持唤醒。',
    values: {
      action: 'sleep',
      trigger_mode: 'done_only',
      include_failures: false,
      include_interrupted: false,
      detect_blocker_text: false,
      require_user_idle: true,
      user_idle_seconds: 600,
      quiescence_seconds: 60,
      countdown_seconds: 120,
      background_process_policy: 'wait_all',
      arm_expiry_minutes: 720,
      dry_run: false
    }
  },
  {
    id: 'overnight-eco',
    label: '长任务省电',
    description: '后台工作时关显示器、降优先级并切换平衡计划。',
    values: {
      action: 'sleep',
      trigger_mode: 'done_or_blocked',
      include_failures: true,
      include_interrupted: false,
      detect_blocker_text: false,
      require_user_idle: true,
      user_idle_seconds: 300,
      quiescence_seconds: 60,
      countdown_seconds: 120,
      background_process_policy: 'wait_all',
      prevent_sleep_while_working: true,
      turn_off_display_when_idle: true,
      display_off_idle_seconds: 300,
      lower_hermes_priority: true,
      power_plan: 'balanced',
      restore_power_plan: true,
      arm_expiry_minutes: 1440,
      dry_run: false
    }
  }
]

function request(path, options = {}) {
  if (!pluginContext) {
    return Promise.reject(new Error('Power Guard plugin context is not ready'))
  }
  return pluginContext.rest(path, options)
}

function formatSeconds(seconds) {
  const value = Math.max(0, Number(seconds) || 0)
  if (value < 60) return `${Math.round(value)} 秒`
  if (value >= 86400) {
    const days = Math.floor(value / 86400)
    const hours = Math.floor((value % 86400) / 3600)
    return hours ? `${days} 天 ${hours} 小时` : `${days} 天`
  }
  if (value >= 3600) {
    const hours = Math.floor(value / 3600)
    const minutes = Math.floor((value % 3600) / 60)
    return minutes ? `${hours} 小时 ${minutes} 分钟` : `${hours} 小时`
  }
  const minutes = Math.floor(value / 60)
  const rest = Math.round(value % 60)
  return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分钟`
}

function stateLabel(runtime) {
  if (!runtime) return '状态未知'
  const base = STATE_LABELS[runtime.state] || runtime.state || '状态未知'
  if (runtime.state === 'countdown' && runtime.countdown_remaining_seconds != null) {
    return `${base} · ${formatSeconds(runtime.countdown_remaining_seconds)}`
  }
  if (runtime.snooze_remaining_seconds != null) {
    return `${base} · ${formatSeconds(runtime.snooze_remaining_seconds)}`
  }
  return base
}

function settingDraft(settings) {
  return {
    ...settings,
    arm_expiry_minutes: settings.arm_expiry_minutes ?? 720,
    blocker_keywords_text: (settings.blocker_keywords || []).join('\n'),
    protected_processes_text: (settings.protected_processes || []).join('\n')
  }
}

function draftPayload(draft) {
  return {
    ...draft,
    blocker_keywords: String(draft.blocker_keywords_text || '')
      .split(/[,\n]/)
      .map(value => value.trim())
      .filter(Boolean),
    protected_processes: String(draft.protected_processes_text || '')
      .split(/[,;\n]/)
      .map(value => value.trim())
      .filter(Boolean)
  }
}

function Section({ title, description, children, defaultOpen = false }) {
  const [open, setOpen] = useState(defaultOpen)
  return jsxs('section', {
    className: 'flex flex-col rounded-lg border border-(--ui-stroke-secondary)',
    children: [
      jsxs('button', {
        className: 'flex items-start justify-between gap-3 p-4 text-left',
        onClick: () => setOpen(value => !value),
        type: 'button',
        children: [
          jsxs('span', {
            className: 'flex min-w-0 flex-col gap-1',
            children: [
              jsx('span', { className: 'text-sm font-semibold text-foreground', children: title }),
              description
                ? jsx('span', { className: 'text-xs leading-5 text-(--ui-text-tertiary)', children: description })
                : null
            ]
          }),
          jsx(Codicon, { name: open ? 'chevron-up' : 'chevron-down', size: '0.9rem' })
        ]
      }),
      open
        ? jsx('div', {
            className: 'flex flex-col gap-3 border-t border-(--ui-stroke-secondary) p-4',
            children
          })
        : null
    ]
  })
}

function Field({ label, help, children }) {
  return jsxs('label', {
    className: 'grid gap-1.5 md:grid-cols-[minmax(12rem,0.85fr)_minmax(14rem,1.15fr)] md:items-center',
    children: [
      jsxs('span', {
        className: 'flex min-w-0 flex-col gap-0.5',
        children: [
          jsx('span', { className: 'text-xs font-medium text-foreground', children: label }),
          help ? jsx('span', { className: 'text-[0.6875rem] leading-4 text-(--ui-text-quaternary)', children: help }) : null
        ]
      }),
      children
    ]
  })
}

function ToggleField({ label, help, checked, onChange, disabled = false }) {
  return jsx(Field, {
    label,
    help,
    children: jsxs('div', {
      className: 'flex items-center gap-2 md:justify-end',
      children: [
        jsx(Switch, {
          'aria-label': label,
          checked: Boolean(checked),
          disabled,
          onCheckedChange: onChange,
          size: 'xs'
        }),
        jsx('span', {
          className: 'w-10 text-xs text-(--ui-text-secondary)',
          children: checked ? '开启' : '关闭'
        })
      ]
    })
  })
}

function NumberField({ label, help, value, onChange, min = 0, max = 86400, suffix = '秒' }) {
  return jsx(Field, {
    label,
    help,
    children: jsxs('div', {
      className: 'flex items-center gap-2 md:justify-end',
      children: [
        jsx(Input, {
          className: 'w-28 tabular-nums',
          max,
          min,
          onChange: event => onChange(Number(event.target.value)),
          type: 'number',
          value: value ?? 0
        }),
        jsx('span', { className: 'w-8 text-xs text-(--ui-text-tertiary)', children: suffix })
      ]
    })
  })
}

function SelectField({ label, help, value, onChange, options }) {
  return jsx(Field, {
    label,
    help,
    children: jsx('select', {
      className:
        'h-8 min-w-48 rounded-md border border-(--ui-stroke-secondary) bg-transparent px-2 text-xs text-foreground outline-none focus:border-(--ui-accent)',
      onChange: event => onChange(event.target.value),
      value,
      children: options.map(option =>
        jsx(
          'option',
          {
            className: 'bg-(--ui-surface-primary) text-foreground',
            disabled: Boolean(option.disabled),
            value: option.value,
            children: option.label
          },
          option.value
        )
      )
    })
  })
}

function Metric({ label, value, tone = 'normal' }) {
  const valueClass = tone === 'danger' ? 'text-destructive' : tone === 'accent' ? 'text-(--ui-accent)' : 'text-foreground'
  return jsxs('div', {
    className: 'flex min-w-0 flex-col gap-1 rounded-md border border-(--ui-stroke-secondary) px-3 py-2',
    children: [
      jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: label }),
      jsx('span', { className: `truncate text-sm font-medium tabular-nums ${valueClass}`, children: String(value) })
    ]
  })
}

function DecisionCard({ decision }) {
  if (!decision) return null
  const passed = decision.gates.filter(gate => gate.status === 'pass').length
  const progress = Math.round((passed / Math.max(1, decision.gates.length)) * 100)
  const marker = status =>
    status === 'pass'
      ? { icon: 'pass-filled', className: 'text-(--ui-accent)' }
      : status === 'blocked'
        ? { icon: 'error', className: 'text-destructive' }
        : status === 'active'
          ? { icon: 'loading', className: 'text-(--ui-accent)' }
          : { icon: 'circle-large-outline', className: 'text-(--ui-text-quaternary)' }

  return jsxs('section', {
    className: 'flex flex-col gap-3 rounded-lg border border-(--ui-stroke-secondary) p-4',
    children: [
      jsxs('div', {
        className: 'flex flex-wrap items-start justify-between gap-3',
        children: [
          jsxs('div', {
            'aria-atomic': 'true',
            'aria-live': 'polite',
            className: 'flex min-w-0 flex-col gap-1',
            role: 'status',
            children: [
              jsx('span', { className: 'text-base font-semibold text-foreground', children: decision.summary }),
              jsx('span', { className: 'text-xs leading-5 text-(--ui-text-tertiary)', children: decision.detail })
            ]
          }),
          jsx(Badge, { children: `${passed}/${decision.gates.length} 安全门` })
        ]
      }),
      jsx('div', {
        className: 'h-1.5 overflow-hidden rounded-full bg-(--ui-stroke-secondary)',
        children: jsx('div', {
          className: 'h-full rounded-full bg-(--ui-accent) transition-[width] duration-300',
          style: { width: `${progress}%` }
        })
      }),
      jsx('ol', {
        className: 'grid gap-2 sm:grid-cols-2 lg:grid-cols-4',
        children: decision.gates.map(gate => {
          const visual = marker(gate.status)
          return jsxs('li', {
            'aria-current': gate.status === 'active' ? 'step' : undefined,
            className: 'flex items-center gap-2 rounded-md border border-(--ui-stroke-secondary) px-2.5 py-2 text-xs',
            children: [
              jsx(Codicon, { className: visual.className, name: visual.icon, size: '0.8rem' }),
              jsx('span', { className: 'min-w-0 flex-1 truncate text-(--ui-text-secondary)', children: gate.label }),
              gate.value != null
                ? jsx('span', { className: 'tabular-nums text-(--ui-text-quaternary)', children: String(gate.value) })
                : null
            ]
          }, gate.id)
        })
      })
    ]
  })
}

function HistoryPanel({ status, open, onToggle }) {
  const events = status.events || []
  const tasks = status.tasks || []
  const outcomeLabel = outcome => ({ completed: '完成', blocked: '阻塞', failed: '失败', interrupted: '中断', unknown: '未知' }[outcome] || outcome || '运行中')
  const eventLabel = event => ({
    armed: '已确认武装',
    cancelled: '已解除武装',
    settings: '设置已保存',
    settings_disarm: '设置变化，已要求重新确认',
    turn_terminal: '任务进入终态',
    quiescence: '开始静默确认',
    countdown_started: '最终倒计时开始',
    countdown_cancelled: '倒计时已撤销',
    action_claimed: '最终动作进入复核',
    action_requested: '系统请求已发送',
    action_complete: '最终动作完成',
    action_error: '最终动作失败',
    action_aborted: '最终动作被安全门撤销',
    snoozed: '已延后最终动作',
    snooze_resumed: '已取消延后',
    snooze_elapsed: '延后结束，重新判断',
    campaign_expired: '武装已自动过期',
    restart_disarm: '重启后自动解除武装',
    test: '模拟倒计时开始',
    test_execute: '模拟动作进入执行'
  }[event.kind] || event.message)
  const timeLabel = timestamp => {
    if (!timestamp) return ''
    try {
      return new Date(Number(timestamp) * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
    } catch (_) {
      return ''
    }
  }

  return jsxs('section', {
    className: 'flex flex-col rounded-lg border border-(--ui-stroke-secondary)',
    children: [
      jsxs('button', {
        className: 'flex items-center justify-between gap-3 px-4 py-3 text-left',
        onClick: onToggle,
        type: 'button',
        children: [
          jsxs('span', {
            className: 'flex flex-col gap-0.5',
            children: [
              jsx('span', { className: 'text-sm font-semibold text-foreground', children: '最近判断与任务' }),
              jsx('span', {
                className: 'text-xs text-(--ui-text-tertiary)',
                children: `${tasks.length} 个任务记录 · ${events.length} 条事件`
              })
            ]
          }),
          jsx(Codicon, { name: open ? 'chevron-up' : 'chevron-down', size: '0.9rem' })
        ]
      }),
      open
        ? jsxs('div', {
            className: 'grid gap-4 border-t border-(--ui-stroke-secondary) p-4 lg:grid-cols-2',
            children: [
              jsxs('div', {
                className: 'flex min-w-0 flex-col gap-2',
                children: [
                  jsx('span', { className: 'text-xs font-medium text-foreground', children: '任务终态' }),
                  ...(tasks.slice(0, 6).length
                    ? tasks.slice(0, 6).map(task =>
                        jsxs('div', {
                          className: 'flex items-start justify-between gap-3 rounded-md border border-(--ui-stroke-secondary) px-3 py-2 text-xs',
                          children: [
                            jsxs('span', {
                              className: 'flex min-w-0 flex-col gap-0.5',
                              children: [
                                jsx('span', { className: 'truncate text-(--ui-text-secondary)', children: task.last_tool || task.platform || task.task_key }),
                                jsx('span', { className: 'truncate text-(--ui-text-quaternary)', children: task.reason || '等待结果' })
                              ]
                            }),
                            jsx(Badge, { children: outcomeLabel(task.outcome) })
                          ]
                        }, task.task_key)
                      )
                    : [jsx('span', { className: 'text-xs text-(--ui-text-quaternary)', children: '武装后开始的新任务会显示在这里。' }, 'empty')])
                ]
              }),
              jsxs('div', {
                className: 'flex min-w-0 flex-col gap-2',
                children: [
                  jsx('span', { className: 'text-xs font-medium text-foreground', children: '状态事件' }),
                  ...(events.slice(0, 8).length
                    ? events.slice(0, 8).map(event =>
                        jsxs('div', {
                          className: 'flex items-start gap-2 text-xs leading-5',
                          children: [
                            jsx('span', { className: 'w-16 shrink-0 tabular-nums text-(--ui-text-quaternary)', children: timeLabel(event.created_at) }),
                            jsx('span', { className: 'min-w-0 text-(--ui-text-secondary)', children: eventLabel(event) })
                          ]
                        }, event.id)
                      )
                    : [jsx('span', { className: 'text-xs text-(--ui-text-quaternary)', children: '还没有事件。' }, 'empty')])
                ]
              })
            ]
          })
        : null
    ]
  })
}

function RuntimeCard({ status, cancelMutation, snoozeMutation, resumeMutation }) {
  const settings = status.settings
  const runtime = status.runtime
  const armed = Boolean(settings.armed)
  const countdown = runtime.state === 'countdown'
  const lastAction = runtime.last_action

  return jsxs('div', {
    className: 'flex flex-col gap-4 rounded-lg border border-(--ui-stroke-secondary) p-4',
    children: [
      jsxs('div', {
        className: 'flex flex-wrap items-start justify-between gap-3',
        children: [
          jsxs('div', {
            className: 'flex min-w-0 flex-col gap-1',
            children: [
              jsxs('div', {
                className: 'flex items-center gap-2',
                children: [
                  jsx(Codicon, { name: countdown ? 'warning' : armed ? 'pulse' : 'circle-large-outline', size: '0.9rem' }),
                  jsx('span', { className: 'text-sm font-semibold', children: stateLabel(runtime) }),
                  armed ? jsx(Badge, { children: ACTION_LABELS[settings.action] || settings.action }) : null,
                  settings.dry_run ? jsx(Badge, { children: '模拟模式' }) : null
                ]
              }),
              jsx('p', {
                className: 'text-xs text-(--ui-text-tertiary)',
                children: armed
                  ? '已见过任务后，Power Guard 才会进入终态判定；单纯启动 Hermes 不会触发。'
                  : '当前不会执行任何自动电源动作。'
              })
            ]
          }),
          armed || countdown
            ? jsxs('div', {
                className: 'flex flex-wrap items-center gap-2',
                children: [
                  status.capabilities?.snooze_supported
                    ? runtime.snooze_remaining_seconds
                      ? jsx(Button, {
                          disabled: resumeMutation.isPending,
                          onClick: () => resumeMutation.mutate(),
                          variant: 'outline',
                          children: '取消延后，继续判断'
                        })
                      : jsx(Button, {
                          disabled: snoozeMutation.isPending,
                          onClick: () => snoozeMutation.mutate(15),
                          variant: 'outline',
                          children: '延后 15 分钟'
                        })
                    : null,
                  jsx(Button, {
                    disabled: cancelMutation.isPending,
                    onClick: () => cancelMutation.mutate({ reason: 'desktop_button' }),
                    variant: countdown ? 'destructive' : 'outline',
                    children: countdown ? '取消倒计时并解除' : '解除启用'
                  })
                ]
              })
            : null
        ]
      }),
      jsxs('div', {
        className: 'grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5',
        children: [
          jsx(Metric, {
            label: 'Hermes 会话',
            value: (runtime.active_tasks || 0) + (runtime.desktop_busy_count || 0),
            tone: runtime.active_tasks || runtime.desktop_busy_count ? 'accent' : 'normal'
          }),
          jsx(Metric, { label: '后台进程', value: runtime.active_background_processes || 0 }),
          jsx(Metric, { label: '子代理', value: runtime.active_delegations || 0 }),
          jsx(Metric, {
            label: '用户空闲',
            value: runtime.user_idle_seconds == null ? '未知' : formatSeconds(runtime.user_idle_seconds)
          }),
          jsx(Metric, {
            label: runtime.snooze_remaining_seconds ? '延后剩余' : '武装剩余',
            value: runtime.snooze_remaining_seconds != null
              ? formatSeconds(runtime.snooze_remaining_seconds)
              : runtime.armed_remaining_seconds != null
                ? formatSeconds(runtime.armed_remaining_seconds)
                : '—'
          })
        ]
      }),
      runtime.last_error
        ? jsx('div', {
            className: 'rounded-md border border-destructive/40 px-3 py-2 text-xs text-destructive',
            children: runtime.last_error
          })
        : null,
      lastAction
        ? jsx('div', {
            className: 'rounded-md border border-(--ui-stroke-secondary) px-3 py-2 text-xs text-(--ui-text-secondary)',
            children: `${lastAction.simulated ? '模拟' : '实际'} ${ACTION_LABELS[lastAction.action] || lastAction.action}：${lastAction.message}`
          })
        : null
    ]
  })
}

function ArmConfirmation({ draft }) {
  return jsxs('div', {
    className: 'flex flex-col gap-2 text-xs leading-5 text-(--ui-text-secondary)',
    children: [
      jsx('p', {
        children: `确认后不会立即${ACTION_LABELS[draft.action] || draft.action}；只观察确认后开始的新任务。`
      }),
      jsx('p', {
        children: `终态：${draft.trigger_mode === 'done_only' ? '仅全部成功' : '完成或结构化阻塞'}`
      }),
      jsx('p', {
        children: `用户空闲：${draft.require_user_idle ? formatSeconds(draft.user_idle_seconds) : '不作为进入门槛'}；静默 ${formatSeconds(draft.quiescence_seconds)}；倒计时 ${formatSeconds(draft.countdown_seconds)}。`
      }),
      jsx('p', {
        children: `武装将在 ${formatSeconds((draft.arm_expiry_minutes || 720) * 60)}后自动失效；重启 Hermes 也会解除。`
      }),
      jsx('p', {
        className: 'font-medium text-foreground',
        children: '任何新任务、键鼠活动、设置变化或控制面板离线都会撤销倒计时。'
      })
    ]
  })
}

function PowerGuardPage() {
  const queryClient = useQueryClient()
  const statusQuery = useQuery({
    queryFn: () => request('/status'),
    queryKey: QUERY_KEY,
    refetchInterval: 2000
  })
  const [draft, setDraft] = useState(null)
  const [dirty, setDirty] = useState(false)
  const [armConfirmOpen, setArmConfirmOpen] = useState(false)
  const [showDetails, setShowDetails] = useState(false)

  useEffect(() => {
    if (statusQuery.data?.settings && (!draft || !dirty)) {
      setDraft(settingDraft(statusQuery.data.settings))
    }
  }, [statusQuery.data?.settings, dirty])

  const refreshRuntime = data => {
    queryClient.setQueryData(QUERY_KEY, data)
  }

  const refreshSettings = data => {
    refreshRuntime(data)
    if (data?.settings) {
      setDraft(settingDraft(data.settings))
      setDirty(false)
    }
  }

  const saveMutation = useMutation({
    mutationFn: payload => request('/settings', { method: 'POST', body: payload }),
    onSuccess: data => {
      const confirmationInvalidated = Boolean(statusQuery.data?.settings?.armed && !data.settings?.armed)
      refreshSettings(data)
      if (confirmationInvalidated) {
        setArmConfirmOpen(true)
        host.notify({
          kind: 'warning',
          title: '设置已保存，武装已解除',
          message: '规则发生变化，需要重新检查并确认后才能自动睡眠。'
        })
      }
    },
    onError: error => host.notifyError(error, 'Power Guard 设置保存失败')
  })
  const armMutation = useMutation({
    mutationFn: () => request('/arm', { method: 'POST', body: {} }),
    onSuccess: data => {
      refreshSettings(data)
      host.notify({ kind: 'success', title: 'Power Guard 已确认启用', message: `自动${ACTION_LABELS[data.settings.action] || data.settings.action}已武装；新任务完成后才会进入倒计时。` })
    },
    onError: error => host.notifyError(error, 'Power Guard 启用失败')
  })
  const cancelMutation = useMutation({
    mutationFn: body => request('/cancel', { method: 'POST', body }),
    onSuccess: data => {
      refreshRuntime(data)
      host.notify({ kind: 'info', message: 'Power Guard 已解除，倒计时已取消。' })
    },
    onError: error => host.notifyError(error, '取消失败')
  })
  const snoozeMutation = useMutation({
    mutationFn: minutes => request('/snooze', { method: 'POST', body: { minutes } }),
    onSuccess: data => {
      refreshRuntime(data)
      host.notify({ kind: 'info', title: '已延后自动睡眠', message: `Power Guard 将在 ${formatSeconds(data.runtime.snooze_remaining_seconds)}后重新判断。` })
    },
    onError: error => host.notifyError(error, '延后失败')
  })
  const resumeMutation = useMutation({
    mutationFn: () => request('/resume', { method: 'POST', body: {} }),
    onSuccess: data => {
      refreshRuntime(data)
      host.notify({ kind: 'info', message: '已取消延后，Power Guard 正在重新判断。' })
    },
    onError: error => host.notifyError(error, '恢复判断失败')
  })
  const testMutation = useMutation({
    mutationFn: () => request('/test', { method: 'POST', body: { seconds: 8 } }),
    onSuccess: data => {
      refreshRuntime(data)
      host.notify({ kind: 'info', message: '已启动 8 秒模拟倒计时，不会执行真实电源动作。' })
    },
    onError: error => host.notifyError(error, '模拟测试启动失败')
  })
  const displayMutation = useMutation({
    mutationFn: () => request('/display-off', { method: 'POST', body: {} }),
    onSuccess: () => host.notify({ kind: 'success', message: '已发送关闭显示器信号；移动鼠标即可唤醒。' }),
    onError: error => host.notifyError(error, '关闭显示器失败')
  })

  if (statusQuery.isLoading && !statusQuery.data) {
    return jsxs('div', {
      className: 'flex h-full items-center justify-center gap-2 text-sm text-(--ui-text-tertiary)',
      children: [jsx(Codicon, { name: 'loading', size: '1rem' }), '正在读取 Power Guard…']
    })
  }

  if (statusQuery.error && !statusQuery.data) {
    return jsxs('div', {
      className: 'flex h-full flex-col items-start gap-3 p-5',
      children: [
        jsx('h1', { className: 'text-base font-semibold', children: 'Power Guard 后端尚未加载' }),
        jsx('p', {
          className: 'max-w-2xl text-sm leading-6 text-(--ui-text-tertiary)',
          children: '插件文件已经就位，但 Python API 只在 Hermes 后端启动时挂载。重启一次 Hermes Desktop 后，这个面板就会连通。'
        }),
        jsx(Button, { onClick: () => statusQuery.refetch(), variant: 'outline', children: '重试连接' })
      ]
    })
  }

  const status = statusQuery.data
  if (!status || !draft) return null
  const settings = status.settings
  const connectionStale = Boolean(statusQuery.isError)
  const update = (key, value) => {
    setDraft(current => ({ ...current, [key]: value }))
    setDirty(true)
  }
  const applyPreset = preset => {
    setDraft(current => ({ ...current, ...preset.values }))
    setDirty(true)
    setArmConfirmOpen(false)
  }

  return jsxs('div', {
    className: 'h-full overflow-auto',
    children: [
      jsxs('div', {
        className: 'flex w-full max-w-5xl flex-col gap-4 p-5',
        children: [
          jsxs('header', {
            className: 'flex flex-wrap items-start justify-between gap-3',
            children: [
              jsxs('div', {
                className: 'flex min-w-0 flex-col gap-1',
                children: [
                  jsx('h1', {
                    className: 'text-lg font-semibold text-foreground',
                    children: `自动${ACTION_LABELS[draft.action] || draft.action} · Power Guard`
                  }),
                  jsx('p', {
                    className: 'max-w-3xl text-sm leading-6 text-(--ui-text-tertiary)',
                    children: '当前 profile 中确认后开始的新任务是完成目标；所有可见 Desktop 忙碌会话、后台进程和子代理始终只能阻止执行。'
                  })
                ]
              }),
              jsxs('div', {
                className: 'flex items-center gap-2',
                children: [
                  dirty
                    ? jsx(Badge, { children: '有未保存设置' })
                    : jsx(Badge, { children: '设置已保存' }),
                  jsx(Button, {
                    disabled: connectionStale || !dirty || saveMutation.isPending,
                    onClick: () => saveMutation.mutate(draftPayload(draft)),
                    children: saveMutation.isPending ? '保存中…' : '保存设置'
                  })
                ]
              })
            ]
          }),
          connectionStale
            ? jsxs('div', {
                className: 'flex items-start justify-between gap-3 rounded-lg border border-destructive/50 bg-destructive/5 p-3',
                role: 'alert',
                children: [
                  jsxs('div', {
                    className: 'flex min-w-0 flex-col gap-1',
                    children: [
                      jsx('span', { className: 'text-sm font-medium text-destructive', children: '控制面板连接中断' }),
                      jsx('span', {
                        className: 'text-xs leading-5 text-(--ui-text-tertiary)',
                        children: '自动电源动作已安全暂停。当前显示的是上次成功状态，正在重连。'
                      })
                    ]
                  }),
                  jsx(Button, { onClick: () => statusQuery.refetch(), variant: 'outline', children: '立即重试' })
                ]
              })
            : null,
          !status.capabilities?.decision_explanation_supported
            ? jsxs('div', {
                className: 'flex items-start gap-3 rounded-lg border border-(--ui-accent) bg-(--ui-accent)/5 p-3',
                children: [
                  jsx(Codicon, { className: 'mt-0.5 text-(--ui-accent)', name: 'sync', size: '0.9rem' }),
                  jsxs('div', {
                    className: 'flex min-w-0 flex-col gap-1',
                    children: [
                      jsx('span', { className: 'text-sm font-medium text-foreground', children: '0.3 后端将在下次重启后启用' }),
                      jsx('span', {
                        className: 'text-xs leading-5 text-(--ui-text-tertiary)',
                        children: '新版界面已热加载；状态解释、延后和自动失效需要 Hermes 后端重启。当前不会影响正在运行的任务。'
                      })
                    ]
                  })
                ]
              })
            : null,
          jsxs('section', {
            className: 'flex flex-col gap-2',
            children: [
              jsx('span', { className: 'text-xs font-medium text-(--ui-text-secondary)', children: '快速预设' }),
              jsx('div', {
                className: 'grid gap-2 md:grid-cols-3',
                children: PRESETS.map(preset =>
                  jsxs('button', {
                    className: 'flex flex-col gap-1 rounded-lg border border-(--ui-stroke-secondary) p-3 text-left transition-colors hover:border-(--ui-accent) hover:bg-(--ui-accent)/5',
                    onClick: () => applyPreset(preset),
                    type: 'button',
                    children: [
                      jsx('span', { className: 'text-sm font-medium text-foreground', children: preset.label }),
                      jsx('span', { className: 'text-xs leading-5 text-(--ui-text-tertiary)', children: preset.description })
                    ]
                  }, preset.id)
                )
              })
            ]
          }),
          jsx(DecisionCard, { decision: status.decision }),
          jsx(RuntimeCard, { status, cancelMutation, snoozeMutation, resumeMutation }),
          jsx(ConfirmDialog, {
            cancelLabel: '返回检查设置',
            confirmLabel: `确认并武装自动${ACTION_LABELS[draft.action] || draft.action}`,
            busyLabel: '正在启用…',
            description: jsx(ArmConfirmation, { draft }),
            destructive: !draft.dry_run && draft.action === 'shutdown',
            doneLabel: '已启用',
            onClose: () => setArmConfirmOpen(false),
            onConfirm: () => armMutation.mutateAsync(),
            open: armConfirmOpen && !settings.armed,
            title: `确认启用自动${ACTION_LABELS[draft.action] || draft.action}`
          }),
          jsx(HistoryPanel, {
            status,
            open: showDetails,
            onToggle: () => setShowDetails(value => !value)
          }),
          jsxs(Section, {
            title: '启用与动作',
            defaultOpen: true,
            description: '默认需要你手动确认一次。启用前的旧任务不会倒追，也不会因为当前空闲而直接执行电源动作。',
            children: [
              jsx(SelectField, {
                label: '最终动作',
                help: 'Windows 在倒计时结束后执行。',
                value: draft.action,
                onChange: value => update('action', value),
                options: [
                  { value: 'shutdown', label: ACTION_LABELS.shutdown },
                  { value: 'lock', label: ACTION_LABELS.lock },
                  {
                    value: 'sleep',
                    label: status.capabilities?.sleep_available ? ACTION_LABELS.sleep : '睡眠（当前系统不可用）',
                    disabled: !status.capabilities?.sleep_available
                  },
                  {
                    value: 'hibernate',
                    label: status.capabilities?.hibernate_available ? ACTION_LABELS.hibernate : '休眠（当前未启用）',
                    disabled: !status.capabilities?.hibernate_available
                  },
                  { value: 'notify', label: ACTION_LABELS.notify }
                ]
              }),
              jsx(SelectField, {
                label: '任务终态规则',
                help: '“完成或阻塞”包括已耗尽自动重试、等待人工、登录/授权墙等。',
                value: draft.trigger_mode,
                onChange: value => update('trigger_mode', value),
                options: [
                  { value: 'done_or_blocked', label: '全部完成，或已经无法自动继续' },
                  { value: 'done_only', label: '只有全部成功完成' }
                ]
              }),
              jsx(ToggleField, {
                label: '把失败视为无法继续',
                help: '例如模型/API 重试耗尽。只在“完成或阻塞”模式生效。',
                checked: draft.include_failures,
                onChange: value => update('include_failures', value),
                disabled: draft.trigger_mode !== 'done_or_blocked'
              }),
              jsx(ToggleField, {
                label: '把手动中断也视为终态',
                help: '默认关闭，避免你点击停止后电脑随即执行电源动作。',
                checked: draft.include_interrupted,
                onChange: value => update('include_interrupted', value),
                disabled: draft.trigger_mode !== 'done_or_blocked'
              }),
              jsx(NumberField, {
                label: '安静确认窗口',
                help: '全部任务终止后再观察一段时间，拦住迟到的子代理和后台进程。最短 30 秒。',
                value: draft.quiescence_seconds,
                onChange: value => update('quiescence_seconds', value),
                min: 30,
                max: 3600
              }),
              jsx(NumberField, {
                label: '最后倒计时',
                help: '状态栏和系统通知都会给取消机会。最短 60 秒。',
                value: draft.countdown_seconds,
                onChange: value => update('countdown_seconds', value),
                min: 60,
                max: 3600
              }),
              jsx(NumberField, {
                label: '武装有效期',
                help: '超过这个时间仍未执行，会自动解除武装，避免隔天误触发。',
                value: draft.arm_expiry_minutes,
                onChange: value => update('arm_expiry_minutes', value),
                min: 30,
                max: 10080,
                suffix: '分钟'
              })
            ]
          }),
          jsxs(Section, {
            title: '“无法自动继续”识别',
            description: '结构化 turn_exit_reason 优先；普通回复里出现登录、授权、需要输入等语句时，再用下面的关键词补足。',
            children: [
              jsx(ToggleField, {
                label: '识别回复中的阻塞语句',
                help: '关闭后只认 Hermes 的结构化失败/终止原因。',
                checked: draft.detect_blocker_text,
                onChange: value => update('detect_blocker_text', value)
              }),
              jsx(Field, {
                label: '阻塞关键词',
                help: '一行一个；中英文都支持。只在最终回复中匹配。',
                children: jsx('textarea', {
                  className:
                    'min-h-32 w-full resize-y rounded-md border border-(--ui-stroke-secondary) bg-transparent p-2 text-xs leading-5 text-foreground outline-none focus:border-(--ui-accent)',
                  disabled: !draft.detect_blocker_text,
                  onChange: event => update('blocker_keywords_text', event.target.value),
                  value: draft.blocker_keywords_text || ''
                })
              }),
              jsx(NumberField, {
                label: '等待人工多久后算阻塞',
                help: 'clarify、审批等需要你输入的工具在这个时间内仍然算“运行中”。',
                value: draft.waiting_input_timeout_seconds,
                onChange: value => update('waiting_input_timeout_seconds', value),
                min: 60,
                max: 86400
              })
            ]
          }),
          jsxs(Section, {
            title: '防误触发',
            description: '这些门槛全部通过后，才会进入安静窗口。适合你一边打游戏、一边让 Hermes 在后台跑。',
            children: [
              jsx(ToggleField, {
                label: '必须检测到用户空闲',
                help: '你还在操作鼠标键盘时绝不执行最终动作。',
                checked: draft.require_user_idle,
                onChange: value => update('require_user_idle', value)
              }),
              jsx(NumberField, {
                label: '用户空闲门槛',
                help: '从最后一次键盘/鼠标输入开始计算。',
                value: draft.user_idle_seconds,
                onChange: value => update('user_idle_seconds', value),
                min: 0,
                max: 86400
              }),
              jsx(ToggleField, {
                label: '新活动自动取消倒计时',
                help: '强烈建议保持开启。',
                checked: draft.cancel_on_new_activity,
                onChange: value => update('cancel_on_new_activity', value)
              }),
              jsx(SelectField, {
                label: '后台进程策略',
                help: '预览服务器/守护进程可能一直不退出，可选择忽略 detached 进程。',
                value: draft.background_process_policy,
                onChange: value => update('background_process_policy', value),
                options: [
                  { value: 'wait_all', label: '等待全部 Hermes 后台进程' },
                  { value: 'ignore_detached', label: '忽略 detached 守护进程' }
                ]
              }),
              jsx(Field, {
                label: '受保护程序',
                help: '检测到这些 exe 正在运行时不执行；一行一个，例如 blender.exe。',
                children: jsx('textarea', {
                  className:
                    'min-h-20 w-full resize-y rounded-md border border-(--ui-stroke-secondary) bg-transparent p-2 text-xs leading-5 text-foreground outline-none focus:border-(--ui-accent)',
                  onChange: event => update('protected_processes_text', event.target.value),
                  placeholder: 'blender.exe\nobs64.exe',
                  value: draft.protected_processes_text || ''
                })
              })
            ]
          }),
          jsxs(Section, {
            title: '节能功能',
            description: '任务期间先保证工作不中断，再按需降低耗电；结束时恢复电源计划，然后执行最终动作。',
            children: [
              jsx(ToggleField, {
                label: '任务运行时阻止系统睡眠',
                help: '只保持系统运行，不强制点亮显示器。',
                checked: draft.prevent_sleep_while_working,
                onChange: value => update('prevent_sleep_while_working', value)
              }),
              jsx(ToggleField, {
                label: '空闲后关闭显示器',
                help: '移动鼠标即可唤醒；不会暂停 Hermes。',
                checked: draft.turn_off_display_when_idle,
                onChange: value => update('turn_off_display_when_idle', value)
              }),
              jsx(NumberField, {
                label: '关闭显示器空闲门槛',
                help: '只在 Hermes 仍有工作时生效。',
                value: draft.display_off_idle_seconds,
                onChange: value => update('display_off_idle_seconds', value),
                min: 30,
                max: 86400
              }),
              jsx(ToggleField, {
                label: '降低 Hermes 进程优先级',
                help: '减少对游戏/前台应用的干扰；任务可能略慢。',
                checked: draft.lower_hermes_priority,
                onChange: value => update('lower_hermes_priority', value)
              }),
              jsx(SelectField, {
                label: '任务期间电源计划',
                help: 'Power Guard 会记住原计划并在结束/取消时恢复。',
                value: draft.power_plan,
                onChange: value => update('power_plan', value),
                options: [
                  { value: 'unchanged', label: '保持当前计划' },
                  { value: 'balanced', label: '平衡' },
                  { value: 'power_saver', label: '节能' }
                ]
              }),
              jsx(ToggleField, {
                label: '结束后恢复原电源计划',
                help: '建议开启，解除 Power Guard 时也会恢复。',
                checked: draft.restore_power_plan,
                onChange: value => update('restore_power_plan', value),
                disabled: draft.power_plan === 'unchanged'
              })
            ]
          }),
          jsxs(Section, {
            title: '安全测试',
            description: '先验证倒计时、状态栏和取消链路。模拟模式永远不会调用 Windows 电源 API。',
            children: [
              jsx(ToggleField, {
                label: '全局模拟模式',
                help: '即使真实规则触发，也只记录 SIMULATED 动作。',
                checked: draft.dry_run,
                onChange: value => update('dry_run', value)
              }),
              jsxs('div', {
                className: 'flex flex-wrap gap-2',
                children: [
                  jsx(Button, {
                    disabled: connectionStale || testMutation.isPending,
                    onClick: () => testMutation.mutate(),
                    variant: 'outline',
                    children: '测试 8 秒模拟倒计时'
                  }),
                  jsx(Button, {
                    disabled: connectionStale || displayMutation.isPending,
                    onClick: () => displayMutation.mutate(),
                    variant: 'outline',
                    children: '立即测试关闭显示器'
                  })
                ]
              })
            ]
          }),
          jsxs('div', {
            className: 'sticky bottom-0 flex flex-wrap items-center justify-between gap-3 border-t border-(--ui-stroke-secondary) bg-(--ui-surface-primary)/95 py-3 backdrop-blur',
            children: [
              jsx('span', {
                className: 'text-xs text-(--ui-text-tertiary)',
                children: dirty ? '先保存设置，再启用。' : '设置已保存。启用后只观察之后开始的任务。'
              }),
              jsxs('div', {
                className: 'flex gap-2',
                children: [
                  settings.armed
                    ? jsx(Button, {
                        disabled: cancelMutation.isPending,
                        onClick: () => cancelMutation.mutate({ reason: 'desktop_footer' }),
                        variant: 'outline',
                        children: '解除启用'
                      })
                    : jsx(Button, {
                        disabled: connectionStale || dirty || armMutation.isPending || armConfirmOpen,
                        onClick: () => setArmConfirmOpen(true),
                        children: armConfirmOpen
                          ? '确认窗口已打开'
                          : draft.dry_run
                            ? '检查并确认模拟模式'
                            : `检查并确认自动${ACTION_LABELS[draft.action] || draft.action}`
                      })
                ]
              })
            ]
          })
        ]
      })
    ]
  })
}

function PowerGuardStatus() {
  const queryClient = useQueryClient()
  const notifiedToken = useRef('')
  const busyBySession = useValue(host.state.busyBySession)
  const busyCountRef = useRef(0)
  busyCountRef.current = Object.values(busyBySession || {}).filter(Boolean).length
  useQuery({
    queryFn: () =>
      request('/ui-heartbeat', {
        method: 'POST',
        body: { busy_count: busyCountRef.current, instance_id: UI_INSTANCE_ID }
      }),
    queryKey: ['power-guard', 'ui-heartbeat'],
    refetchInterval: 2000,
    retry: false
  })
  const statusQuery = useQuery({
    queryFn: () => request('/status'),
    queryKey: QUERY_KEY,
    refetchInterval: 2000
  })
  const cancelMutation = useMutation({
    mutationFn: () => request('/cancel', { method: 'POST', body: { reason: 'status_bar' } }),
    onSuccess: data => queryClient.setQueryData(QUERY_KEY, data),
    onError: error => host.notifyError(error, '取消 Power Guard 失败')
  })

  const status = statusQuery.data
  const runtime = status?.runtime
  const settings = status?.settings
  const token = runtime?.state === 'countdown' ? runtime.countdown_token || '' : ''

  useEffect(() => {
    if (!token || token === notifiedToken.current) return
    notifiedToken.current = token
    const seconds = runtime?.countdown_remaining_seconds ?? settings?.countdown_seconds ?? 0
    const action = ACTION_LABELS[settings?.action] || settings?.action || '电源动作'
    host.notify({
      kind: 'warning',
      title: `即将自动${action}`,
      message: `${formatSeconds(seconds)}后执行。点击状态栏闪电按钮可立即取消。`,
      detail: '任何键鼠活动或新 Hermes 任务也会自动撤销。'
    })
    try {
      pluginContext?.os.notify({
        activate: '/power-guard',
        body: `${formatSeconds(seconds)}后自动${action}。打开 Hermes 或点击状态栏闪电按钮可取消。`,
        title: `Power Guard · 即将${action}`
      })
    } catch (_) {
      // Native notification is an optional accelerator; polling/toast remains.
    }
  }, [token, runtime?.countdown_remaining_seconds, settings?.action])

  const persistentState = ['countdown', 'error', 'expired'].includes(runtime?.state)
  if (!status || (!settings?.armed && !persistentState)) return null
  const countdown = runtime?.state === 'countdown'
  const errorState = runtime?.state === 'error'
  const snoozed = runtime?.snooze_remaining_seconds != null
  const label = countdown
    ? `${runtime.countdown_remaining_seconds ?? 0}s`
    : errorState
      ? '执行失败'
      : runtime?.state === 'expired'
        ? '武装已过期'
        : snoozed
          ? `${Math.max(1, Math.ceil(runtime.snooze_remaining_seconds / 60))}m`
          : runtime?.active_tasks || runtime?.active_background_processes || runtime?.active_delegations || runtime?.desktop_busy_count
            ? '运行中'
            : '已启用'
  const icon = countdown || errorState ? 'warning' : snoozed ? 'watch' : 'power'
  const accessibilityLabel = countdown ? 'Power Guard 倒计时，点击立即取消' : `Power Guard：${stateLabel(runtime)}`

  return jsxs('button', {
    'aria-label': accessibilityLabel,
    className:
      'inline-flex h-full items-center gap-1 rounded-none px-1.5 text-[0.6875rem] tabular-nums text-(--ui-text-tertiary) transition-colors hover:bg-(--chrome-action-hover) hover:text-foreground',
    disabled: countdown && cancelMutation.isPending,
    onClick: () => {
      if (countdown) cancelMutation.mutate()
      else host.navigate('/power-guard')
    },
    type: 'button',
    children: [
      jsx(Codicon, { name: icon, size: '0.75rem' }),
      jsx('span', { children: countdown && cancelMutation.isPending ? '取消中…' : label })
    ]
  })
}

export default {
  id: 'power-guard',
  name: 'Power Guard',
  description: '全部 Hermes 工作完成或阻塞后，经过安全门和倒计时自动睡眠，并提供节能控制。',
  defaultEnabled: true,
  register(ctx) {
    pluginContext = ctx
    ctx.registerMany([
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: '/power-guard' },
        render: () => jsx(PowerGuardPage, {})
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        order: 75,
        data: { codicon: 'power', label: '自动睡眠', path: '/power-guard' }
      },
      {
        id: 'status',
        area: STATUSBAR_AREAS.right,
        order: 95,
        render: () => jsx(PowerGuardStatus, {})
      },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: 'power-guard.open',
          keywords: ['power', 'shutdown', 'energy', '关机', '节能'],
          label: 'Power Guard: 打开自动睡眠面板',
          run: () => host.navigate('/power-guard')
        }
      },
      {
        id: 'snooze',
        area: PALETTE_AREA,
        data: {
          id: 'power-guard.snooze',
          keywords: ['snooze', 'delay', 'sleep', '延后', '睡眠'],
          label: 'Power Guard: 延后自动睡眠 15 分钟',
          run: () => {
            request('/snooze', { method: 'POST', body: { minutes: 15 } })
              .then(data => {
                host.notify({ kind: 'info', message: `自动睡眠已延后 ${formatSeconds(data.runtime.snooze_remaining_seconds)}。` })
                return data
              })
              .catch(error => host.notifyError(error, '延后自动睡眠失败'))
          }
        }
      },
      {
        id: 'cancel',
        area: PALETTE_AREA,
        data: {
          id: 'power-guard.cancel',
          keywords: ['cancel', 'shutdown', '取消', '关机'],
          label: 'Power Guard: 取消自动睡眠/倒计时',
          run: () => {
            request('/cancel', { method: 'POST', body: { reason: 'command_palette' } })
              .then(data => {
                host.notify({ kind: 'info', message: 'Power Guard 已取消。' })
                return data
              })
              .catch(error => host.notifyError(error, '取消 Power Guard 失败'))
          }
        }
      }
    ])
  }
}
