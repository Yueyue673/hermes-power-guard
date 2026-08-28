# Changelog

All notable changes to this project are documented here.

## [0.4.2] - 2026-08-29

### Safety hardening

- Define the arm boundary after all pre-arm registry sampling and keep external sensor calls outside SQLite write transactions.
- Baseline only long-running terminal processes; cron runs, subagents, and completion deliveries always remain blockers.
- Track baseline units and process start times so work appearing after the arm boundary cannot be hidden by a stable identity.
- Resample live background registries during the final adjacent safety check instead of trusting only the preceding heartbeat.
- Repeat action/simulation, task, background, Desktop-busy, protected-process, and user-input checks in the token micro-check immediately before the OS call.
- Add an arm request token so concurrent cancel/settings changes win, and migrate tasks that start while the pre-arm sample is running into the new generation.
- Move protected-process and active-power-plan probes outside SQLite write transactions.
- Preserve an existing explicit `wait_all` policy because 0.4.0 did not record whether it came from a preset or a deliberate strict choice.

## [0.4.1] - 2026-08-28

### Fixed

- Pre-existing preview servers, daemons, and already-running cron jobs are now captured as the one-shot campaign baseline instead of blocking sleep forever.
- Background work started after confirmation still blocks until it finishes.
- Duplicate heartbeat observations from multiple Hermes processes are deduplicated by source and stable ID instead of being summed.
- Existing 0.4.0 installations migrate once from the former `wait_all` default to campaign-scoped `wait_new`; users can still select the strict policy afterwards.

## [0.4.0] - 2026-08-28

### Product design

- Rebuilt the Desktop page around four focused views: Overview, Rules, Energy, and History.
- Replaced the card/metric wall with one current conclusion, one primary action, flat evidence, and progressively disclosed conditions.
- Rewrote runtime language around state, evidence, next event, and action; removed internal lifecycle jargon from the primary UI.
- Split end-of-work policy from during-work energy controls.
- Added a committed product design direction, benchmark evidence, state matrix, and language contract.

### Project presentation

- Replaced the long engineering-first README with a concise product entry in English and Chinese.
- Added a restrained project hero and faithful UI preview.
- Added compatibility and troubleshooting documentation plus structured issue and pull-request templates.

### Compatibility

- Added Desktop UI contract version 2 for precise decision copy and compatibility notices.

## [0.3.0] - 2026-08-28

### Added

- Native Desktop control page, sidebar entry, status-bar state, and command-palette actions.
- Seven-gate eligibility explanation and recent evidence history.
- Safe, success-only, and overnight energy presets.
- Snooze/resume and campaign expiry.
- Per-renderer Desktop heartbeats and connection-loss safe pause.
- Conservative clock/resume discontinuity handling.

### Safety

- Canonical single monitor per process.
- Fail-awake background sensors.
- Profile/session/turn-safe task identities.
- Late approval recovery and Kanban generation reuse handling.
- Keyboard/mouse cancellation independent of the pre-countdown idle gate.
- Desktop-only real action claiming and adjacent token checks.
- Serialized temporary power-plan changes with crash recovery.
- Strict lifecycle-reason boundaries.

### Verification

- 40 unit/API/concurrency/safety tests.
- Real isolated Hermes API-path simulation.
- No real power action in tests.
