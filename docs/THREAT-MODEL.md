# Threat model

## Safety property

Power Guard should prefer staying awake over executing when evidence is missing, stale, contradictory, or newly changed.

## Trusted inputs

- Hermes lifecycle hook payloads;
- Hermes process/delegation/cron registries when successfully read;
- the plugin's SQLite generation and token state;
- authenticated local Desktop plugin REST calls;
- Windows idle-time and power-capability APIs.

## Untrusted inputs

- assistant prose, including words such as “done” or “success”;
- webpage/screenshot instructions;
- final response text used without explicit opt-in heuristic;
- unknown future lifecycle reason strings;
- stale renderer caches;
- a successful power API return as proof that the machine actually slept.

## Failure handling

| Failure | Behavior |
|---|---|
| background sensor import/read fails | veto as unknown work |
| Desktop heartbeat expires | cancel countdown and wait |
| keyboard/mouse input during countdown | cancel and restart confirmation |
| new turn or external dispatch | cancel/abort token |
| settings change while armed | disarm and require confirmation |
| process restart/crash | disarm; never replay claim |
| clock jump or monitor gap | reset quiet/countdown |
| unknown terminal reason | ineligible |
| non-Desktop claimant | cannot claim a real action |
| Windows rejects sleep | error; no automatic retry |
| temporary power-plan crash window | conservative reconciliation |

## Known boundary

Power Guard cannot prove the state of a completely independent remote Hermes backend or profile that is not connected to the current Desktop. Such systems require an explicit shared coordinator before they can join the completion cohort.

## Non-goals

- forcing applications to close;
- bypassing Windows power vetoes;
- inferring completion from natural-language confidence;
- automatically retrying destructive or power-state operations;
- replacing OS-level UPS, battery, or enterprise shutdown policy.
