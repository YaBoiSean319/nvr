# 03: [Camera Admin 3] Separate discovery into a results table and one device setup panel

Status: ready-for-agent

Issue: https://github.com/YaBoiSean319/nvr/issues/70

## Parent

https://github.com/YaBoiSean319/nvr/issues/67

## What to build

Discover and review unadded devices in Add Cameras without mixing them into the existing camera inventory or rendering a form for every result.

Design and implementation handoff: https://github.com/YaBoiSean319/nvr/tree/docs/nvr-camera-inventory-redesign

## Acceptance criteria

- [ ] Discover is a dedicated Add Cameras mode with compact multicast/private-range scan controls, progress/error/retry and last-scan context.
- [ ] Render a bounded searchable results table and one selected-device setup panel; preserve current scan/network/time/concurrency limits.
- [ ] Default to devices not yet added; Show added devices labels linked results and offers Open existing camera while disabling duplicate addition.
- [ ] Reuse actual identity/linkage validation, handle incomplete advertised identity honestly, and reject concurrent duplicate add attempts server-side.
- [ ] Retain advertised endpoint/profile loading, brand/address/separate credential fields, stream tests and existing link-to-inventory behavior.
- [ ] Linking a discovered device to an existing camera preserves stable identity and historical media associations.
- [ ] Adding requires explicit review/test/action; default to motion policy with recording disabled and preserve explicit administrator enablement prerequisites.
- [ ] Failed tests/adds preserve safe inputs; successful addition updates inventory and removes the device from the unadded results without losing scan context.
- [ ] Guard dirty candidate setup during selection/workspace changes, reconcile stale scan/add responses, and never echo or persist saved secrets.
- [ ] Test scan fixtures, linked/unlinked results, failed/retried adds, link-existing and concurrent duplicate rejection through observable browser/API behavior.

## Blocked by

- https://github.com/YaBoiSean319/nvr/issues/69

## Verification

Use authenticated observable browser/API behavior, deterministic camera fixtures and generated playable media as applicable. Preserve recording priority, identity, authorization, media/history, retention/holds and existing supported workflows. Do not count markup text or HTTP success alone as proof a complete workflow works.

## Post-merge testing required

- [ ] Verify multicast and routed private-range discovery with intended devices and network configuration; record version, environment, workload, steps, owner and evidence.
