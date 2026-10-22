# Approved NVR workspace overhaul

Approved October 22, 2026. Planning only: no production application changes are included in this branch.

Canonical specification: [#49](https://github.com/YaBoiSean319/nvr/issues/49). All 16 implementation issues are open and labeled ready-for-agent. Dependencies are recorded explicitly in each issue's Blocked by section because the available publishing interface does not provide native dependency/sub-issue writes. Work only tickets whose blockers have completed; this reference branch is not an integration branch.

## Start a fresh implementation session

Read the canonical specification and the issue selected from the frontier. Use the approved visual reference and glossary, then inspect current main before editing. This branch is a documentation snapshot based on main commit 088ed9ec82ddfcbc6222330bf92d69a1b0b25ee7; begin implementation from current main and bring over reference documents only as needed. Do not replace newer production code with this snapshot.

The initial frontier is [#50](https://github.com/YaBoiSean319/nvr/issues/50). After the shell lands, Auto Live, camera administration and system settings become independent paths according to the blockers below. Each ticket is an end-to-end demonstrable slice, with prefactoring inside working delivery rather than a separate horizontal rewrite.

The restriction against implementation applied to the planning session. A later session authorized by the user may implement these issues. Merge/deployment instructions must come from that later session; publication of this plan does not itself authorize deployment.

## Sources and precedence

- The canonical specification is authoritative, including real branding settings added after the preview.
- [Approved interactive preview source](approved-preview.fragment.html) records visual/interaction direction; sample frames and simulated actions are not production behavior. It is an in-conversation HTML fragment and depends on that preview host for icons. It is reference source, not a deployable application or an implementation starting point.
- [Design decisions](design-decisions.md) record the interview and delegated choices.
- [Specification snapshot](specification.md) is a publication-time copy of the canonical issue.
- The root glossary defines Live workspace, Playback workspace, selected camera, active tile, Auto layout, saved view and site branding.
- Earlier #1/#33 recording, role, evidence, archive, audio and time contracts remain unless #49 explicitly supersedes them. In particular: individual exports compress each camera's gaps; grid compresses only globally empty spans; one ZIP retains mappings/checksums/source-time overlays.

## Implementation tickets

| Issue | Deliverable | Blocked by |
| --- | --- | --- |
| [#50](https://github.com/YaBoiSean319/nvr/issues/50) | Ship the application shell with a real Live viewing journey | None |
| [#51](https://github.com/YaBoiSean319/nvr/issues/51) | Make Live camera selection drive Auto tiling and focus | #50 |
| [#52](https://github.com/YaBoiSean319/nvr/issues/52) | Unify camera/group/view browsing and workspace restoration | #51 |
| [#53](https://github.com/YaBoiSean319/nvr/issues/53) | Deliver adaptive Live quality and truthful tile recovery | #51 |
| [#54](https://github.com/YaBoiSean319/nvr/issues/54) | Build the Playback workspace with a shared interactive timeline | #51 |
| [#55](https://github.com/YaBoiSean319/nvr/issues/55) | Extend synchronized playback to sixteen real camera feeds | #54 |
| [#56](https://github.com/YaBoiSean319/nvr/issues/56) | Complete investigation tools, frame navigation and export marks | #54 |
| [#57](https://github.com/YaBoiSean319/nvr/issues/57) | Ship sixteen-camera evidence exports and durable job controls | #55, #56 |
| [#58](https://github.com/YaBoiSean319/nvr/issues/58) | Replace camera forms with a complete inventory/detail workflow | #50 |
| [#59](https://github.com/YaBoiSean319/nvr/issues/59) | Add a visual weekly recording schedule editor | #58 |
| [#60](https://github.com/YaBoiSean319/nvr/issues/60) | Add visual motion and AI zone editing | #58 |
| [#61](https://github.com/YaBoiSean319/nvr/issues/61) | Organize system settings with real saves and complete access | #50 |
| [#62](https://github.com/YaBoiSean319/nvr/issues/62) | Implement persistent application branding with safe assets | #61 |
| [#63](https://github.com/YaBoiSean319/nvr/issues/63) | Make operational health and incident actions available everywhere | #53 |
| [#64](https://github.com/YaBoiSean319/nvr/issues/64) | Finish responsive, accessible and visually consistent workflows | #52, #56, #57, #59, #60, #62, #63 |
| [#65](https://github.com/YaBoiSean319/nvr/issues/65) | Measure responsiveness and record deployment acceptance gates | #64 |

## Verification and completion

Use the approved authenticated API/browser boundary and generated playable footage; real frames/rendered MP4s are required to establish media behavior. Existing HTML-string assertions are not a substitute for browser workflow tests. Follow the repository's current acceptance guidance, including its warning about an unwanted Analytics network test, instead of claiming unrun checks passed.

UI target: p95 input-to-painted-feedback ≤100 ms with 200+ inventory entries and background exports. Healthy-stream reference goals: p95 first decoded fresh live frame ≤2 seconds and seek-to-decoded-footage ≤1 second. Declare hardware/network/streams and cold/warm conditions, report errors/timeouts and preserve recording priority. These are requirements to validate, not measured results or a 200-camera recording certification.

Every issue carries unchecked Post-merge testing required work. Record version, environment/workload, steps, owner and evidence; keep implementation-complete and deployment-validated separate. Preserve the original 30-day parallel reliability qualification gate. Do not close the overall qualification gate on CI or prototype approval alone.

## Branding acceptance reminder

Persistent administrator-controlled app name, colors, primary/light/dark logos and compact icon; real save/reset/public sign-in presentation; validated bounded uploads; safe fallbacks and legible themes; no new required Compose mount, remote logo URLs, executable theme content or footage watermark. See #62 and #49 for the complete contract.
