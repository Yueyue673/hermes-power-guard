# Changelog

All notable changes to this project are documented here.

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
