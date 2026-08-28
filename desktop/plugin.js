import {
  Badge,
  Button,
  Codicon,
  ConfirmDialog,
  ErrorState,
  host,
  Input,
  Loader,
  PALETTE_AREA,
  ROUTES_AREA,
  SegmentedControl,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  SIDEBAR_NAV_AREA,
  STATUSBAR_AREAS,
  StatusDot,
  Switch,
  Textarea,
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

const ACTION_LABELS = {
  shutdown: '关机',
  hibernate: '休眠',
  sleep: '睡眠',
  lock: '锁屏',
  notify: '只提醒'
}

const VIEW_OPTIONS = [
  { id: 'overview', label: '概览' },
  { id: 'policy', label: '规则' },
  { id: 'energy', label: '节能' },
  { id: 'history', label: '记录' }
]

const PRESETS = [
  {
    id: 'balanced',
    label: '平衡',
    description: '完成或明确无法继续；空闲 5 分钟；确认 30 秒；倒计时 90 秒。',
    values: {
      action: 'sleep', trigger_mode: 'done_or_blocked', include_failures: true,
      include_interrupted: false, detect_blocker_text: false, require_user_idle: true,
      user_idle_seconds: 300, quiescence_seconds: 30, countdown_seconds: 90,
      waiting_input_timeout_seconds: 900, background_process_policy: 'wait_all',
      prevent_sleep_while_working: true, turn_off_display_when_idle: false,
      lower_hermes_priority: false, power_plan: 'unchanged', arm_expiry_minutes: 720, dry_run: false
    }
  },
  {
    id: 'success-only',
    label: '仅成功',
    description: '失败、阻塞、中断或未知结果都保持唤醒。',
    values: {
      action: 'sleep', trigger_mode: 'done_only', include_failures: false,
      include_interrupted: false, detect_blocker_text: false, require_user_idle: true,
      user_idle_seconds: 600, quiescence_seconds: 60, countdown_seconds: 120,
      background_process_policy: 'wait_all', arm_expiry_minutes: 720, dry_run: false
    }
  },
  {
    id: 'overnight-eco',
    label: '夜间节能',
    description: '确认和倒计时各 2 分钟；工作时关闭显示器并使用平衡计划。',
    values: {
      action: 'sleep', trigger_mode: 'done_or_blocked', include_failures: true,
      include_interrupted: false, detect_blocker_text: false, require_user_idle: true,
      user_idle_seconds: 300, quiescence_seconds: 60, countdown_seconds: 120,
      background_process_policy: 'wait_all', prevent_sleep_while_working: true,
      turn_off_display_when_idle: true, display_off_idle_seconds: 300,
      lower_hermes_priority: true, power_plan: 'balanced', restore_power_plan: true,
      arm_expiry_minutes: 1440, dry_run: false
    }
  }
]

function request(path, options = {}) {
  if (!pluginContext) return Promise.reject(new Error('Power Guard plugin context is not ready'))
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
    blocker_keywords: String(draft.blocker_keywords_text || '').split(/[,\n]/).map(value => value.trim()).filter(Boolean),
    protected_processes: String(draft.protected_processes_text || '').split(/[,;\n]/).map(value => value.trim()).filter(Boolean)
  }
}

function workCounts(status) {
  const backend = status.decision?.counts
  if (backend) {
    return {
      turns: backend.turns || 0,
      processes: backend.background || 0,
      agents: backend.delegations || 0,
      windows: backend.desktop_busy || 0
    }
  }
  const runtime = status.runtime || {}
  return {
    turns: runtime.active_tasks || 0,
    processes: runtime.active_background_processes || 0,
    agents: runtime.active_delegations || 0,
    windows: runtime.desktop_busy_count || 0
  }
}

function toneForState(state) {
  if (state === 'error') return 'bad'
  if (['countdown', 'terminal_not_eligible', 'waiting_for_desktop_ui', 'waiting_for_idle_check', 'waiting_for_protected_process', 'expired'].includes(state)) return 'warn'
  if (['quiescence', 'action_requested', 'action_complete'].includes(state)) return 'good'
  return 'muted'
}

