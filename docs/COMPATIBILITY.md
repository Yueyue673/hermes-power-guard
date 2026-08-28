# Compatibility

## Runtime

| Component | Support |
|---|---|
| Operating system | Windows for real sleep/shutdown/hibernate/lock actions |
| Hermes surface | Local Hermes Desktop-managed backend |
| Python | 3.11 tested |
| Desktop plugin | Disk-loaded ESM through `@hermes/plugin-sdk` |
| Source tests | Windows and Ubuntu CI |

Power Guard can be imported and tested outside Hermes, but production lifecycle and background-work sensors use Hermes APIs when available.

## Power actions

| Action | Behavior |
|---|---|
| Sleep | Non-forced `SetSuspendState`; default |
| Hibernate | Available only when Windows reports hibernation support |
| Shutdown | Immediate Windows shutdown request after the same confirmation path |
| Lock | Windows workstation lock |
| Notify | Runs the decision path without a power-state change |

Unsupported Windows sleep states are disabled in the Desktop selector. Power Guard does not enable hibernation on the user's behalf.

## Hermes version boundary

Power Guard v0.4 expects Desktop status contract version 2 for precise state/evidence/next-event messages. If the Desktop UI updates before the backend, the page shows a compatibility notice and keeps power actions conservative until Hermes restarts.

The Python plugin and Desktop UI have separate enable switches. Both are required for a real action because the Desktop lease is part of cancellation safety.

## Scope

Completion cohort:

- work in the active Hermes profile observed after explicit confirmation;
- supported Kanban lifecycle events observed by the plugin;
- background processes, subagents, completion deliveries, and cron runs that start after confirmation.

Pre-existing background work is recorded as the campaign baseline by default. The strict `wait_all` policy is available when every already-running Hermes process should remain a veto.

Safety blockers:

- connected Desktop busy sessions/windows;
- background terminal processes;
- pending completion deliveries;
- async subagents;
- currently running cron jobs;
- protected executables;
- unavailable required sensors.

Not covered:

- a completely independent remote backend;
- a detached profile not connected to the current Desktop;
- future scheduled cron runs that are not currently running;
- arbitrary non-Hermes processes unless explicitly listed as protected.

## Upgrade

Pull or overlay the new source, enable the Python plugin if necessary, then restart Hermes Desktop once. Restart always cancels an existing one-shot rule; upgrades never preserve an armed state.
