# Architecture

Power Guard is a unified Hermes plugin with four pieces:

```text
Hermes lifecycle hooks ─┐
background sensors ─────┼─> power_guard_core.py ─> SQLite state machine
Desktop heartbeat ──────┘             │
                                      ├─> plugin_api.py (scoped REST)
                                      └─> conservative Windows power adapter

Desktop plugin.js <──────── scoped REST ────────┘
```

## Authority boundaries

- Hermes hooks provide turn, tool, approval, gateway, and Kanban lifecycle signals.
- Hermes process/delegation/cron registries are safety sensors.
- SQLite is the cross-thread/process campaign authority.
- Desktop Renderer owns only presentation and its heartbeat lease.
- Windows is authoritative for supported sleep/hibernate states and whether a request is accepted.

## Campaign model

A campaign is explicitly armed and receives a monotonically increasing generation. Only tasks observed in that generation can satisfy completion. Existing work and all active background sensors remain safety vetoes.

Important runtime states include:

```text
disarmed
  -> armed_waiting_for_task
  -> running
  -> terminal/safety wait states
  -> quiescence
  -> countdown
  -> executing
  -> action_requested | action_complete | error
```

Snooze clears quiet/countdown eligibility and adds a `not-before` gate. Resume always recomputes eligibility. Restart disarms rather than replaying state.

## Action protocol

1. Evaluate task generation and every safety sensor.
2. Start a quiet window.
3. Mint a unique countdown token.
4. Keep the authenticated Desktop cancel lease fresh.
5. Atomically claim the due token and disarm future scheduling.
6. Wait briefly so concurrent cancellation can linearize.
7. Recheck task activity, background heartbeats, Desktop busy state, protected processes, input timestamp, action setting, and token owner.
8. Check the token again immediately adjacent to the OS adapter.
9. Request the action once; never auto-retry.

The OS call is outside the eligibility transaction. SQLite protects ownership, while repeated token checks allow new work and cancellation to abort before the irreversible boundary.

## Background sensor semantics

A required sensor has two meaningful results:

- healthy with zero or more work items;
- unavailable/unknown.

Unknown is represented as an active safety veto. It is never coerced to zero.

## Desktop heartbeat

Each renderer uses a unique instance ID. The backend aggregates fresh leases using maximum busy count, preventing an idle window from overwriting a busy peer. Losing every fresh UI lease blocks the campaign because the user can no longer see or cancel the countdown.

## Temporary power plans

Power-plan apply/restore is serialized with API cancellation. The previous plan is persisted before the external command. A `pending` crash marker is reconciled on restart:

- restore only if the current plan is exactly the plan Power Guard intended to apply;
- clear without overwriting if a user or another program selected a third plan.