function conclusionFor(status) {
  const runtime = status.runtime || {}
  const settings = status.settings || {}
  const action = ACTION_LABELS[settings.action] || settings.action || '电源动作'
  const counts = workCounts(status)
  const total = counts.turns + counts.processes + counts.agents + counts.windows
  const evidence = `会话 ${counts.turns} · 后台 ${counts.processes} · 子代理 ${counts.agents} · 其他窗口 ${counts.windows}`
  if (status.capabilities?.ui_contract_version >= 2 && status.decision?.summary) {
    return {
      tone: toneForState(runtime.state),
      title: status.decision.summary,
      evidence: status.decision.detail,
      next: status.decision.next
    }
  }

  if (!settings.armed) return {
    tone: 'muted', title: `自动${action}未启用`,
    evidence: `当前不会执行任何自动电源动作。${evidence}`,
    next: '确认规则后，可以只为接下来开始的新任务启用一次。'
  }
  if (runtime.state === 'countdown') return {
    tone: 'warn', title: `${formatSeconds(runtime.countdown_remaining_seconds)}后将${action}`,
    evidence: `当前工作已结束，最后倒计时正在进行。${evidence}`,
    next: '任何新任务或键鼠活动都会取消；你也可以现在取消。'
  }
  if (runtime.state === 'error') return {
    tone: 'bad', title: `${action}请求失败`,
    evidence: runtime.last_error || 'Windows 没有接受请求，Power Guard 不会自动重试。',
    next: '检查错误后重新启用，或先运行模拟测试。'
  }
  if (runtime.state === 'action_requested') return {
    tone: 'muted', title: `${action}请求已发送`,
    evidence: runtime.last_action?.message || '系统已接收请求，但 Power Guard 不把它表述为已经完成。',
    next: '不会自动重试同一个请求。'
  }
  if (runtime.state === 'expired') return {
    tone: 'warn', title: '本次自动操作已过期',
    evidence: '有效期结束前没有满足执行条件，因此没有发送电源请求。',
    next: '需要时请重新确认启用。'
  }
  if (runtime.state === 'snoozed' || runtime.snooze_remaining_seconds != null) return {
    tone: 'muted', title: `已延后 ${formatSeconds(runtime.snooze_remaining_seconds)}`,
    evidence: `延后期间不会执行${action}。${evidence}`,
    next: '时间结束后会重新检查，或现在恢复检查。'
  }
  if (total > 0 || runtime.state === 'running') return {
    tone: 'good', title: `Hermes 还有 ${total} 项工作`,
    evidence,
    next: `全部结束并满足空闲条件后，开始 ${formatSeconds(settings.quiescence_seconds)} 的最后确认。`
  }
  if (runtime.state === 'waiting_for_desktop_ui') return {
    tone: 'warn', title: '等待桌面控制面板在线',
    evidence: '没有可用的桌面取消入口，因此不会开始电源操作。',
    next: '保持 Hermes Desktop 打开并等待连接恢复。'
  }
  if (runtime.state === 'waiting_for_protected_process') return {
    tone: 'warn', title: '受保护程序仍在运行',
    evidence: runtime.protected_process || '检测到规则中列出的程序。',
    next: '程序退出后会重新检查。'
  }
  if (runtime.state === 'waiting_for_idle_check') return {
    tone: 'warn', title: '无法确认用户是否空闲',
    evidence: '空闲状态未知，因此不会执行电源操作。',
    next: '恢复系统空闲检测后会重新检查。'
  }
  if (runtime.state === 'waiting_for_user_idle') return {
    tone: 'muted', title: '正在等你离开电脑',
    evidence: `当前空闲 ${runtime.user_idle_seconds == null ? '状态未知' : formatSeconds(runtime.user_idle_seconds)}，规则要求 ${formatSeconds(settings.user_idle_seconds)}。`,
    next: '达到空闲时间后会进入最后确认。'
  }
  if (runtime.state === 'terminal_not_eligible') return {
    tone: 'warn', title: '任务结果不符合当前规则',
    evidence: '至少一个任务的结果不是规则允许的完成状态。',
    next: '查看历史记录，或调整规则后重新确认启用。'
  }
  if (runtime.state === 'quiescence') return {
    tone: 'good', title: '工作已结束，正在最后确认',
    evidence: `没有检测到剩余工作。确认窗口为 ${formatSeconds(settings.quiescence_seconds)}。`,
    next: `窗口内没有新活动，就开始 ${formatSeconds(settings.countdown_seconds)} 倒计时。`
  }
  return {
    tone: 'muted', title: '等待接下来开始的新任务',
    evidence: `本次自动${action}已启用，但不会追溯启用前的工作。`,
    next: '新任务开始后，Power Guard 才会跟踪结果。'
  }
}

function Row({ label, help, children, top = false }) {
  return jsxs('div', {
    className: `grid gap-2 py-3 md:grid-cols-[minmax(12rem,0.9fr)_minmax(15rem,1.1fr)] ${top ? 'md:items-start' : 'md:items-center'}`,
    children: [
      jsxs('div', { className: 'flex min-w-0 flex-col gap-1', children: [
        jsx('span', { className: 'text-sm font-medium text-(--ui-text-primary)', children: label }),
        help ? jsx('span', { className: 'text-xs leading-5 text-(--ui-text-tertiary)', children: help }) : null
      ] }),
      jsx('div', { className: 'min-w-0 md:justify-self-end', children })
    ]
  })
}

function ToggleRow({ label, help, checked, onChange, disabled = false }) {
  return jsx(Row, { label, help, children: jsxs('div', { className: 'flex items-center gap-2', children: [
    jsx(Switch, { 'aria-label': label, checked: Boolean(checked), disabled, onCheckedChange: onChange, size: 'xs' }),
    jsx('span', { className: 'w-8 text-xs text-(--ui-text-secondary)', children: checked ? '开' : '关' })
  ] }) })
}

function NumberRow({ label, help, value, onChange, min = 0, max = 86400, suffix = '秒', disabled = false }) {
  return jsx(Row, { label, help, children: jsxs('div', { className: 'flex items-center gap-2', children: [
    jsx(Input, { className: 'w-28 tabular-nums', disabled, max, min, onChange: event => onChange(Number(event.target.value)), type: 'number', value: value ?? 0 }),
    jsx('span', { className: 'w-10 text-xs text-(--ui-text-tertiary)', children: suffix })
  ] }) })
}

function SelectRow({ label, help, value, onChange, options }) {
  return jsx(Row, { label, help, children: jsxs(Select, { value, onValueChange: onChange, children: [
    jsx(SelectTrigger, { className: 'w-64 max-w-full', children: jsx(SelectValue, {}) }),
    jsx(SelectContent, { children: options.map(option => jsx(SelectItem, { disabled: Boolean(option.disabled), value: option.value, children: option.label }, option.value)) })
  ] }) })
}

function FlatSection({ title, description, children, defaultOpen = true }) {
  const [open, setOpen] = useState(defaultOpen)
  return jsxs('section', { className: 'border-t border-(--ui-stroke-tertiary) pt-4', children: [
    jsxs(Button, { className: 'w-full items-start justify-between gap-4 text-left', onClick: () => setOpen(value => !value), size: 'inline', variant: 'text', children: [
      jsxs('span', { className: 'flex min-w-0 flex-col gap-1', children: [
        jsx('span', { className: 'text-sm font-semibold text-(--ui-text-primary)', children: title }),
        description ? jsx('span', { className: 'text-xs leading-5 text-(--ui-text-tertiary)', children: description }) : null
      ] }),
      jsx(Codicon, { name: open ? 'chevron-up' : 'chevron-down', size: '0.9rem' })
    ] }),
    open ? jsx('div', { className: 'mt-2 divide-y divide-(--ui-stroke-tertiary)', children }) : null
  ] })
}

