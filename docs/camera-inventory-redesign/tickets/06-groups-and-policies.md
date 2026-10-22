# 06: [Camera Admin 6] Organize Groups & Policies around one focused group editor

Status: ready-for-agent

Issue: https://github.com/YaBoiSean319/nvr/issues/73

## Parent

https://github.com/YaBoiSean319/nvr/issues/67

## What to build

Manage shared camera policies in a separate compact workspace with one selected group and understandable inheritance rather than stacked global forms.

Design and implementation handoff: https://github.com/YaBoiSean319/nvr/tree/docs/nvr-camera-inventory-redesign

## Acceptance criteria

- [ ] Provide a searchable group list and one selected-group editor with focused sections/tabs, bounded panes, and clear selection/empty states.
- [ ] Preserve group create/rename/browsing membership separately from policy-group inheritance; avoid silently changing camera identity or membership meanings.
- [ ] Retain weekly recording schedules, retention, storage assignment, group AI settings and effective-policy/override source information.
- [ ] Preserve timezone, overnight-start-day, overlap, DST, inherited settings and camera override-removal behavior.
- [ ] Reuse visual schedule/zone editors with keyboard alternatives and normalized coordinates; do not fall back to mandatory routine JSON editing.
- [ ] Use explicit Save/Revert and the shared dirty-navigation/failed-save contract for group selection, tabs, loads and workspace changes.
- [ ] Place existing system-wide AI runtime/model configuration in the appropriate existing App Settings area, preserving functionality and authorization without copying it into each group.
- [ ] Keep all mutations administrator-only and retain authorized operator effective-policy inspection through camera details.
- [ ] Test actual saved group policies, inheritance/override round trips, dirty-group navigation and permission boundaries through browser/API behavior.

## Blocked by

- https://github.com/YaBoiSean319/nvr/issues/69

## Verification

Use authenticated observable browser/API behavior, deterministic camera fixtures and generated playable media as applicable. Preserve recording priority, identity, authorization, media/history, retention/holds and existing supported workflows. Do not count markup text or HTTP success alone as proof a complete workflow works.

## Post-merge testing required

- [ ] Confirm effective policies, storage placement and optional AI runtime configuration on the upgraded persistent deployment; record version, environment, workload, steps, owner and evidence.
