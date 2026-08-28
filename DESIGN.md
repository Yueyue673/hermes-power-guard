# Power Guard Product Design

## Direction: Operational calm

Power Guard is a background safety utility. It should feel like a reliable system panel: quiet when nothing needs attention, exact when a condition blocks sleep, and unmistakable during the final countdown.

The product borrows structure—not branding—from mature tools:

- **Syncthing:** priorities are explicit and ordered; safety wins over convenience.
- **Tailscale:** technical boundaries are stated plainly instead of hidden behind marketing.
- **Raycast:** one primary action, keyboard-safe confirmation, compact native control surfaces.
- **Linear:** information hierarchy comes from luminance, spacing, and type—not card decoration.
- **Hermes Desktop:** host tokens, host primitives, and flat-over-boxed composition.

## Product priorities

In this order:

1. **Do not sleep on incomplete evidence.** Unknown is a blocking state.
2. **Keep cancellation visible.** A real action requires a live Desktop lease.
3. **Explain the present condition.** The user should know what is blocking and what happens next.
4. **Require deliberate consent.** Every campaign is one-shot and policy-bound.
5. **Stay out of the way.** When disarmed or working normally, the UI should be quiet.

Lower priorities may not weaken higher ones.

## Information architecture

The page has three durable views:

### Overview

The daily control surface. It contains, in order:

1. one current conclusion;
2. one supporting sentence with evidence;
3. one primary action;
4. a flat execution path;
5. compact live counts;
6. only the most recent action result.

### Policy

Rules and energy controls. Presets change the draft only. Basic settings appear first; uncommon heuristics, protected processes, and energy tuning are progressively disclosed.

### History

Task outcomes and state events, presented as a chronological audit trail. History is evidence, not decoration.

## Visual grammar

### Host-native

The plugin inherits Hermes typography, background, and accent. It does not create a second brand inside the app.

Use only:

- `--ui-text-*` for hierarchy;
- `--ui-stroke-tertiary` for list hairlines;
- `--ui-accent` for the current step or primary action;
- semantic destructive/warning/success primitives for actual states;
- SDK components for buttons, dialogs, switches, inputs, status dots, and segmented controls.

### Flat over boxed

- No metric cards.
- No card inside a card.
- No border around every gate.
- No rounded container whose only purpose is grouping.
- Group with 16–24px whitespace and a single tertiary hairline.
- Reserve a framed surface for countdown, connection loss, or error.

### Density

- 4px for micro-alignment.
- 8px between tightly related items.
- 16px between rows in one section.
- 24px between sections.
- One dominant element per viewport region.

### Status hierarchy

| State | Treatment |
|---|---|
| Disarmed | muted text, no persistent status-bar item |
| Working / waiting | neutral text + one active status dot |
| Eligible / quiet | accent on current step only |
| Countdown | warning surface, timer, explicit cancel action |
| Error | destructive message with the rejected action and recovery path |
| Requested | neutral confirmation: request sent, outcome not claimed |

## Copy grammar

Every runtime message answers these fields:

1. **State:** what is true now?
2. **Evidence:** which task, count, process, or timer proves it?
3. **Next:** what event advances the system?
4. **Action:** what can the user do, if anything?

Good:

- `Hermes 还有 2 项工作`
- `1 个后台进程和 1 个子代理仍在运行。全部结束后开始 30 秒确认。`
- `你刚刚使用了键盘，倒计时已取消。`
- `Windows 没有接受睡眠请求。Power Guard 不会自动重试。`

Avoid:

- `正在重新计算当前状态`
- `安全门逐门放行`
- `稳妥、智能、强大、完善`
- `当前终态不允许执行`
- unexplained internal nouns such as `generation`, `claim`, `武装`, or `turn_exit_reason`

Internal terms remain in logs and technical documentation, not primary UI.

## Controls

- Primary disarmed action: `启用本次自动睡眠`.
- Primary armed action: no repeated enable button; show `取消本次自动睡眠` as secondary/destructive according to timing.
- Countdown action: `取消睡眠`.
- Snooze action: `延后 15 分钟`.
- Policy changes while active: save, cancel the current campaign, then require a fresh confirmation.
- Confirm dialog title: `启用本次自动睡眠?`.

## Repository presentation

The GitHub README is a product entry point, not an internal verification report.

Order:

1. product banner and one concrete sentence;
2. screenshot or accurate UI preview;
3. install in under one screen;
4. three behaviors that distinguish the product;
5. decision flow;
6. compatibility and scope boundary;
7. links to architecture, threat model, security, and contributing.

Move exhaustive test lists, implementation detail, and long safety rationale into docs. Do not use file counts or self-congratulatory claims as proof of maturity.

## Review gates

A design change is complete only when:

- the first viewport has one obvious conclusion and one primary action;
- every visible sentence contains state, evidence, next event, or action;
- no nested card or redundant border remains;
- every color and control comes from the host design system;
- disarmed, working, blocked, offline, countdown, cancelled, requested, and error states have distinct copy;
- keyboard and screen-reader behavior is verified;
- README claims match tested behavior and documented scope;
- a real screenshot or faithful generated preview is updated with the release.