function RuntimeHero({ status, dirty, connectionStale, onArm, cancelMutation, snoozeMutation, resumeMutation }) {
  const conclusion = conclusionFor(status)
  const runtime = status.runtime
  const settings = status.settings
  const countdown = runtime.state === 'countdown'
  return jsxs('section', { 'aria-atomic': 'true', 'aria-live': 'polite', className: 'flex flex-col gap-4', role: 'status', children: [
    jsxs('div', { className: 'flex items-center gap-2', children: [
      jsx(StatusDot, { tone: conclusion.tone }),
      jsx('span', { className: 'text-xs text-(--ui-text-secondary)', children: ACTION_LABELS[settings.action] || settings.action }),
      settings.dry_run ? jsx(Badge, { variant: 'muted', children: '模拟模式' }) : null
    ] }),
    jsxs('div', { className: 'flex max-w-3xl flex-col gap-2', children: [
      jsx('h2', { className: 'text-2xl font-semibold leading-tight text-(--ui-text-primary)', children: conclusion.title }),
      jsx('p', { className: 'text-sm leading-6 text-(--ui-text-secondary)', children: conclusion.evidence }),
      jsx('p', { className: 'text-sm leading-6 text-(--ui-text-tertiary)', children: conclusion.next })
    ] }),
    jsxs('div', { className: 'flex flex-wrap gap-2', children: [
      !settings.armed
        ? jsx(Button, { disabled: connectionStale || dirty, onClick: onArm, children: dirty ? '先保存规则' : `启用本次自动${ACTION_LABELS[settings.action] || settings.action}` })
        : jsx(Button, { disabled: cancelMutation.isPending, onClick: () => cancelMutation.mutate({ reason: 'desktop_primary' }), variant: countdown ? 'destructive' : 'secondary', children: countdown ? `取消${ACTION_LABELS[settings.action] || settings.action}` : `取消本次自动${ACTION_LABELS[settings.action] || settings.action}` }),
      settings.armed && status.capabilities?.snooze_supported
        ? runtime.snooze_remaining_seconds != null
          ? jsx(Button, { disabled: resumeMutation.isPending, onClick: () => resumeMutation.mutate(), variant: 'outline', children: '现在恢复检查' })
          : jsx(Button, { disabled: snoozeMutation.isPending, onClick: () => snoozeMutation.mutate(15), variant: 'outline', children: '延后 15 分钟' })
        : null
    ] })
  ] })
}

function ExecutionPath({ status }) {
  const gates = status.decision?.gates || []
  const [open, setOpen] = useState(false)
  if (!gates.length) return null
  const tone = gateStatus => gateStatus === 'pass' ? 'good' : gateStatus === 'blocked' ? 'bad' : gateStatus === 'active' ? 'warn' : 'muted'
  const current = gates.find(gate => gate.status === 'blocked' || gate.status === 'active') || gates.find(gate => gate.status !== 'pass') || gates[gates.length - 1]
  return jsxs('section', { className: 'border-t border-(--ui-stroke-tertiary) pt-4', children: [
    jsxs(Button, { className: 'w-full items-center justify-between gap-4 text-left', onClick: () => setOpen(value => !value), size: 'inline', variant: 'text', children: [
      jsxs('span', { className: 'flex min-w-0 items-center gap-2', children: [
        jsx(StatusDot, { tone: tone(current.status) }),
        jsxs('span', { className: 'min-w-0 text-sm text-(--ui-text-secondary)', children: [
          jsx('span', { className: 'font-medium text-(--ui-text-primary)', children: current.label }),
          current.detail ? ` · ${current.detail}` : ''
        ] })
      ] }),
      jsxs('span', { className: 'flex shrink-0 items-center gap-1 text-xs text-(--ui-text-tertiary)', children: [
        open ? '收起条件' : '查看全部条件',
        jsx(Codicon, { name: open ? 'chevron-up' : 'chevron-down', size: '0.85rem' })
      ] })
    ] }),
    open ? jsx('ol', { className: 'mt-2 divide-y divide-(--ui-stroke-tertiary)', children: gates.map(gate =>
      jsxs('li', { 'aria-current': gate.status === 'active' ? 'step' : undefined, className: 'grid grid-cols-[auto_minmax(0,1fr)] items-center gap-x-3 gap-y-1 py-2.5 md:grid-cols-[auto_minmax(8rem,0.65fr)_minmax(10rem,1.35fr)]', children: [
        jsx(StatusDot, { tone: tone(gate.status) }),
        jsx('span', { className: gate.status === 'active' ? 'text-sm font-medium text-(--ui-text-primary)' : 'text-sm text-(--ui-text-secondary)', children: gate.label }),
        jsx('span', { className: 'col-start-2 text-xs text-(--ui-text-tertiary) md:col-start-auto md:text-right', children: gate.detail || (gate.value != null ? String(gate.value) : '') })
      ] }, gate.id)
    ) }) : null
  ] })
}

function LiveEvidence({ status }) {
  const runtime = status.runtime
  const counts = workCounts(status)
  const items = [
    ['会话', counts.turns], ['后台', counts.processes], ['子代理', counts.agents], ['其他窗口', counts.windows],
    ['键鼠空闲', runtime.user_idle_seconds == null ? '未知' : formatSeconds(runtime.user_idle_seconds)],
    [runtime.snooze_remaining_seconds != null ? '延后剩余' : '本次剩余', runtime.snooze_remaining_seconds != null ? formatSeconds(runtime.snooze_remaining_seconds) : runtime.armed_remaining_seconds != null ? formatSeconds(runtime.armed_remaining_seconds) : '—']
  ]
  return jsx('dl', { className: 'grid gap-x-6 gap-y-2 border-t border-(--ui-stroke-tertiary) pt-4 sm:grid-cols-2 lg:grid-cols-6', children: items.flatMap(([label, value]) => [
    jsxs('div', { className: 'flex items-baseline justify-between gap-3 lg:flex-col lg:items-start lg:gap-1', children: [
      jsx('dt', { className: 'text-xs text-(--ui-text-tertiary)', children: label }),
      jsx('dd', { className: 'text-sm font-medium tabular-nums text-(--ui-text-primary)', children: String(value) })
    ] }, label)
  ]) })
}

