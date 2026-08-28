# Design benchmarks

Power Guard does not copy another product's skin. These references each solve a different maturity problem.

## Syncthing — ordered priorities

**Observed:** Syncthing's README states its goals in priority order: data safety, security, usability, automation, availability. Lower goals may not compromise higher ones.

**Borrow:** Power Guard orders safety, cancellation, explanation, consent, and quiet automation. This resolves trade-offs: a faster sleep is rejected when evidence is unknown.

**Do not borrow:** Syncthing's broad multi-platform scope. Power Guard's real action boundary remains Windows Desktop.

**Verification:** `DESIGN.md` lists ordered product priorities and the threat model implements unknown-as-veto.

## Tailscale — plain scope

**Observed:** Tailscale's README starts with one sentence, names what the repository contains, and explicitly states which GUI wrappers are outside the open-source tree.

**Borrow:** State the active-profile and disconnected-backend boundary in the README and UI without hiding it under feature language.

**Do not borrow:** Networking terminology or brand styling.

**Verification:** a first-time reader can answer what is and is not observed without opening architecture code.

## Raycast Extensions — product before contribution process

**Observed:** Raycast explains the product in one sentence, shows a real header image, then moves to contribution guidelines.

**Borrow:** Lead the repository with the actual control surface and one concrete outcome. Keep developer process below install and use.

**Do not borrow:** Raycast's red accent, glass shadows, or macOS-specific identity inside Hermes.

**Verification:** the README's first screen contains the product preview, purpose, and install—not test counts.

## LocalSend — install and compatibility are product content

**Observed:** LocalSend places screenshots, downloads, compatibility, and operational setup before build instructions. It also distinguishes unofficial builds.

**Borrow:** Put install, Windows/Hermes compatibility, and scope before development. Release artifacts must be directly downloadable and checksummed.

**Do not borrow:** its long multilingual table of contents or platform breadth.

**Verification:** installation fits in one screen and the Release asset is downloaded and checked during release verification.

## Linear — hierarchy through restraint

**Observed:** Linear's system uses near-neutral surfaces, one accent, subtle hairlines, and type/spacing rather than many bordered cards.

**Borrow:** one active state, one accent, flat list rows, progressive disclosure.

**Do not borrow:** the black-purple brand palette, proprietary fonts, or marketing atmosphere.

**Verification:** the Desktop plugin inherits Hermes tokens; no metric-card grid or nested card remains.

## Hermes Desktop — host authority

**Observed:** Hermes explicitly requires “tokens over literals, flat over boxed,” one primitive per concern, and intent before automation.

**Borrow:** native SDK controls, tertiary hairlines, host typography, one primary action, no custom plugin chrome.

**Do not borrow:** nothing; this is the host contract rather than an optional reference.

**Verification:** all controls come from `@hermes/plugin-sdk`, colors from `--ui-*`, and overlays use native dialogs.

## Sources

- https://github.com/syncthing/syncthing
- https://github.com/tailscale/tailscale
- https://github.com/raycast/extensions
- https://github.com/localsend/localsend
- https://linear.app
- Hermes Desktop `DESIGN.md`
