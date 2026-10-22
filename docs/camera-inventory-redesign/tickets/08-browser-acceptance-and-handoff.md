# 08: [Camera Admin 8] Verify integrated browser workflows, responsiveness and deployment gates

Status: ready-for-agent

Issue: https://github.com/YaBoiSean319/nvr/issues/75

## Parent

https://github.com/YaBoiSean319/nvr/issues/67

## What to build

Qualify the integrated camera administration redesign with executable browser journeys, retained regressions and an honest deployment acceptance record.

Design and implementation handoff: https://github.com/YaBoiSean319/nvr/tree/docs/nvr-camera-inventory-redesign

## Acceptance criteria

- [ ] Run administrator/operator journeys covering inventory, one-camera editor, drafts, discovery/link/add, manual/CSV onboarding, bulk outcomes/retry, group policies and optional preview.
- [ ] Add/run real browser coverage in CI when no runner exists; replace obsolete markup/card assertions with observable workflow checks without removing useful API/media regression coverage.
- [ ] Exercise 0/1/200-camera fixtures, unknown metadata, empty/loading/error states, stale responses, filtered-out selection and invalid/deleted camera recovery.
- [ ] Verify 320/360/768/1024/1440 widths, touch and keyboard, table/tab/dialog focus restoration, non-hover actions, visible focus, status labels and no horizontal document overflow.
- [ ] Verify light/dark/system/branded contrast, reduced motion, accessible pane resizing and phone Back to Cameras restoring current-session table context.
- [ ] Keep only harmless per-user sort/columns/pane preferences persistent; clear session state on logout and verify drafts/secrets/preview do not persist.
- [ ] Measure representative click/filter/panel feedback with 200 configured entries against the existing responsiveness target; report environment, sample count, errors and observed timing without inventing results.
- [ ] Observe that inventory browsing starts no media per row and that preview churn releases resources while supported recording/media admission protections remain intact.
- [ ] Run appropriate repository/CI regressions for identity, inventory, discovery, groups/schedule/storage/retention, archive/holds/evidence, live/playback/audio and settings; document the known Analytics-test constraint where it applies.
- [ ] Publish sanitized actual verification evidence and explicitly unchecked deployment gates with version, environment/workload, steps, owner, result and evidence fields.
- [ ] Leave wider production performance/capacity and the original 30-day qualification gates open; a fixture inventory or passing CI is not production recording certification.

## Blocked by

- https://github.com/YaBoiSean319/nvr/issues/70
- https://github.com/YaBoiSean319/nvr/issues/71
- https://github.com/YaBoiSean319/nvr/issues/72
- https://github.com/YaBoiSean319/nvr/issues/73
- https://github.com/YaBoiSean319/nvr/issues/74

## Verification

Use authenticated observable browser/API behavior, deterministic camera fixtures and generated playable media as applicable. Preserve recording priority, identity, authorization, media/history, retention/holds and existing supported workflows. Do not count markup text or HTTP success alone as proof a complete workflow works.

## Post-merge testing required

- [ ] Complete supported-browser/device/identity/persistent-upgrade and recording-health walkthroughs on the actual deployment, and retain the original measured 30-day parallel pilot; record version, environment, workload, steps, owner and evidence.