function LastResult({ status }) {
  const runtime = status.runtime
  if (!runtime.last_error && !runtime.last_action) return null
  return jsxs('section', { className: `border-t border-(--ui-stroke-tertiary) pt-4 ${runtime.last_error ? 'text-destructive' : ''}`, children: [
    jsx('h3', { className: 'text-xs font-medium', children: runtime.last_error ? '最近错误' : '最近操作' }),
    jsx('p', { className: `mt-1 text-xs leading-5 ${runtime.last_error ? '' : 'text-(--ui-text-secondary)'}`, children: runtime.last_error || `${runtime.last_action.simulated ? '模拟' : '实际'}${ACTION_LABELS[runtime.last_action.action] || runtime.last_action.action}：${runtime.last_action.message}` })
  ] })
}

function OverviewView(props) {
  return jsxs('div', { className: 'flex flex-col gap-6', children: [
    jsx(RuntimeHero, props),
    jsx(ExecutionPath, { status: props.status }),
    jsx(LiveEvidence, { status: props.status }),
    jsx(LastResult, { status: props.status })
  ] })
}

function PolicyView({ status, draft, update, applyPreset }) {
  const actionOptions = [
    { value: 'shutdown', label: ACTION_LABELS.shutdown }, { value: 'lock', label: ACTION_LABELS.lock },
    { value: 'sleep', label: status.capabilities?.sleep_available ? ACTION_LABELS.sleep : '睡眠（当前系统不可用）', disabled: !status.capabilities?.sleep_available },
    { value: 'hibernate', label: status.capabilities?.hibernate_available ? ACTION_LABELS.hibernate : '休眠（当前未启用）', disabled: !status.capabilities?.hibernate_available },
    { value: 'notify', label: ACTION_LABELS.notify }
  ]
  return jsxs('div', { className: 'flex flex-col gap-6', children: [
    jsxs('section', { children: [
      jsx('h2', { className: 'text-base font-semibold text-(--ui-text-primary)', children: '从预设开始' }),
      jsx('p', { className: 'mt-1 text-xs leading-5 text-(--ui-text-tertiary)', children: '预设只修改草稿；保存后才会生效。' }),
      jsx('div', { className: 'mt-4 divide-y divide-(--ui-stroke-tertiary) border-y border-(--ui-stroke-tertiary)', children: PRESETS.map(preset =>
        jsx('div', { className: 'py-3', children: jsxs(Button, { className: 'w-full items-start justify-between gap-4 text-left', onClick: () => applyPreset(preset), size: 'inline', variant: 'text', children: [
          jsxs('span', { className: 'flex flex-col gap-1', children: [jsx('span', { className: 'text-sm font-medium text-(--ui-text-primary)', children: preset.label }), jsx('span', { className: 'text-xs leading-5 text-(--ui-text-tertiary)', children: preset.description })] }),
          jsx(Codicon, { className: 'text-(--ui-text-quaternary)', name: 'chevron-right', size: '0.9rem' })
        ] }) }, preset.id)
      ) })
    ] }),
    jsxs(FlatSection, { title: '基本规则', description: '选择最终动作、允许的任务结果和确认时间。', children: [
      jsx(SelectRow, { label: '最终动作', help: '倒计时结束后向 Windows 请求。', value: draft.action, onChange: value => update('action', value), options: actionOptions }),
      jsx(SelectRow, { label: '任务结果', help: '“无法自动继续”包括重试耗尽、等待人工和授权墙。', value: draft.trigger_mode, onChange: value => update('trigger_mode', value), options: [{ value: 'done_or_blocked', label: '全部完成，或明确无法自动继续' }, { value: 'done_only', label: '只有全部成功完成' }] }),
      jsx(ToggleRow, { label: '把失败视为无法继续', help: '只在“完成或无法继续”规则下生效。', checked: draft.include_failures, onChange: value => update('include_failures', value), disabled: draft.trigger_mode !== 'done_or_blocked' }),
      jsx(ToggleRow, { label: '把手动中断视为结束', help: '默认关闭，避免点击停止后随即触发。', checked: draft.include_interrupted, onChange: value => update('include_interrupted', value), disabled: draft.trigger_mode !== 'done_or_blocked' }),
      jsx(NumberRow, { label: '最后确认窗口', help: '工作结束后继续观察迟到的子代理和后台进程，最短 30 秒。', value: draft.quiescence_seconds, onChange: value => update('quiescence_seconds', value), min: 30, max: 3600 }),
      jsx(NumberRow, { label: '最后倒计时', help: '状态栏和系统通知都会提供取消机会，最短 60 秒。', value: draft.countdown_seconds, onChange: value => update('countdown_seconds', value), min: 60, max: 3600 }),
      jsx(NumberRow, { label: '本次有效期', help: '过期后自动取消，避免隔天误触发。', value: draft.arm_expiry_minutes, onChange: value => update('arm_expiry_minutes', value), min: 30, max: 10080, suffix: '分钟' })
    ] }),
    jsxs(FlatSection, { title: '无法继续的识别', description: '结构化结果优先；可选用最终回复中的关键词补足。', defaultOpen: false, children: [
      jsx(ToggleRow, { label: '识别回复中的阻塞语句', help: '关闭后只使用 Hermes 的结构化结果。', checked: draft.detect_blocker_text, onChange: value => update('detect_blocker_text', value) }),
      jsx(Row, { label: '阻塞关键词', help: '一行一个，中英文均可；只匹配最终回复。', top: true, children: jsx(Textarea, { className: 'min-h-32 w-80 max-w-full resize-y', disabled: !draft.detect_blocker_text, onChange: event => update('blocker_keywords_text', event.target.value), value: draft.blocker_keywords_text || '' }) }),
      jsx(NumberRow, { label: '等待人工多久后算阻塞', help: '澄清、审批等需要输入的工具，在此时间内仍算运行中。', value: draft.waiting_input_timeout_seconds, onChange: value => update('waiting_input_timeout_seconds', value), min: 60, max: 86400 })
    ] }),
    jsxs(FlatSection, { title: '防误触发', description: '未知状态始终阻止执行；这些条件全部满足才进入最后确认。', defaultOpen: false, children: [
      jsx(ToggleRow, { label: '必须检测到用户空闲', help: '仍在使用键盘鼠标时不执行最终动作。', checked: draft.require_user_idle, onChange: value => update('require_user_idle', value) }),
      jsx(NumberRow, { label: '用户空闲时间', help: draft.require_user_idle ? '从最后一次键盘或鼠标输入开始计算。' : '启用上方规则后可设置。', value: draft.user_idle_seconds, onChange: value => update('user_idle_seconds', value), min: 0, max: 86400, disabled: !draft.require_user_idle }),
      jsx(SelectRow, { label: '后台进程', help: '预览服务器或守护进程可能长期运行。', value: draft.background_process_policy, onChange: value => update('background_process_policy', value), options: [{ value: 'wait_all', label: '等待全部 Hermes 后台进程' }, { value: 'ignore_detached', label: '忽略 detached 守护进程' }] }),
      jsx(Row, { label: '受保护程序', help: '这些 exe 运行时不执行，一行一个。', top: true, children: jsx(Textarea, { className: 'min-h-24 w-80 max-w-full resize-y', onChange: event => update('protected_processes_text', event.target.value), placeholder: 'blender.exe\nobs64.exe', value: draft.protected_processes_text || '' }) })
    ] })
  ] })
}

