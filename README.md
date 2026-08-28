# Hermes Power Guard

![Hermes Power Guard — wait for Hermes work, show the countdown, then request sleep](assets/hero.svg)

**Put Windows to sleep after Hermes has no observable work left—not after an assistant merely says it is done.**

Each use requires one confirmation. Power Guard watches the work that starts afterward, explains what is still keeping the PC awake, and shows a countdown you can cancel before it asks Windows to sleep.

[Download v0.4.0](https://github.com/Yueyue673/hermes-power-guard/releases/latest) · [Install](#install) · [How it decides](#how-it-decides) · [Troubleshooting](docs/TROUBLESHOOTING.md) · [中文](README.zh-CN.md)

![Power Guard overview while one session and one subagent are still active](assets/ui-preview.svg)

## Install

Windows PowerShell:

```powershell
git clone https://github.com/Yueyue673/hermes-power-guard.git "$env:LOCALAPPDATA\hermes\plugins\power-guard"
hermes plugins enable power-guard --no-allow-tool-override
```

Restart Hermes Desktop once. In **Settings → Plugins**, enable **Power Guard**. The Python plugin and Desktop UI have separate switches; both must be on. Installation never enables automatic sleep.

Already cloned elsewhere? Run `python scripts/install.py`.

## How it decides

### It waits for work, not words

A final chat reply is not enough. Active Hermes sessions, background terminals, pending completion deliveries, subagents, running cron jobs, busy Desktop windows, and protected applications can all keep the PC awake.

By default, background processes and cron jobs that were already running when you confirmed are treated as the campaign baseline. Work started afterwards still blocks. A strict “wait for all Hermes background processes” policy remains available.

### It stays awake when evidence is missing

If a required sensor, task result, input reading, or Desktop connection is unknown, Power Guard does not continue toward sleep. A restart also cancels the current one-shot rule.

### It cancels when you return

Keyboard or mouse input during the countdown cancels it. New Hermes work, a rule change, an offline control panel, or a clock/resume gap also restarts the checks.

## The path to sleep

```text
Confirm this one use
  → observe a new Hermes task
  → wait for every tracked item to finish
  → check Desktop, input, and protected applications
  → watch briefly for late work
  → show a cancelable countdown
  → check tasks and input one last time
  → ask Windows to sleep once
```

The sleep request is non-forced. Windows or an application may reject it to protect unsaved work. Power Guard reports the rejection and does not retry automatically.

## Use

Open **Power Guard** from the Desktop sidebar.

- **Overview** shows the current conclusion, evidence, next event, and one primary action.
- **Rules** controls what counts as finished and how long confirmation takes.
- **Energy** controls power use while Hermes is still working.
- **History** shows task results and state changes.

Save the rule, then select **Enable this automatic sleep**. It will not act on work that started before confirmation.

Inspection and simulation commands:

```text
/power-guard status
/power-guard cancel
/power-guard snooze 15
/power-guard resume
/power-guard test 8
```

## Compatibility and boundary

| | Boundary |
|---|---|
| Real power actions | Windows; local Hermes Desktop-managed backend |
| Default action | Non-forced sleep |
| Work that can finish the rule | Active-profile work observed after confirmation |
| Work that can block it | Connected Desktop sessions, background processes, subagents, running cron jobs, protected applications, unknown sensors |
| Automated tests | Simulation only; no real power action |
| Interface language | Chinese in v0.4; English documentation included |

A separate remote backend or detached profile that is not connected to the current Desktop cannot be proven idle. Power Guard does not claim to cover it.

## Project documents

- [Product design](DESIGN.md)
- [Benchmarks](docs/BENCHMARKS.md)
- [State matrix](docs/STATE-MATRIX.md)
- [Product language](docs/PRODUCT-LANGUAGE.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Threat model](docs/THREAT-MODEL.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Compatibility](docs/COMPATIBILITY.md)
- [Security policy](SECURITY.md)
- [Contributing](CONTRIBUTING.md)
- [Changelog](CHANGELOG.md)

MIT — see [LICENSE](LICENSE).
