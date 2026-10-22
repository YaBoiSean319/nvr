# Camera administration implementation handoff

Specification: [#67](https://github.com/YaBoiSean319/nvr/issues/67)

Status: Approved design; ready-for-agent implementation tickets.

Baseline: current main includes merged PR #66; observed commit 6069e037ad7290ee02ea949878e7b4f9057dc15c. Refresh main and reconcile newer work before implementation.

## Ticket graph

| Ticket | Delivers | Blocked by |
| --- | --- | --- |
| [#68](https://github.com/YaBoiSean319/nvr/issues/68) | Separate workspaces and deliver the compact camera inventory | None |
| [#69](https://github.com/YaBoiSean319/nvr/issues/69) | Deliver focused camera settings tabs with safe saves and drafts | [#68](https://github.com/YaBoiSean319/nvr/issues/68) |
| [#70](https://github.com/YaBoiSean319/nvr/issues/70) | Separate discovery into a results table and one device setup panel | [#69](https://github.com/YaBoiSean319/nvr/issues/69) |
| [#71](https://github.com/YaBoiSean319/nvr/issues/71) | Deliver focused manual addition and CSV review/import workflows | [#69](https://github.com/YaBoiSean319/nvr/issues/69) |
| [#72](https://github.com/YaBoiSean319/nvr/issues/72) | Add explicit bulk selection, reviewed changes and failed-only retry | [#69](https://github.com/YaBoiSean319/nvr/issues/69) |
| [#73](https://github.com/YaBoiSean319/nvr/issues/73) | Organize Groups & Policies around one focused group editor | [#69](https://github.com/YaBoiSean319/nvr/issues/69) |
| [#74](https://github.com/YaBoiSean319/nvr/issues/74) | Add an on-demand selected-camera preview with complete teardown | [#69](https://github.com/YaBoiSean319/nvr/issues/69) |
| [#75](https://github.com/YaBoiSean319/nvr/issues/75) | Verify integrated browser workflows, responsiveness and deployment gates | [#70](https://github.com/YaBoiSean319/nvr/issues/70), [#71](https://github.com/YaBoiSean319/nvr/issues/71), [#72](https://github.com/YaBoiSean319/nvr/issues/72), [#73](https://github.com/YaBoiSean319/nvr/issues/73), [#74](https://github.com/YaBoiSean319/nvr/issues/74) |

The initial frontier is #68. After #68, implement #69. Tickets #70–74 can then proceed independently; #75 verifies the integrated result.

## Source documents

- [Approved design decisions](../camera-inventory-redesign.md)
- [Full specification](spec.md)
- [Domain glossary](../../GLOSSARY.md)
- Individual mirrored issue briefs are in the tickets directory. The linked GitHub issues and any later comments are authoritative for subsequent changes.

## Implementation boundary

Implement the inventory table and exactly one selected-camera editor; keep Add Cameras and Groups & Policies separate. Preserve existing recording/identity/media/history/hold/retention/evidence semantics and supported controls. This planning handoff contains no application implementation changes.

Use authenticated browser journeys backed by real APIs, isolated stores, deterministic cameras and generated playable media. Core browser workflows require executable checks. Record unrun deployment-only validation with explicit owner/environment/version/evidence fields; the original production and 30-day acceptance gates remain open.

## Suggested next-session request

> /implement-spec Please implement NVR specification #67 and tickets #68–75 from current main, work through their blocking dependencies, run the required checks, and open a PR for review. Do not merge the PR.