function EnergyView({ draft, update, connectionStale, testMutation, displayMutation }) {
  return jsxs('div', { className: 'flex flex-col gap-6', children: [
    jsxs('section', { children: [
      jsx('h2', { className: 'text-base font-semibold text-(--ui-text-primary)', children: '工作期间' }),
      jsx('p', { className: 'mt-1 text-xs leading-5 text-(--ui-text-tertiary)', children: '这些设置只影响 Hermes 工作时的耗电和前台体验，不改变任务结束后的判断规则。' }),
      jsx('div', { className: 'mt-3 divide-y divide-(--ui-stroke-tertiary) border-y border-(--ui-stroke-tertiary)', children: [
        jsx(ToggleRow, { label: '阻止系统自动睡眠', help: '只保持系统运行，不强制点亮显示器。', checked: draft.prevent_sleep_while_working, onChange: value => update('prevent_sleep_while_working', value) }),
        jsx(ToggleRow, { label: '空闲后关闭显示器', help: '移动鼠标即可唤醒，不会暂停 Hermes。', checked: draft.turn_off_display_when_idle, onChange: value => update('turn_off_display_when_idle', value) }),
        jsx(NumberRow, { label: '关闭显示器空闲时间', help: draft.turn_off_display_when_idle ? '只在 Hermes 仍有工作时生效。' : '启用上方规则后可设置。', value: draft.display_off_idle_seconds, onChange: value => update('display_off_idle_seconds', value), min: 30, max: 86400, disabled: !draft.turn_off_display_when_idle }),
        jsx(ToggleRow, { label: '降低 Hermes 进程优先级', help: '减少对前台应用的影响，任务可能略慢。', checked: draft.lower_hermes_priority, onChange: value => update('lower_hermes_priority', value) }),
        jsx(SelectRow, { label: '临时电源计划', help: '可在工作结束或取消后恢复原计划。', value: draft.power_plan, onChange: value => update('power_plan', value), options: [{ value: 'unchanged', label: '保持当前计划' }, { value: 'balanced', label: '平衡' }, { value: 'power_saver', label: '节能' }] }),
        jsx(ToggleRow, { label: '结束后恢复原计划', help: draft.power_plan === 'unchanged' ? '选择临时计划后可设置。' : '取消本次自动操作时也会恢复。', checked: draft.restore_power_plan, onChange: value => update('restore_power_plan', value), disabled: draft.power_plan === 'unchanged' })
      ] })
    ] }),
    jsxs(FlatSection, { title: '诊断工具', description: '模拟倒计时不会调用 Windows 电源 API；关闭显示器测试会真实关闭屏幕。', defaultOpen: false, children: [
      jsx(ToggleRow, { label: '全局模拟模式', help: '规则触发时只记录模拟结果，不发送电源请求。', checked: draft.dry_run, onChange: value => update('dry_run', value) }),
      jsx(Row, { label: '取消链路', help: '检查倒计时、通知和状态栏。', children: jsxs('div', { className: 'flex flex-wrap gap-2', children: [
        jsx(Button, { disabled: connectionStale || testMutation.isPending, onClick: () => testMutation.mutate(), variant: 'outline', children: '运行 8 秒模拟' }),
        jsx(Button, { disabled: connectionStale || displayMutation.isPending, onClick: () => displayMutation.mutate(), variant: 'outline', children: '关闭显示器测试' })
      ] }) })
    ] })
  ] })
}

