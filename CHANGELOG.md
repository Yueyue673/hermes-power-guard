# Changelog

All notable changes to this project are documented here.

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
