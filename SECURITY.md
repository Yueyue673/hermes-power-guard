# Security Policy

## Supported version

Security fixes target the latest release.

## Reporting a vulnerability

Please do not publish a working power-action bypass in a public issue. Use GitHub's private vulnerability reporting for this repository when available, or contact the repository owner through their GitHub profile.

Include:

- Hermes Agent and Power Guard versions;
- operating system and Desktop/CLI surface;
- whether the campaign was armed and which action was selected;
- exact lifecycle sequence;
- a minimal reproduction that does not execute a real power action.

## Security posture

Power Guard is fail-awake by design. Unknown lifecycle states, broken background sensors, missing Desktop heartbeat, user input during countdown, clock discontinuities, and claim-token changes block or restart the action path.

The project does not accept changes that make a destructive action easier by trusting assistant prose, provider output, web-page content, or unstructured prompt text.