function timeLabel(timestamp) {
  if (!timestamp) return ''
  try { return new Date(Number(timestamp) * 1000).toLocaleString([], { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' }) } catch (_) { return '' }
}

function eventLabel(event) {
  return ({
    armed: '已启用本次自动操作', cancelled: '已取消本次自动操作', settings: '规则已保存',
    settings_disarm: '规则变化，需要重新确认', turn_terminal: '任务已结束', quiescence: '开始最后确认',
    countdown_started: '倒计时开始', countdown_cancelled: '倒计时已取消', action_claimed: '开始最后检查',
    action_requested: '已发送系统请求', action_complete: '操作已完成', action_error: '系统请求失败',
    action_aborted: '最后检查发现变化，已取消', snoozed: '已延后本次操作', snooze_resumed: '已恢复检查',
    snooze_elapsed: '延后结束，重新检查', campaign_expired: '本次自动操作已过期', restart_disarm: '重启后自动取消',
    test: '模拟倒计时开始', test_execute: '模拟操作进入执行'
  }[event.kind] || event.message || event.kind)
}

function HistoryView({ status }) {
  const tasks = status.tasks || []
  const events = status.events || []
  const outcome = value => ({ completed: '完成', blocked: '无法继续', failed: '失败', interrupted: '中断', unknown: '未知' }[value] || value || '运行中')
  return jsxs('div', { className: 'flex flex-col gap-8', children: [
    jsxs('section', { children: [
      jsx('h2', { className: 'text-base font-semibold text-(--ui-text-primary)', children: '任务结果' }),
      jsx('p', { className: 'mt-1 text-xs text-(--ui-text-tertiary)', children: `${tasks.length} 条记录；未知结果不会被当作完成。` }),
      tasks.length ? jsx('div', { className: 'mt-3 divide-y divide-(--ui-stroke-tertiary) border-y border-(--ui-stroke-tertiary)', children: tasks.map(task => jsxs('div', { className: 'grid gap-2 py-3 text-xs sm:grid-cols-[minmax(0,1fr)_auto]', children: [
        jsxs('span', { className: 'flex min-w-0 flex-col gap-1', children: [
          jsx('span', { className: 'truncate text-sm text-(--ui-text-primary)', children: task.last_tool || task.platform || task.task_key }),
          jsx('span', { className: 'truncate text-(--ui-text-tertiary)', children: task.reason || '等待结果' })
        ] }),
        jsx(Badge, { variant: task.outcome === 'failed' ? 'destructive' : task.outcome === 'unknown' ? 'warn' : 'muted', children: outcome(task.outcome) })
      ] }, task.task_key)) }) : jsx('p', { className: 'mt-3 text-sm text-(--ui-text-tertiary)', children: '启用后开始的新任务会显示在这里。' })
    ] }),
    jsxs('section', { children: [
      jsx('h2', { className: 'text-base font-semibold text-(--ui-text-primary)', children: '状态记录' }),
      jsx('p', { className: 'mt-1 text-xs text-(--ui-text-tertiary)', children: `${events.length} 条事件，按时间倒序。` }),
      events.length ? jsx('ol', { className: 'mt-3 divide-y divide-(--ui-stroke-tertiary) border-y border-(--ui-stroke-tertiary)', children: events.map(event => jsxs('li', { className: 'grid gap-2 py-3 text-xs sm:grid-cols-[8rem_minmax(0,1fr)]', children: [
        jsx('time', { className: 'tabular-nums text-(--ui-text-quaternary)', children: timeLabel(event.created_at) }),
        jsx('span', { className: 'text-(--ui-text-secondary)', children: eventLabel(event) })
      ] }, event.id)) }) : jsx('p', { className: 'mt-3 text-sm text-(--ui-text-tertiary)', children: '还没有状态记录。' })
    ] })
  ] })
}

function ArmConfirmation({ draft }) {
  return jsxs('div', { className: 'flex flex-col gap-2 text-xs leading-5 text-(--ui-text-secondary)', children: [
    jsx('p', { children: `确认后不会立即${ACTION_LABELS[draft.action] || draft.action}，只观察确认后开始的新任务。` }),
    jsx('p', { children: `任务结果：${draft.trigger_mode === 'done_only' ? '仅全部成功' : '完成或明确无法继续'}。` }),
    jsx('p', { children: `键鼠空闲：${draft.require_user_idle ? formatSeconds(draft.user_idle_seconds) : '不作为进入门槛'}；迟到任务检查 ${formatSeconds(draft.quiescence_seconds)}；倒计时 ${formatSeconds(draft.countdown_seconds)}。` }),
    jsx('p', { children: `本次操作将在 ${formatSeconds((draft.arm_expiry_minutes || 720) * 60)}后自动取消；重启 Hermes 也会取消。` }),
    jsx('p', { className: 'font-medium text-(--ui-text-primary)', children: '新任务、键鼠活动、规则变化或控制面板离线都会取消倒计时或最后检查。' })
  ] })
}

function OfflineBanner({ onRetry }) {
  return jsxs('div', { className: 'flex items-start justify-between gap-3 border-y border-destructive/40 py-3', role: 'alert', children: [
    jsxs('div', { className: 'flex min-w-0 flex-col gap-1', children: [
      jsx('span', { className: 'text-sm font-medium text-destructive', children: '连接已中断，自动操作已暂停' }),
      jsx('span', { className: 'text-xs leading-5 text-(--ui-text-tertiary)', children: '当前显示上次成功获取的状态。保持 Desktop 在线是执行条件之一。' })
    ] }),
    jsx(Button, { onClick: onRetry, variant: 'outline', children: '重试' })
  ] })
}

function PowerGuardPage() {
  const queryClient = useQueryClient()
  const statusQuery = useQuery({ queryFn: () => request('/status'), queryKey: QUERY_KEY, refetchInterval: 2000 })
  const [view, setView] = useState('overview')
  const [draft, setDraft] = useState(null)
  const [dirty, setDirty] = useState(false)
  const [armConfirmOpen, setArmConfirmOpen] = useState(false)

  useEffect(() => {
    if (statusQuery.data?.settings && (!draft || !dirty)) setDraft(settingDraft(statusQuery.data.settings))
  }, [statusQuery.data?.settings, dirty])

  const refreshRuntime = data => queryClient.setQueryData(QUERY_KEY, data)
  const refreshSettings = data => {
    refreshRuntime(data)
    if (data?.settings) { setDraft(settingDraft(data.settings)); setDirty(false) }
  }
  const saveMutation = useMutation({
    mutationFn: payload => request('/settings', { method: 'POST', body: payload }),
    onSuccess: data => {
      const invalidated = Boolean(statusQuery.data?.settings?.armed && !data.settings?.armed)
      refreshSettings(data)
      if (invalidated) host.notify({ kind: 'warning', title: '规则已保存，本次自动操作已取消', message: '规则变化后需要重新确认，Power Guard 不会沿用旧授权。' })
    },
    onError: error => host.notifyError(error, 'Power Guard 规则保存失败')
  })
  const armMutation = useMutation({
    mutationFn: () => request('/arm', { method: 'POST', body: {} }),
    onSuccess: data => { refreshSettings(data); host.notify({ kind: 'success', title: '本次自动操作已启用', message: `只会跟踪现在之后开始的新任务；满足条件后自动${ACTION_LABELS[data.settings.action] || data.settings.action}。` }) },
    onError: error => host.notifyError(error, 'Power Guard 启用失败')
  })
  const cancelMutation = useMutation({
    mutationFn: body => request('/cancel', { method: 'POST', body }),
    onSuccess: data => { refreshRuntime(data); host.notify({ kind: 'info', message: '本次自动操作和倒计时已取消。' }) },
    onError: error => host.notifyError(error, '取消失败')
  })
  const snoozeMutation = useMutation({
    mutationFn: minutes => request('/snooze', { method: 'POST', body: { minutes } }),
    onSuccess: data => { refreshRuntime(data); host.notify({ kind: 'info', title: '已延后自动操作', message: `${formatSeconds(data.runtime.snooze_remaining_seconds)}后重新检查。` }) },
    onError: error => host.notifyError(error, '延后失败')
  })
  const resumeMutation = useMutation({
    mutationFn: () => request('/resume', { method: 'POST', body: {} }),
    onSuccess: data => { refreshRuntime(data); host.notify({ kind: 'info', message: '已取消延后，正在重新检查。' }) },
    onError: error => host.notifyError(error, '恢复检查失败')
  })
  const testMutation = useMutation({
    mutationFn: () => request('/test', { method: 'POST', body: { seconds: 8 } }),
    onSuccess: data => { refreshRuntime(data); host.notify({ kind: 'info', message: '已启动 8 秒模拟倒计时，不会执行真实电源操作。' }) },
    onError: error => host.notifyError(error, '模拟测试启动失败')
  })
  const displayMutation = useMutation({
    mutationFn: () => request('/display-off', { method: 'POST', body: {} }),
    onSuccess: () => host.notify({ kind: 'success', message: '已发送关闭显示器信号；移动鼠标即可唤醒。' }),
    onError: error => host.notifyError(error, '关闭显示器失败')
  })

  if (statusQuery.isLoading && !statusQuery.data) return jsx('div', { className: 'flex h-full items-center justify-center', children: jsx(Loader, { type: 'lemniscate-bloom' }) })
  if (statusQuery.error && !statusQuery.data) return jsx('div', { className: 'flex h-full items-center justify-center p-6', children: jsx(ErrorState, { title: 'Power Guard 后端尚未加载', description: '重启 Hermes Desktop 以挂载插件 API，然后重试。', children: jsx(Button, { onClick: () => statusQuery.refetch(), variant: 'outline', children: '重试连接' }) }) })

  const status = statusQuery.data
  if (!status || !draft) return null
  const connectionStale = Boolean(statusQuery.isError)
  const update = (key, value) => { setDraft(current => ({ ...current, [key]: value })); setDirty(true) }
  const applyPreset = preset => { setDraft(current => ({ ...current, ...preset.values })); setDirty(true) }

  return jsxs('div', { className: 'h-full overflow-auto', children: [
    jsxs('main', { className: 'mx-auto flex w-full max-w-5xl flex-col gap-5 p-5', children: [
      jsxs('header', { className: 'flex flex-wrap items-end justify-between gap-4', children: [
        jsxs('div', { className: 'flex flex-col gap-1', children: [
          jsx('h1', { className: 'text-lg font-semibold text-(--ui-text-primary)', children: 'Power Guard' }),
          jsx('p', { className: 'text-xs leading-5 text-(--ui-text-tertiary)', children: '等待会话、后台进程和子代理结束，再检查键鼠空闲并显示可取消倒计时。' })
        ] }),
        jsxs('div', { className: 'flex flex-wrap items-center gap-2', children: [
          jsx(SegmentedControl, { onChange: setView, options: VIEW_OPTIONS, value: view }),
          dirty ? jsx(Badge, { variant: 'warn', children: '未保存' }) : null,
          dirty ? jsx(Button, { disabled: connectionStale || saveMutation.isPending, onClick: () => saveMutation.mutate(draftPayload(draft)), size: 'sm', children: saveMutation.isPending ? '保存中…' : '保存规则' }) : null
        ] })
      ] }),
      connectionStale ? jsx(OfflineBanner, { onRetry: () => statusQuery.refetch() }) : null,
      Number(status.capabilities?.ui_contract_version || 0) < 2 ? jsx('div', { className: 'border-y border-(--ui-stroke-tertiary) py-3 text-xs leading-5 text-(--ui-text-secondary)', children: '新版状态说明需要重启 Hermes 后端后生效；当前不会执行未经确认的电源操作。' }) : null,
      view === 'overview' ? jsx(OverviewView, { status, dirty, connectionStale, onArm: () => setArmConfirmOpen(true), cancelMutation, snoozeMutation, resumeMutation }) : null,
      view === 'policy' ? jsx(PolicyView, { status, draft, update, applyPreset }) : null,
      view === 'energy' ? jsx(EnergyView, { draft, update, connectionStale, testMutation, displayMutation }) : null,
      view === 'history' ? jsx(HistoryView, { status }) : null
    ] }),
    jsx(ConfirmDialog, {
      cancelLabel: '返回检查规则', confirmLabel: `启用本次自动${ACTION_LABELS[draft.action] || draft.action}`,
      busyLabel: '正在启用…', description: jsx(ArmConfirmation, { draft }),
      destructive: !draft.dry_run && draft.action === 'shutdown', doneLabel: '已启用',
      onClose: () => setArmConfirmOpen(false), onConfirm: () => armMutation.mutateAsync(),
      open: armConfirmOpen && !status.settings.armed, title: `启用本次自动${ACTION_LABELS[draft.action] || draft.action}?`
    })
  ] })
}

function PowerGuardStatus() {
  const queryClient = useQueryClient()
  const notifiedToken = useRef('')
  const busyBySession = useValue(host.state.busyBySession)
  const busyCountRef = useRef(0)
  busyCountRef.current = Object.values(busyBySession || {}).filter(Boolean).length
  useQuery({
    queryFn: () => request('/ui-heartbeat', { method: 'POST', body: { busy_count: busyCountRef.current, instance_id: UI_INSTANCE_ID } }),
    queryKey: ['power-guard', 'ui-heartbeat'], refetchInterval: 2000, retry: false
  })
  const statusQuery = useQuery({ queryFn: () => request('/status'), queryKey: QUERY_KEY, refetchInterval: 2000 })
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
    const action = ACTION_LABELS[settings?.action] || settings?.action || '电源操作'
    host.notify({ kind: 'warning', title: `即将自动${action}`, message: `${formatSeconds(seconds)}后执行。点击状态栏按钮可立即取消。`, detail: '任何键鼠活动或新 Hermes 任务也会自动取消。' })
    try {
      pluginContext?.os.notify({ activate: '/power-guard', body: `${formatSeconds(seconds)}后自动${action}。打开 Hermes 或点击状态栏按钮可取消。`, title: `Power Guard · 即将${action}` })
    } catch (_) {
      // Native notification is optional; polling and the in-app notification remain authoritative.
    }
  }, [token, runtime?.countdown_remaining_seconds, settings?.action])

  const persistentState = ['countdown', 'error', 'expired'].includes(runtime?.state)
  if (!status || (!settings?.armed && !persistentState)) return null
  const countdown = runtime?.state === 'countdown'
  const errorState = runtime?.state === 'error'
  const snoozed = runtime?.snooze_remaining_seconds != null
  const active = runtime?.active_tasks || runtime?.active_background_processes || runtime?.active_delegations || runtime?.desktop_busy_count
  const label = countdown ? `${runtime.countdown_remaining_seconds ?? 0}s` : errorState ? '失败' : runtime?.state === 'expired' ? '已过期' : snoozed ? `${Math.max(1, Math.ceil(runtime.snooze_remaining_seconds / 60))}m` : active ? '工作中' : '已启用'
  const icon = countdown || errorState ? 'warning' : snoozed ? 'watch' : 'power'

  return jsxs('button', {
    'aria-label': countdown ? 'Power Guard 倒计时，点击立即取消' : `Power Guard：${conclusionFor(status).title}`,
    className: 'inline-flex h-full items-center gap-1 rounded-none px-1.5 text-[0.6875rem] tabular-nums text-(--ui-text-tertiary) transition-colors hover:bg-(--chrome-action-hover) hover:text-(--ui-text-primary)',
    disabled: countdown && cancelMutation.isPending,
    onClick: () => countdown ? cancelMutation.mutate() : host.navigate('/power-guard'), type: 'button',
    children: [jsx(Codicon, { name: icon, size: '0.75rem' }), jsx('span', { children: countdown && cancelMutation.isPending ? '取消中…' : label })]
  })
}

export default {
  id: 'power-guard',
  name: 'Power Guard',
  description: '等待 Hermes 会话、后台进程和子代理结束，显示可取消倒计时，再请求 Windows 睡眠。',
  defaultEnabled: true,
  register(ctx) {
    pluginContext = ctx
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: '/power-guard' }, render: () => jsx(PowerGuardPage, {}) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, order: 75, data: { codicon: 'power', label: '自动睡眠', path: '/power-guard' } },
      { id: 'status', area: STATUSBAR_AREAS.right, order: 95, render: () => jsx(PowerGuardStatus, {}) },
      { id: 'open', area: PALETTE_AREA, data: { id: 'power-guard.open', keywords: ['power', 'shutdown', 'energy', '关机', '节能'], label: 'Power Guard: 打开自动睡眠面板', run: () => host.navigate('/power-guard') } },
      { id: 'snooze', area: PALETTE_AREA, data: { id: 'power-guard.snooze', keywords: ['snooze', 'delay', 'sleep', '延后', '睡眠'], label: 'Power Guard: 延后自动睡眠 15 分钟', run: () => {
        request('/snooze', { method: 'POST', body: { minutes: 15 } }).then(data => host.notify({ kind: 'info', message: `自动睡眠已延后 ${formatSeconds(data.runtime.snooze_remaining_seconds)}。` })).catch(error => host.notifyError(error, '延后自动睡眠失败'))
      } } },
      { id: 'cancel', area: PALETTE_AREA, data: { id: 'power-guard.cancel', keywords: ['cancel', 'shutdown', '取消', '关机'], label: 'Power Guard: 取消自动睡眠或倒计时', run: () => {
        request('/cancel', { method: 'POST', body: { reason: 'command_palette' } }).then(() => host.notify({ kind: 'info', message: 'Power Guard 已取消。' })).catch(error => host.notifyError(error, '取消 Power Guard 失败'))
      } } }
    ])
  }
}
