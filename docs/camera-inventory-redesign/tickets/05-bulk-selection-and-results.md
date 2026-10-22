# 05: [Camera Admin 5] Add explicit bulk selection, reviewed changes and failed-only retry

Status: ready-for-agent

Issue: https://github.com/YaBoiSean319/nvr/issues/72

## Parent

https://github.com/YaBoiSean319/nvr/issues/67

## What to build

Apply supported administrative changes to an explicit reviewed set of inventory cameras with clear per-camera results and controlled retry.

Design and implementation handoff: https://github.com/YaBoiSean319/nvr/tree/docs/nvr-camera-inventory-redesign

## Acceptance criteria

- [ ] Show bulk controls only when checkboxes are selected; keep editor selection independent and display total and hidden-by-filter counts.
- [ ] Provide Clear selection and explicitly counted Select all matching cameras; filtering/refresh does not silently change the checked set.
- [ ] Preview exact camera IDs/names, intended changes, supported policy consequences and exclusions before apply; execute that reviewed set rather than recomputing a filter.
- [ ] Retain supported group assignment, recording mode and recording enablement actions; add no new destructive bulk operation.
- [ ] Revalidate missing, archived and no-longer-authorized cameras and surface their outcomes instead of silently omitting them.
- [ ] Implement genuine per-camera success/failure semantics across API/UI as needed while preserving supported API compatibility and bounded workload.
- [ ] Reconcile uncertain transport outcomes before retry; Retry failed revalidates and resubmits only eligible failures, excluding prior successes.
- [ ] Closing a result panel cannot falsely claim in-flight work was canceled; indicate incomplete/unknown outcomes and recovery accurately.
- [ ] Keep recording priority, administrator authorization, stable identities and existing policy semantics; clear session-specific bulk state on logout.
- [ ] Test hidden selection, fixed preview scope, partial failures, unknown outcomes and failed-only retry using observable persisted outcomes.

## Blocked by

- https://github.com/YaBoiSean319/nvr/issues/69

## Verification

Use authenticated observable browser/API behavior, deterministic camera fixtures and generated playable media as applicable. Preserve recording priority, identity, authorization, media/history, retention/holds and existing supported workflows. Do not count markup text or HTTP success alone as proof a complete workflow works.

## Post-merge testing required

- [ ] Exercise a controlled batch on the deployment and verify recording health, inherited-policy results and selective retry; record version, environment, workload, steps, owner and evidence.
